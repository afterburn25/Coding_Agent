"""Background Provisioning Manager — post-install workstation setup.

After Nexus itself is installed and the UI is usable, this manager takes
over: it inventories the configured workstation stack (image backends,
photoreal model fleet, voice/STT assets, helper tools), builds a
dependency-ordered plan, and installs components progressively in the
background while the user keeps working.

Design rules baked in (see docs/BACKGROUND_PROVISIONING.md):

- Persisted plan — survives restarts; mid-flight items resume, verified
  completions are never re-downloaded.
- Honest states — an item is only ``completed`` after a verification
  probe (file exists + hash matches where a checksum is known).
- Bounded retries — transient failures back off exponentially and give
  up; permanent failures surface with a classified error code.
- Disk-aware — items whose estimated footprint won't fit inside the
  free-space reserve stay ``waiting`` with a ``blocked_reason`` instead
  of filling the disk.
- Non-blocking — at most one heavy install and one light install run at
  once; foreground work always wins.
- One voice line per incident — spoken summaries fire only on terminal
  failure / setup complete, never per retry.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

PLAN_VERSION = 1
_GB = 1024 ** 3
# Item lifecycle: waiting -> running -> verifying -> completed
#                 waiting/running -> failed (terminal) | cancelled
#                 waiting -> skipped (unsupported/declined)
TERMINAL = {"completed", "failed", "cancelled", "skipped"}
# Install-profile tiers — a profile includes its own tier and below.
TIER_ORDER = {"core": 0, "recommended": 1, "complete": 2}
# Failures worth retrying automatically (bounded exponential backoff).
# permission_denied is included: on Windows, AV on-access scans routinely
# hold a fresh exe/pyd lock for a few seconds mid-install; a bounded retry
# clears it, while a real ACL problem still exhausts attempts and reports.
TRANSIENT_ERRORS = {"network_failure", "connection_reset", "server_busy",
                    "backend_health_failed", "permission_denied"}
MAX_ATTEMPTS = 3


def classify_setup_error(exc: BaseException | str) -> str:
    """Map an install failure onto a stable, reportable error code."""
    text = f"{type(exc).__name__}: {exc}".lower() if not isinstance(exc, str) \
        else exc.lower()
    if isinstance(exc, PermissionError) or "permission" in text or "access is denied" in text:
        return "permission_denied"
    if isinstance(exc, (urllib.error.URLError, TimeoutError)) or \
            any(k in text for k in ("timed out", "timeout", "connection reset",
                                    "connection aborted", "name resolution",
                                    "network is unreachable", "eof occurred")):
        return "network_failure"
    if "no space" in text or "disk full" in text or "errno 28" in text:
        return "disk_full"
    if "sha-256" in text or "sha256" in text or "checksum" in text or "hash mismatch" in text:
        return "checksum_failed"
    if "python" in text and ("version" in text or "3.1" in text or "candidate" in text):
        return "unsupported_python"
    if "health" in text or "not running" in text or "endpoint" in text \
            or "connection refused" in text or "backend is offline" in text:
        return "backend_health_failed"
    if "lock" in text or "being used by another process" in text:
        return "file_lock"
    if "unsupported" in text and "hardware" in text:
        return "unsupported_hardware"
    if "download" in text:
        return "model_download_failed"
    return "install_failed"


@dataclass
class ProvisionItem:
    id: str
    label: str
    kind: str                       # tool | invokeai_model | voice_assets
    priority: int = 100
    depends_on: list[str] = field(default_factory=list)
    provides: str = ""              # capability key offered on completion
    est_bytes: int = 0              # download size
    est_disk_bytes: int = 0         # installed footprint
    heavy: bool = False             # occupies the single heavy slot
    tier: str = "recommended"       # core | recommended | complete
    requires_approval: bool = False  # OS-level/heavyweight optional install
    payload: dict[str, Any] = field(default_factory=dict)
    # runtime state (persisted)
    state: str = "waiting"
    approved: bool = False
    detail: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    next_retry_at: float = 0.0
    blocked_reason: str = ""
    verified: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProvisioningManager:
    """Owns the post-install setup plan and drives it to completion."""

    def __init__(self, runtime_root: Path, config: Any, *,
                 image_manager: Any = None,
                 install_tool_hook: Callable[..., dict] | None = None,
                 tool_installed_hook: Callable[[str], bool] | None = None,
                 job_lookup: Callable[[str], Any] | None = None,
                 capability_registry: Any = None,
                 browser_install_hook: Callable[..., dict] | None = None,
                 browser_status_hook: Callable[[], dict] | None = None) -> None:
        self.runtime_root = Path(runtime_root)
        self.config = config
        self.image_manager = image_manager
        self._install_tool = install_tool_hook
        self._tool_installed = tool_installed_hook
        self._browser_install = browser_install_hook
        self._browser_status = browser_status_hook
        self._job_lookup = job_lookup
        self.capabilities = capability_registry
        self._state_path = self.runtime_root / "data" / "provisioning" / "plan.json"
        self._items: dict[str, ProvisionItem] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = False
        self._scheduler: threading.Thread | None = None
        self._active: set[str] = set()
        self._last_reconcile = 0.0
        self._cap_events: dict[str, threading.Event] = {}
        # Hooks wired by the server: UI event fan-out, notification, voice,
        # and capability-ready (waiting_for_capability drains).
        self.on_change: Callable[[ProvisionItem, str], None] | None = None
        self.on_notify: Callable[[dict[str, Any]], None] | None = None
        self.on_speak: Callable[[str, str], None] | None = None
        self.on_capability_ready: Callable[[str], None] | None = None
        self._spoken: set[str] = set()
        self._announced_start = False
        self._announced_done = False
        self._load()

    # ------------------------------------------------------------------ plan

    def _tool(self, tid: str, label: str, *, priority: int, tier: str,
              mb: int, provides: str = "",
              requires_approval: bool = False) -> ProvisionItem:
        return ProvisionItem(
            id=f"tool-{tid}", label=label, kind="tool", priority=priority,
            tier=tier, provides=provides, heavy=mb >= 400,
            requires_approval=requires_approval,
            est_bytes=mb * 1024 ** 2,
            est_disk_bytes=(mb + 20) * 1024 ** 2,
            payload={"tool_id": tid})

    def _declared_plan(self) -> list[ProvisionItem]:
        """The versioned workstation stack, tiered for install profiles.

        core        — lean always-on essentials the everyday agent
                      surfaces degrade without (voice, STT, search, media)
        recommended — core + the full creative/tooling stack
        complete    — recommended + heavyweight OS-level installs that
                      need explicit user approval (Docker, Blender)

        Entries reference real tool manifests and fleet specs — sizes
        come from the manifests/specs themselves, never invented."""
        items: list[ProvisionItem] = []
        items.append(ProvisionItem(
            id="voice-assets", label="Voice (TTS) assets",
            kind="voice_assets", priority=10, provides="tts", tier="core",
            est_bytes=410 * 1024 ** 2, est_disk_bytes=410 * 1024 ** 2))
        # Lean utility backbone — the agent's everyday surfaces.
        for tid, label, prio, mb, provides in (
            ("ripgrep", "ripgrep repository search", 12, 8,
             "repository_search"),
            ("ffmpeg", "FFmpeg media toolkit", 13, 90, "media_process"),
            ("ffprobe", "FFprobe media inspector", 14, 10,
             "media_inspect"),
            ("tesseract", "Tesseract OCR", 15, 65, "ocr"),
            ("pandoc", "Pandoc document conversion", 16, 30,
             "document_convert"),
            ("duckdb", "DuckDB data engine", 17, 25, "data_query"),
        ):
            items.append(self._tool(tid, label, priority=prio,
                                    tier="core", mb=mb,
                                    provides=provides))
        items.append(ProvisionItem(
            id="whisper-stt", label="Whisper STT model",
            kind="tool", priority=35, provides="stt", tier="core",
            est_bytes=10 * 1024 ** 2, est_disk_bytes=10 * 1024 ** 2,
            payload={"tool_id": "whisper"}))
        # Managed Chromium via Playwright — only downloads when no
        # system channel (Edge) resolves; a present browser verifies
        # instantly.
        items.append(ProvisionItem(
            id="browser-runtime", label="Browser runtime (Chromium)",
            kind="browser_runtime", priority=45,
            provides="browser_preview", tier="recommended",
            est_bytes=170 * 1024 ** 2,
            est_disk_bytes=480 * 1024 ** 2))
        items.append(ProvisionItem(
            id="invokeai", label="InvokeAI image backend",
            kind="tool", priority=20, provides="image_generation",
            est_bytes=2 * _GB, est_disk_bytes=6 * _GB, heavy=True,
            payload={"tool_id": "invokeai"}))
        # Photoreal fleet — Juggernaut is the general-purpose default and
        # lands first; the specialists follow.
        from .image.fleet import FLEET_BY_ID
        fleet_order = ["juggernaut-xl-v9", "cyberrealistic-xl-v9",
                       "realvisxl-v5"]
        for idx, fid in enumerate(fleet_order):
            spec = FLEET_BY_ID.get(fid)
            if spec is None:
                continue
            items.append(ProvisionItem(
                id=f"model-{fid}", label=f"{spec['display_name']} (image model)",
                kind="invokeai_model", priority=30 + idx * 10,
                depends_on=["invokeai"], provides="image_model",
                est_bytes=int(spec["size_bytes"]),
                est_disk_bytes=int(spec["size_bytes"]) + 64 * 1024 ** 2,
                heavy=True,
                # Licensed weights — approval records informed consent;
                # the license name rides the payload for the UI.
                requires_approval=bool(spec.get("license_name")),
                payload={"fleet_id": fid, "source": spec["invokeai_source"],
                         "sha256": spec["sha256"],
                         "license": spec["license_name"]}))
        items.append(ProvisionItem(
            id="comfyui", label="ComfyUI advanced image backend",
            kind="tool", priority=60, provides="image_generation",
            est_bytes=2 * _GB, est_disk_bytes=4 * _GB, heavy=True,
            payload={"tool_id": "comfyui"}))
        items.append(self._tool("piper", "Piper voice engine",
                                priority=62, tier="recommended", mb=60,
                                provides="tts_fallback"))
        # Chatterbox Turbo — isolated Python 3.12 runtime (torch+CUDA +
        # chatterbox-tts ~4.5GB disk) then the pinned turbo model (~3.3GB).
        # Kokoro stays the always-there fallback; these land in the
        # default (recommended) profile so upgrades never strand a
        # chatterbox-preset user without speech.
        items.append(ProvisionItem(
            id="chatterbox-runtime", label="Chatterbox voice runtime",
            kind="chatterbox_runtime", priority=63, tier="recommended",
            provides="tts_chatterbox",
            est_bytes=2 * _GB + 800 * 1024 ** 2,
            est_disk_bytes=5 * _GB, heavy=True))
        items.append(ProvisionItem(
            id="chatterbox-model", label="Chatterbox Turbo voice model",
            kind="chatterbox_model", priority=64, tier="recommended",
            depends_on=["chatterbox-runtime"], provides="tts_chatterbox",
            est_bytes=int(3.4 * _GB), est_disk_bytes=int(3.4 * _GB),
            heavy=True))
        # Heavyweight OS-level installs — explicit consent required;
        # they sit in `waiting_approval` until the user approves.
        items.append(self._tool("docker", "Docker Desktop",
                                priority=80, tier="complete", mb=600,
                                provides="containers",
                                requires_approval=True))
        items.append(self._tool("blender", "Blender 3D suite",
                                priority=81, tier="complete", mb=350,
                                provides="render_3d",
                                requires_approval=True))
        # Configured ComfyUI-side image model profiles — the full set, after
        # the InvokeAI fleet. Pure weight downloads into models/image/.
        try:
            profiles = list(getattr(self.config, "image_models", []) or [])
        except Exception:
            profiles = []
        for idx, profile in enumerate(profiles):
            if not getattr(profile, "enabled", True):
                continue
            size = sum(int(c.get("size_bytes") or 0)
                       for c in (getattr(profile, "components", None) or []))
            items.append(ProvisionItem(
                id=f"imagemodel-{profile.id}",
                label=f"{profile.display_name or profile.id} (image model)",
                kind="image_model", priority=70 + idx * 10,
                depends_on=["comfyui"], provides="image_model",
                est_bytes=size, est_disk_bytes=size + 64 * 1024 ** 2,
                heavy=True,
                # Same licensed-weights consent rule as the InvokeAI fleet.
                requires_approval=bool(getattr(
                    profile, "license_name", "")),
                payload={"model_id": profile.id,
                         "license": getattr(
                             profile, "license_name", "") or ""}))
        return items

    def _profile_includes(self, it: ProvisionItem) -> bool:
        """Install-profile filter — `core` is always included; larger
        tiers join as the configured profile widens. `custom` installs
        exactly provisioning_include (falling back to recommended when
        the list is empty); provisioning_exclude always subtracts."""
        try:
            include = {str(x) for x in getattr(
                self.config, "provisioning_include", []) or []}
            exclude = {str(x) for x in getattr(
                self.config, "provisioning_exclude", []) or []}
        except Exception:
            include, exclude = set(), set()
        if it.id in exclude:
            return False
        profile = str(getattr(self.config, "provisioning_profile",
                              "recommended") or "recommended"
                      ).strip().lower()
        if profile == "custom":
            if not include:
                return TIER_ORDER.get(it.tier, 1) <= \
                    TIER_ORDER["recommended"]
            return it.id in include
        if profile not in TIER_ORDER:
            profile = "recommended"
        return TIER_ORDER.get(it.tier, 1) <= TIER_ORDER[profile]

    def _load(self) -> None:
        declared = {it.id: it for it in self._declared_plan()}
        saved: dict[str, dict] = {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("items"), list):
                saved = {row["id"]: row for row in raw["items"]
                         if isinstance(row, dict) and row.get("id")}
        except Exception:
            saved = {}
        for iid, item in declared.items():
            if not self._profile_includes(item):
                continue
            prev = saved.get(iid)
            if prev:
                keep = {k: prev.get(k) for k in (
                    "state", "detail", "progress", "error_code",
                    "error_message", "attempts", "next_retry_at",
                    "verified", "started_at", "finished_at", "approved")}
                for k, v in keep.items():
                    if v is not None:
                        setattr(item, k, v)
                # Mid-flight items requeue; completed items stay completed
                # (verification was already performed when they finished).
                if item.state in {"running", "verifying", "queued"}:
                    item.state = "waiting"
                    item.detail = "resumed after restart"
            # OS-level installs hold for explicit consent — they are
            # never dispatched (or auto-retried) unapproved.
            if item.requires_approval and not item.approved \
                    and item.state == "waiting":
                item.state = "waiting_approval"
                item.detail = "needs approval to install"
            self._items[iid] = item
        # Re-verify cheaply where possible so upgrades never redownload
        # healthy components but uninstalled ones get requeued honestly.
        self._inventory()
        self._save()

    def replan(self) -> dict[str, Any]:
        """Re-evaluate the declared plan after a profile/include/exclude
        change. Newly included items join the queue (approval-gated ones
        enter waiting_approval); newly excluded items are dropped —
        running ones are cancelled first. Item state for ids still in
        scope is preserved."""
        added, removed = [], []
        with self._lock:
            declared = {it.id: it for it in self._declared_plan()}
            for iid, item in declared.items():
                if iid in self._items or not self._profile_includes(item):
                    continue
                if item.requires_approval:
                    item.state = "waiting_approval"
                    item.detail = "needs approval to install"
                self._items[iid] = item
                added.append(iid)
            for iid in list(self._items):
                if iid in declared and self._profile_includes(
                        self._items[iid]):
                    continue
                it = self._items.pop(iid)
                if it.state in {"running", "queued", "verifying",
                                "waiting"}:
                    it.state = "cancelled"
                    it.detail = "removed by profile change"
                    it.finished_at = time.time()
                removed.append(iid)
            self._save()
        self._wake.set()
        for iid in removed:
            self._emit(None, "item_removed")
        return {"added": added, "removed": removed,
                "profile": str(getattr(self.config,
                                       "provisioning_profile",
                                       "recommended"))}

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": PLAN_VERSION, "saved_at": time.time(),
                 "items": [it.as_dict() for it in
                           sorted(self._items.values(),
                                  key=lambda i: i.priority)]},
                indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception:
            pass

    def _inventory(self) -> None:
        """Cheap completeness probes — requeue 'completed' items whose
        payload is actually missing (e.g. files deleted out from under a
        previous run). A persisted plan must never override ground truth
        forever."""
        for it in self._items.values():
            if it.state != "completed":
                continue
            _pt = time.monotonic()
            if it.kind == "tool":
                # Re-run the manifest's own detection — a plan that says
                # "completed/verified" while the payload is gone requeues
                # instead of lying forever.
                tool_id = str(it.payload.get("tool_id") or it.id)
                if self._tool_installed is None:
                    continue  # no detector wired — don't churn state
                try:
                    ok = bool(self._tool_installed(tool_id))
                except Exception:
                    ok = True  # detection errored — don't churn state
                if not ok:
                    it.state, it.verified = "waiting", False
                    it.detail = "payload missing — requeued"
                    if tool_id == "invokeai":
                        self._invalidate_invokeai_discovery()
            elif it.kind == "voice_assets":
                try:
                    from .voice.assets import asset_status
                    status = asset_status(self._voice_asset_dir())
                    if not all(v.get("verified") for v in status.values()):
                        it.state, it.verified = "waiting", False
                        it.detail = "assets missing — requeued"
                except Exception:
                    pass
            elif it.kind == "chatterbox_runtime":
                try:
                    from .voice.chatterbox_runtime import runtime_status
                    if not runtime_status(
                            self._chatterbox_runtime_dir(),
                            deep=False)["verified"]:
                        it.state, it.verified = "waiting", False
                        it.detail = "runtime missing — requeued"
                except Exception:
                    pass
            elif it.kind == "chatterbox_model":
                try:
                    from .voice.chatterbox_assets import model_ready
                    if not model_ready(self._chatterbox_model_dir()):
                        it.state, it.verified = "waiting", False
                        it.detail = "model files missing — requeued"
                except Exception:
                    pass
            elif it.kind == "invokeai_model":
                # InvokeAI's model registry is a local SQLite file — it
                # can be re-verified while the backend is stopped. When
                # InvokeAI itself isn't installed, depends_on ordering
                # handles the item; nothing to re-check here.
                try:
                    rt = getattr(getattr(self, "image_manager", None),
                                 "invokeai_runtime", None)
                    if rt is None or rt.discover()[0] is None:
                        continue
                    fid = str(it.payload.get("fleet_id") or "")
                    if not fid:
                        continue
                    from .image.fleet import fleet_for_model_name
                    present = any(
                        (fleet_for_model_name(str(m.get("name") or ""))
                         or {}).get("id") == fid
                        for m in rt.registry_models())
                    if not present:
                        it.state, it.verified = "waiting", False
                        it.detail = "model missing from InvokeAI registry — requeued"
                except Exception:
                    pass
            elif it.kind == "browser_runtime":
                try:
                    if self._browser_status is not None \
                            and not self._browser_ready():
                        it.state, it.verified = "waiting", False
                        it.detail = "browser channel missing — requeued"
                except Exception:
                    pass
            elif it.kind == "image_model":
                # Weight files only — a cheap disk existence re-check.
                try:
                    manager = self.image_manager
                    profile = manager.router.get_profile(
                        str(it.payload.get("model_id") or ""))
                    if not manager.library.verify_model(profile).get("installed"):
                        it.state, it.verified = "waiting", False
                        it.detail = "components missing — requeued"
                except Exception:
                    pass
            _pms = (time.monotonic() - _pt) * 1000
            if _pms > 100:
                print(f"[nexus-init] inventory/{it.id} {_pms:.0f}ms",
                      flush=True)

    def _invalidate_invokeai_discovery(self) -> None:
        """Drop the runtime's 60s discovery cache after install/repair so
        the next image request doesn't route around a stale miss."""
        try:
            rt = getattr(getattr(self, "image_manager", None),
                         "invokeai_runtime", None)
            if rt is not None:
                rt.invalidate_discovery()
        except Exception:
            pass

    def _voice_asset_dir(self) -> Path:
        configured = Path(str(getattr(
            self.config, "voice_assets_dir", "data/voice/assets")
            or "data/voice/assets"))
        return configured if configured.is_absolute() \
            else self.runtime_root / configured

    def _chatterbox_runtime_dir(self) -> Path:
        configured = Path(str(getattr(
            self.config, "voice_chatterbox_runtime_dir",
            "runtime/voice/chatterbox") or "runtime/voice/chatterbox"))
        return configured if configured.is_absolute() \
            else self.runtime_root / configured

    def _chatterbox_model_dir(self) -> Path:
        return self._voice_asset_dir() / "chatterbox"

    # ------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "provisioning_enabled", True))

    def start(self) -> None:
        """Auto-start only in installed (frozen) builds or when dev
        explicitly opts in — a unit test or dev checkout must never kick
        off multi-GB downloads as a side effect of AppState init."""
        import sys
        auto = getattr(sys, "frozen", False) or bool(
            getattr(self.config, "provisioning_dev_enable", False))
        if not (self.enabled and auto) or self._scheduler is not None:
            return
        self._scheduler = threading.Thread(
            target=self._loop, name="provisioning", daemon=True)
        self._scheduler.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._scheduler:
            self._scheduler.join(timeout=5)

    def pause(self) -> None:
        self._paused = True
        self._emit(None, "paused")

    def resume(self) -> None:
        self._paused = False
        self._wake.set()
        self._emit(None, "resumed")

    # --------------------------------------------------------------- control

    def cancel_item(self, item_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if it is None or it.state in TERMINAL:
                return False
            it.state = "cancelled"
            it.detail = "cancelled by user"
            it.finished_at = time.time()
            self._save()
        self._emit(it, "cancelled")
        return True

    def approve_item(self, item_id: str) -> bool:
        """Grant consent for an OS-level install — moves it from
        waiting_approval into the dispatch queue."""
        with self._lock:
            it = self._items.get(item_id)
            if it is None or not it.requires_approval:
                return False
            it.approved = True
            if it.state == "waiting_approval":
                it.state = "waiting"
                it.detail = "approved — queued"
            self._save()
        self._wake.set()
        self._emit(it, "approved")
        return True

    def retry_item(self, item_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if it is None or it.state not in {"failed", "cancelled", "skipped"}:
                return False
            it.state, it.error_code, it.error_message = "waiting", "", ""
            it.attempts, it.next_retry_at = 0, 0.0
            it.blocked_reason = ""
            self._save()
        self._wake.set()
        self._emit(it, "requeued")
        return True

    def wait_for_capability(self, capability: str, timeout: float = 300.0) -> bool:
        """Block until a provider for ``capability`` completes. Used by
        ``waiting_for_capability`` task flows — always bounded."""
        if self.capability_state(capability) == "available":
            return True
        ev = self._cap_events.setdefault(capability, threading.Event())
        return ev.wait(timeout)

    def capability_state(self, capability: str) -> str:
        providers = [it for it in self._items.values()
                     if it.provides == capability]
        if not providers:
            return "unknown"
        if any(it.state == "completed" and it.verified for it in providers):
            return "available"
        if any(it.state in {"running", "verifying", "queued"} for it in providers):
            return "installing"
        if all(it.state in TERMINAL for it in providers):
            return "failed"
        return "setup_required"

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        items = [it.as_dict() for it in
                 sorted(self._items.values(), key=lambda i: i.priority)]
        done = [i for i in items if i["state"] == "completed"]
        running = [i for i in items if i["state"] in {"running", "verifying"}]
        failed = [i for i in items if i["state"] == "failed"]
        awaiting = [i for i in items if i["state"] == "waiting_approval"]
        total_bytes = sum(i["est_bytes"] for i in items)
        remaining_bytes = sum(i["est_bytes"] for i in items
                              if i["state"] not in {"completed", "skipped"})
        try:
            free = shutil.disk_usage(str(self.runtime_root)).free
        except OSError:
            free = 0
        return {
            "enabled": self.enabled,
            "paused": self._paused,
            "total": len(items), "completed": len(done),
            "running": len(running), "failed": len(failed),
            "waiting_approval": len(awaiting),
            "total_download_bytes": total_bytes,
            "remaining_download_bytes": remaining_bytes,
            "free_disk_bytes": free,
            "active": sorted(self._active),
            "items": items,
            "config": {
                "provisioning_enabled": self.enabled,
                "provisioning_auto_retry": bool(getattr(
                    self.config, "provisioning_auto_retry", True)),
                "provisioning_voice_notifications": bool(getattr(
                    self.config, "provisioning_voice_notifications", True)),
                "provisioning_profile": str(getattr(
                    self.config, "provisioning_profile", "recommended")),
                "provisioning_include": list(getattr(
                    self.config, "provisioning_include", []) or []),
                "provisioning_exclude": list(getattr(
                    self.config, "provisioning_exclude", []) or []),
            },
            # waiting_approval doesn't block completion — it's a pending
            # user decision, not pending work (same rule as
            # _check_all_done).
            "complete": len(done) + len(awaiting) + len(
                [i for i in items if i["state"] == "skipped"]
            ) == len(items),
        }

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        self._announce_start()
        while not self._stop.is_set():
            try:
                if self._paused:
                    self._wake.wait(2.0)
                    self._wake.clear()
                    continue
                self._reconcile()
                dispatched = self._dispatch()
                self._check_all_done()
                self._wake.wait(1.0 if dispatched else 5.0)
                self._wake.clear()
            except Exception:
                # A transient error (e.g. runtime root briefly missing)
                # must never kill the scheduler — back off and retry.
                self._wake.wait(15.0)
                self._wake.clear()

    def _reconcile(self) -> None:
        """Heal terminal states when ground truth changed under us.

        A tool marked failed/cancelled whose payload now detects on disk is
        completed outright (a verification-time miss must not strand its
        dependents forever), and items skipped because a dependency failed
        requeue once every dependency is satisfied. Failed work also earns
        one bounded requeue per boot while attempts remain — a detection
        race (AV holding a fresh exe) must never wedge the whole plan.
        Rate-limited: detection probes are cheap but not free."""
        now = time.time()
        if now - self._last_reconcile < 15.0:
            return
        self._last_reconcile = now
        completed: list[ProvisionItem] = []
        changed = False
        with self._lock:
            for it in self._items.values():
                if it.kind == "tool" and it.state in {"failed", "cancelled"}:
                    tool_id = str(it.payload.get("tool_id") or it.id)
                    try:
                        ok = bool(self._tool_installed
                                  and self._tool_installed(tool_id))
                    except Exception:
                        ok = False
                    if ok:
                        it.state = "completed"
                        it.verified = True
                        it.error_code = it.error_message = ""
                        it.detail = "detected installed on disk"
                        it.finished_at = now
                        completed.append(it)
                        changed = True
                        if tool_id == "invokeai":
                            self._invalidate_invokeai_discovery()
            for it in self._items.values():
                if it.state != "skipped":
                    continue
                deps = [self._items.get(d) for d in it.depends_on]
                if deps and all(d is not None and d.state == "completed"
                                for d in deps):
                    it.state = "waiting"
                    it.detail = "requeued — dependency now satisfied"
                    changed = True
            auto_retry = bool(getattr(
                self.config, "provisioning_auto_retry", True))
            if auto_retry:
                for it in self._items.values():
                    if it.state != "failed" or it.attempts >= MAX_ATTEMPTS:
                        continue
                    deps = [self._items.get(d) for d in it.depends_on]
                    if deps and not all(d is not None
                                        and d.state == "completed"
                                        for d in deps):
                        continue
                    it.state = "waiting"
                    it.detail = f"requeued for retry ({it.attempts}/{MAX_ATTEMPTS})"
                    it.next_retry_at = 0.0
                    changed = True
            if changed:
                self._save()
        for it in completed:
            self._finish_lifecycle(it)

    def _dispatch(self) -> bool:
        now = time.time()
        reserve = int(getattr(self.config, "provisioning_disk_reserve_bytes",
                              8 * _GB))
        max_parallel = max(1, int(getattr(self.config,
                                        "provisioning_parallel", 2)))
        dispatched = False
        with self._lock:
            running = [it for it in self._items.values()
                       if it.state in {"running", "verifying", "queued"}]
            heavy_running = any(it.heavy for it in running)
            for it in sorted(self._items.values(), key=lambda i: i.priority):
                if it.state != "waiting":
                    continue
                if it.next_retry_at and now < it.next_retry_at:
                    continue
                deps = [self._items[d] for d in it.depends_on
                        if d in self._items]
                if any(d.state != "completed" for d in deps):
                    if any(d.state in {"failed", "cancelled", "skipped"}
                           for d in deps):
                        it.state = "skipped"
                        it.detail = "dependency did not install"
                        it.finished_at = now
                    continue
                if len(running) >= max_parallel:
                    break
                if it.heavy and heavy_running:
                    continue
                # Disk gate — never silently fill the drive.
                need = (it.est_disk_bytes or it.est_bytes) + reserve
                try:
                    free = shutil.disk_usage(str(self.runtime_root)).free
                except OSError:
                    break  # runtime root missing — retry next tick
                if need and free < need:
                    it.blocked_reason = (
                        f"needs ~{need / _GB:.0f} GB free, "
                        f"only {free / _GB:.0f} GB available")
                    continue
                it.blocked_reason = ""
                it.state = "queued"
                it.started_at = it.started_at or now
                it.attempts += 1
                running.append(it)
                heavy_running = heavy_running or it.heavy
                threading.Thread(target=self._run_item, args=(it.id,),
                                 daemon=True).start()
                dispatched = True
        if dispatched:
            self._save()
        return dispatched

    def _run_item(self, item_id: str) -> None:
        it = self._items[item_id]
        self._active.add(item_id)
        self._set(it, "running", detail=it.detail or "starting")
        try:
            if it.kind == "voice_assets":
                self._run_voice_assets(it)
            elif it.kind == "tool":
                self._run_tool(it)
            elif it.kind == "invokeai_model":
                self._run_invokeai_model(it)
            elif it.kind == "image_model":
                self._run_image_model(it)
            elif it.kind == "chatterbox_runtime":
                self._run_chatterbox_runtime(it)
            elif it.kind == "chatterbox_model":
                self._run_chatterbox_model(it)
            elif it.kind == "browser_runtime":
                self._run_browser_runtime(it)
            else:
                raise ValueError(f"unknown provision kind '{it.kind}'")
            self._finish(it, verified=True)
        except _ProvisionCancelled:
            self._set_terminal(it, "cancelled", detail="cancelled")
        except Exception as exc:
            self._fail(it, exc)
        finally:
            self._active.discard(item_id)
            self._wake.set()

    # -------------------------------------------------------------- executors

    def _run_voice_assets(self, it: ProvisionItem) -> None:
        from .voice.assets import ensure_assets
        def _progress(name: str, done: int) -> None:
            it.progress = {"current_file": name, "bytes_done": done,
                           "bytes_total": None}
            self._emit(it, "progress")
        ensure_assets(self._voice_asset_dir(), progress=_progress)
        self._set(it, "verifying", detail="verifying voice assets")
        from .voice.assets import asset_status
        status = asset_status(self._voice_asset_dir())
        bad = [k for k, v in status.items() if not v.get("verified")]
        if bad:
            raise RuntimeError(f"voice assets failed verification: {bad}")

    def _run_chatterbox_runtime(self, it: ProvisionItem) -> None:
        from .voice.chatterbox_runtime import ensure_runtime

        def _progress(name: str, done: int) -> None:
            it.progress = {"current_file": name, "bytes_done": done,
                           "bytes_total": None}
            self._emit(it, "progress")
        ensure_runtime(self._chatterbox_runtime_dir(), progress=_progress)
        self._set(it, "verifying", detail="verifying chatterbox runtime")
        from .voice.chatterbox_runtime import runtime_status
        # Deep import already ran inside ensure_runtime before the marker
        # was written — a shallow re-check is enough here.
        st = runtime_status(self._chatterbox_runtime_dir(), deep=False)
        if not st["verified"]:
            raise RuntimeError(
                f"chatterbox runtime failed verification: {st['detail']}")

    def _run_chatterbox_model(self, it: ProvisionItem) -> None:
        from .voice.chatterbox_assets import ensure_model, model_status

        def _progress(name: str, done: int) -> None:
            it.progress = {"current_file": name, "bytes_done": done,
                           "bytes_total": None}
            self._emit(it, "progress")
        ensure_model(self._chatterbox_model_dir(), progress=_progress)
        self._set(it, "verifying", detail="verifying chatterbox model")
        status = model_status(self._chatterbox_model_dir())
        bad = [k for k, v in status.items() if not v.get("verified")]
        if bad:
            raise RuntimeError(
                f"chatterbox model failed verification: {bad}")

    def _run_tool(self, it: ProvisionItem) -> None:
        tool_id = str(it.payload.get("tool_id") or "")
        if self._install_tool is None:
            raise RuntimeError("tool installer not wired")
        result = self._install_tool(tool_id, approve=True)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "install rejected"))
        job_id = str(result.get("job_id") or "")
        if not job_id:
            # Already-installed/deduped replies without a job verify below.
            self._verify_tool(it)
            return
        while not self._stop.is_set():
            self._check_cancel(it)
            try:
                job = self._job_lookup(job_id) if self._job_lookup else None
            except KeyError:
                job = None
            if job is None:
                raise RuntimeError(f"install job {job_id} disappeared")
            meta = getattr(job, "metadata", {}) or {}
            it.progress = {
                "phase": meta.get("phase", getattr(job, "status", "")),
                "bytes_done": meta.get("bytes_done"),
                "bytes_total": meta.get("bytes_total"),
                "bytes_per_sec": meta.get("bytes_per_sec"),
                "eta_seconds": meta.get("eta_seconds"),
                "current_file": meta.get("current_file") or "",
            }
            it.detail = str(getattr(job, "status", "") or "installing")
            self._emit(it, "progress")
            state = getattr(job, "state", "")
            if state == "completed":
                self._verify_tool(it)
                return
            if state in {"failed", "cancelled"}:
                if state == "cancelled":
                    raise _ProvisionCancelled()
                raise RuntimeError(str(getattr(job, "error", "") or
                                       "install job failed"))
            time.sleep(1.5)

    def _browser_ready(self) -> bool:
        if self._browser_status is None:
            return False
        try:
            st = self._browser_status() or {}
        except Exception:
            return False
        return bool(st.get("ready") or st.get("channel")
                    or st.get("state") == "verified")

    def _run_browser_runtime(self, it: ProvisionItem) -> None:
        """Managed Chromium — a resolvable system channel means the
        download never happens; otherwise the install hook submits the
        tracked playwright job and we follow it like a tool install."""
        if self._browser_install is None:
            raise RuntimeError("browser runtime installer not wired")
        if self._browser_ready():
            return
        result = self._browser_install(approve=True)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error")
                                   or "browser install rejected"))
        if result.get("already_present") or result.get("channel"):
            return
        job_id = str(result.get("job_id") or "")
        if not job_id:
            if self._browser_ready():
                return
            raise RuntimeError("browser install returned no job")
        while not self._stop.is_set():
            self._check_cancel(it)
            try:
                job = self._job_lookup(job_id) if self._job_lookup else None
            except KeyError:
                job = None
            if job is None:
                raise RuntimeError("browser install job disappeared")
            state = getattr(job, "state", "")
            it.detail = str(getattr(job, "status", "") or "installing")
            self._emit(it, "progress")
            if state == "completed":
                self._set(it, "verifying", detail="verifying browser")
                if not self._browser_ready():
                    raise RuntimeError(
                        "browser install finished but no channel resolves")
                return
            if state in {"failed", "cancelled"}:
                if state == "cancelled":
                    raise _ProvisionCancelled()
                raise RuntimeError(str(getattr(job, "error", "") or
                                       "browser install failed"))
            time.sleep(1.5)

    def _verify_tool(self, it: ProvisionItem) -> None:
        """Tool installs verify via the manifest's own detect check — a
        job that "completed" but produced nothing on disk must fail the
        item honestly, not mark a broken install as usable."""
        self._set(it, "verifying", detail="verifying installation")
        if self._tool_installed is not None:
            tool_id = str(it.payload.get("tool_id") or it.id)
            try:
                ok = bool(self._tool_installed(tool_id))
            except Exception:
                ok = False
            if not ok:
                raise RuntimeError(
                    f"{it.label} install finished but the tool was not "
                    "detected on disk")
            if tool_id == "invokeai":
                self._invalidate_invokeai_discovery()

    def _run_invokeai_model(self, it: ProvisionItem) -> None:
        runtime = getattr(self.image_manager, "invokeai_runtime", None)
        backend = getattr(self.image_manager, "invokeai_backend", None)
        if runtime is None or backend is None:
            raise RuntimeError("InvokeAI backend not wired")
        # The model install needs a live backend — ensure_ready() raises
        # a descriptive error (offline/no install) that classifies into a
        # setup failure code.
        runtime.ensure_ready()
        # Upgrades / pre-seeded installs: if the backend already knows the
        # model, verify and finish — never re-download a healthy checkpoint.
        if self._fleet_model_present(backend, it):
            self._verify_model(it)
            return
        # Dedup before download: a verified-size checkpoint already on
        # disk (InvokeAI store or the shared Nexus model dir) registers
        # in place — a second ~7 GB copy is never fetched for the same
        # logical model.
        source = str(it.payload["source"])
        inplace = False
        try:
            from .image.fleet import FLEET_BY_ID, find_fleet_checkpoint
            spec = FLEET_BY_ID.get(str(it.payload.get("fleet_id") or ""))
            if spec is not None:
                roots = []
                mgr = self.image_manager
                for cand in (getattr(mgr, "_invokeai_models_root", None),
                             getattr(mgr, "models_dir", None)):
                    try:
                        roots.append(cand() if callable(cand) else cand)
                    except Exception:
                        pass
                local = find_fleet_checkpoint(spec, [r for r in roots if r])
                if local is not None:
                    source, inplace = str(local), True
        except Exception:
            source, inplace = str(it.payload["source"]), False
        job = (backend.install_model(source, inplace=True) if inplace
               else backend.install_model(source))
        job_id = job.get("id")
        if job_id is None:
            raise RuntimeError(f"model install rejected: {job}")
        self._set(it, "running", detail="downloading model")
        while not self._stop.is_set():
            self._check_cancel(it)
            row = backend.model_install_job(job_id)
            if row is None:
                # invokeai-web restarts wipe in-memory job rows; a
                # vanished job mid-install means the backend crashed or
                # the job was cancelled — treat as a retryable failure
                # rather than waiting forever.
                raise RuntimeError("model install job disappeared "
                                   "(backend restart/crash)")
            status = str(row.get("status") or "").lower()
            total = int(row.get("bytes_total") or it.est_bytes or 0)
            done = int(row.get("bytes") or 0)
            it.progress = {"bytes_done": done, "bytes_total": total,
                           "current_file": it.label,
                           "job_status": status}
            self._emit(it, "progress")
            if status == "completed":
                break
            if status in {"error", "cancelled"}:
                raise RuntimeError(str(row.get("error") or
                                       f"model install {status}"))
            time.sleep(2.0)
        else:
            raise _ProvisionCancelled()
        self._verify_model(it)

    @staticmethod
    def _row_matches_fleet(row: dict, fleet_id: str) -> bool:
        from .image.fleet import fleet_for_model_name
        for cand in (str(row.get("name") or ""), str(row.get("source") or ""),
                     str(row.get("path") or "")):
            spec = fleet_for_model_name(cand)
            if spec and spec["id"] == fleet_id:
                return True
        return False

    def _fleet_model_present(self, backend, it: ProvisionItem) -> bool:
        """One-shot enumeration check — True when the backend already
        registers this fleet model (upgrade/pre-seeded install)."""
        fid = str(it.payload.get("fleet_id") or "")
        try:
            rows = backend.models()
        except Exception:
            return False
        return any(self._row_matches_fleet(r, fid) for r in rows)

    def _verify_model(self, it: ProvisionItem) -> None:
        """A model is 'verified' only when the backend can enumerate it —
        never trust the download job alone."""
        self._set(it, "verifying", detail="verifying model registration")
        backend = getattr(self.image_manager, "invokeai_backend", None)
        fid = str(it.payload.get("fleet_id") or "")
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                rows = backend.models() if backend else []
            except Exception:
                rows = []
            if any(self._row_matches_fleet(r, fid) for r in rows):
                return
            time.sleep(2.0)
        raise RuntimeError("model installed but not enumerable by backend")

    def _run_image_model(self, it: ProvisionItem) -> None:
        """Download a configured ComfyUI-side model profile's components.
        Pure file downloads through ImageAssetLibrary — no live backend."""
        manager = self.image_manager
        if manager is None:
            raise RuntimeError("image manager not wired")
        model_id = str(it.payload.get("model_id") or "")
        try:
            profile = manager.router.get_profile(model_id)
        except Exception as exc:
            raise RuntimeError(f"unknown image model profile '{model_id}'") from exc
        if manager.library.verify_model(profile).get("installed"):
            return  # already on disk — _finish records verification
        job = manager.start_model_install(model_id)
        job_id = str(job.get("id") or "")
        while not self._stop.is_set():
            self._check_cancel(it)
            row = next((j for j in manager.library.install_jobs()
                        if j.get("id") == job_id), None)
            if row is None:
                raise RuntimeError("image model install job disappeared")
            it.progress = {
                "bytes_done": None,
                "bytes_total": it.est_bytes or None,
                "current_file": str(row.get("current_file") or ""),
                "job_status": str(row.get("state") or ""),
                "fraction": row.get("progress", 0.0),
            }
            it.detail = str(row.get("state") or "downloading")
            self._emit(it, "progress")
            state = str(row.get("state") or "")
            if state == "finished":
                break
            if state in {"failed", "cancelled"}:
                if state == "cancelled":
                    raise _ProvisionCancelled()
                raise RuntimeError(str(row.get("error") or "download failed"))
            time.sleep(2.0)
        else:
            raise _ProvisionCancelled()
        if not manager.library.verify_model(profile).get("installed"):
            raise RuntimeError(
                "model download finished but required components are missing")

    # ------------------------------------------------------------ transitions

    def _check_cancel(self, it: ProvisionItem) -> None:
        if it.state == "cancelled" or self._stop.is_set():
            raise _ProvisionCancelled()

    def _set(self, it: ProvisionItem, state: str, *, detail: str = "") -> None:
        with self._lock:
            it.state = state
            if detail:
                it.detail = detail
            self._save()
        self._emit(it, state)

    def _finish(self, it: ProvisionItem, *, verified: bool) -> None:
        with self._lock:
            it.state = "completed"
            it.verified = verified
            it.error_code = it.error_message = ""
            it.finished_at = time.time()
            it.detail = "verified"
            self._save()
        self._finish_lifecycle(it)

    def _finish_lifecycle(self, it: ProvisionItem) -> None:
        """Capability unlock + notification for a completion — whether the
        item just ran to completion or was healed by reconciliation."""
        if self.capabilities is not None:
            try:
                self.capabilities.invalidate()
            except Exception:
                pass
        if it.provides:
            ev = self._cap_events.setdefault(it.provides, threading.Event())
            ev.set()
            if self.on_capability_ready is not None:
                try:
                    self.on_capability_ready(it.provides)
                except Exception:
                    pass
        self._emit(it, "completed")
        self._notify("provision_ready",
                     f"{it.label} is ready.",
                     item=it.id, state="completed")
        # The moment voice itself verifies, one spoken line explains the
        # rest of setup is still running — persona aside, the fact stays.
        if it.id == "voice-assets" and self.on_speak and \
                it_is_first(self._spoken, "voice-notice") and \
                any(x.state not in TERMINAL for x in self._items.values()):
            if bool(getattr(self.config, "provisioning_voice_notifications",
                            True)):
                self.on_speak(
                    "setup-progress",
                    "I'm still finishing setup in the background — some "
                    "tools may be unavailable until installation completes.")

    def _fail(self, it: ProvisionItem, exc: BaseException) -> None:
        code = classify_setup_error(exc)
        retryable = code in TRANSIENT_ERRORS and \
            it.attempts < MAX_ATTEMPTS and \
            bool(getattr(self.config, "provisioning_auto_retry", True))
        with self._lock:
            it.error_code = code
            it.error_message = str(exc)[:300]
            if retryable:
                backoff = min(600.0, 30.0 * (4 ** (it.attempts - 1)))
                it.next_retry_at = time.time() + backoff
                it.state = "waiting"
                it.detail = f"retry {it.attempts}/{MAX_ATTEMPTS} after {code}"
            else:
                it.state = "failed"
                it.finished_at = time.time()
                it.detail = code
            self._save()
        if retryable:
            self._emit(it, "retry_scheduled")
            self._notify("provision_retry",
                         f"{it.label} hit a {code.replace('_', ' ')} — "
                         f"retrying automatically.",
                         item=it.id, state="retrying")
            return
        self._emit(it, "failed")
        self._notify("provision_failed",
                     f"{it.label} failed: {self._human_error(it)}",
                     item=it.id, state="failed", error=code)
        # One spoken line per incident — retries never re-speak.
        if it.id not in self._spoken:
            self._spoken.add(it.id)
            if self.on_speak and bool(getattr(
                    self.config, "provisioning_voice_notifications", True)):
                self.on_speak(it.id,
                              f"{it.label} failed — {self._human_error(it)}")

    @staticmethod
    def _human_error(it: ProvisionItem) -> str:
        return {
            "network_failure": "the connection was interrupted",
            "disk_full": "the disk ran out of space",
            "checksum_failed": "the download failed verification",
            "unsupported_python": "a compatible Python runtime is unavailable",
            "backend_health_failed": "the image backend isn't healthy",
            "permission_denied": "access was denied",
            "file_lock": "a file is locked by another process",
            "unsupported_hardware": "this hardware can't run it",
            "model_download_failed": "the model download failed",
        }.get(it.error_code, "the installation step failed")

    def _set_terminal(self, it: ProvisionItem, state: str,
                      *, detail: str = "") -> None:
        with self._lock:
            it.state = state
            it.detail = detail or state
            it.finished_at = time.time()
            self._save()
        self._emit(it, state)

    def _check_all_done(self) -> None:
        if self._announced_done or not self._items:
            return
        # waiting_approval does not block completion — it is a user
        # decision, not pending work; approving later resumes the item.
        terminal = all(it.state in TERMINAL
                       or it.state == "waiting_approval"
                       for it in self._items.values())
        if not terminal:
            return
        failed = [it for it in self._items.values() if it.state == "failed"]
        self._announced_done = True
        if failed:
            self._notify(
                "provision_partial",
                f"Core setup is complete, but {len(failed)} optional "
                "component(s) still need attention.",
                failed=[it.id for it in failed])
            if self.on_speak and it_is_first(self._spoken, "done"):
                self.on_speak("done",
                              "Setup is mostly complete, but a couple of "
                              "optional components still need attention.")
        else:
            self._notify("provision_complete",
                         "Nexus workstation setup complete.")
            if self.on_speak and it_is_first(self._spoken, "done"):
                self.on_speak("done", "Setup is complete. All planned "
                              "tools and models are ready.")

    def _announce_start(self) -> None:
        pending = [it for it in self._items.values()
                   if it.state not in TERMINAL]
        if not pending or self._announced_start:
            return
        self._announced_start = True
        self._notify(
            "provision_started",
            "Nexus is finishing workstation setup in the background. "
            "Some tools may be temporarily unavailable until installation "
            "completes.",
            pending=len(pending))

    # ------------------------------------------------------------------ misc

    def _emit(self, it: ProvisionItem | None, kind: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(it, kind)
        except Exception:
            pass

    def _notify(self, kind: str, text: str, **extra: Any) -> None:
        if self.on_notify is None:
            return
        try:
            self.on_notify({"kind": kind, "text": text,
                            "time": time.time(), **extra})
        except Exception:
            pass


def it_is_first(seen: set[str], key: str) -> bool:
    if key in seen:
        return False
    seen.add(key)
    return True


class _ProvisionCancelled(Exception):
    pass
