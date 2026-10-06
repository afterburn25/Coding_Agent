"""NexusHelpService — the conversational control plane.

Interprets a user message against the Feature Catalog, Settings
Registry, Action Registry, Page Registry, and the live Capability
Registry, then returns a structured Resolution the orchestrator renders
through the persona pipeline.

Two kinds of outcomes:

    answer   — text + optional actions/links/controls (read-only or a
               proposed mutation awaiting confirmation)
    execute  — a mutation already run through ActionRegistry.execute,
               verified, with an undo handle retained

Follow-ups ("do it", "undo that", "open it") resolve against the last
proposed/executed action kept in ``_context``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .catalog import FeatureCatalog, FeatureSpec
from .pages import PageRegistry, PageSpec
from .settings import SettingsRegistry, SettingSpec
from .actions import ActionRegistry, ActionSpec, ActionResult

_WORD = re.compile(r"[a-z0-9'\-]+")
_NUM = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass(slots=True)
class Resolution:
    """What a message resolved to — the orchestrator renders this."""
    kind: str                # answer|execute|confirm|navigate|decline
    intent: str = ""         # help|status|where|control|list|diagnose
    text: str = ""           # factual draft — persona rewrites wording
    feature: FeatureSpec | None = None
    setting: SettingSpec | None = None
    action: ActionSpec | None = None
    page: PageSpec | None = None
    page_section: str = ""
    result: ActionResult | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    undo: dict[str, Any] | None = None
    truth: dict[str, Any] = field(default_factory=dict)  # provenance


class SelfKnowledgeService:
    """Composes the registries against live runtime state.

    env callables (injected by AppState — the same pattern as
    capabilities.py):
        capability(id, force=False) -> report | None
        probe(name) -> dict          custom subsystem state
        permitted(key) -> verdict    permission check
        get(key) / set(key, value)   settings access
        choices(key) -> list         dynamic allowed values
        + action bodies referenced by ActionSpec.run
    """

    def __init__(self, env: dict[str, Callable] | None = None,
                 catalog: FeatureCatalog | None = None,
                 pages: PageRegistry | None = None,
                 settings: SettingsRegistry | None = None,
                 actions: ActionRegistry | None = None) -> None:
        self._env = dict(env or {})
        self.catalog = catalog or FeatureCatalog()
        self.pages = pages or PageRegistry()
        self._env.setdefault("get", None)
        self._env.setdefault("set", None)
        self._env.setdefault("choices", None)
        self.settings = settings or SettingsRegistry(env=self._env)
        # Action bodies mutate through the SettingsRegistry — inject it
        # before the ActionRegistry copies the env.
        self._env["settings"] = self.settings
        self.actions = actions or ActionRegistry(env=self._env)
        # Follow-up context — survives between messages on this service.
        self._context: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Interpretation
    # ------------------------------------------------------------------

    def respond(self, text: str, execute: bool = True
                ) -> Resolution | None:
        """Resolve a user message, or return None when it isn't a
        self-knowledge/control request. ``execute=False`` is a dry run —
        the resolution describes what would happen without mutating
        (the no-model gate uses this so 'turn voice off' doesn't toggle
        twice)."""
        self._execute = execute
        try:
            t = " ".join(_WORD.findall(str(text or "").lower()))
            if not t:
                return None
            for probe in (self._followup, self._diagnostics,
                          self._control, self._where,
                          self._status_or_explain):
                res = probe(t)
                if res is not None:
                    return res
            return None
        finally:
            self._execute = True

    def would_answer(self, text: str) -> bool:
        """Gate probe — would this turn resolve? Never mutates."""
        try:
            return self.respond(text, execute=False) is not None
        except Exception:
            return False

    # -- follow-ups -----------------------------------------------------

    def _followup(self, t: str) -> Resolution | None:
        proposed = self._context.get("proposed")
        last = self._context.get("last_executed")
        if re.search(r"\bdo it\b|\bgo ahead\b|\byes\b|\bgo for it\b|"
                     r"\bplease do\b|\bdo that\b|\bfix it\b|\bfix "
                     r"that\b|\binstall it\b|\binstall that\b|\bset it "
                     r"up\b|\bretry\b|\btry again\b", t) and proposed:
            if proposed.get("action") == "_set":
                spec_s = self.settings.get(proposed["params"]["key"])
                if spec_s is None:
                    return None
                return self._apply_setting(
                    spec_s, proposed["params"].get("value"),
                    confirmed=True)
            spec = self.actions.get(proposed["action"])
            if spec is None:
                return None
            return self._run(spec, dict(proposed.get("params") or {}),
                             confirmed=True)
        if re.search(r"\bundo\b|\bundo that\b|\bput it back\b|"
                     r"\brevert\b|\bchange it back\b|"
                     r"\b(turn|switch|set|put)\b.{0,15}\bback\b.{0,5}"
                     r"\bon\b|\bback to (normal|before|how it was)\b|"
                     r"\bturn it back\b", t) and last:
            undo = last.get("undo")
            if not undo:
                return Resolution("answer", "undo",
                    text="The last change can't be undone from chat.")
            if undo.get("action") == "_set":
                spec_s = self.settings.get(undo["params"]["key"])
                if spec_s is None:
                    return None
                return self._apply_setting(
                    spec_s, undo["params"].get("value"),
                    confirmed=True, undoing=True)
            spec = self.actions.get(undo.get("action", ""))
            if spec is None:
                return Resolution("answer", "undo",
                    text="The last change can't be undone from chat.")
            return self._run(spec, dict(undo.get("params") or {}),
                             confirmed=True, undoing=True)
        if re.search(r"\bopen it\b|\bopen that\b|\btake me\b", t):
            route = (proposed or {}).get("route") or \
                    (last or {}).get("route")
            if route:
                return self._navigate(route)
        return None

    # -- diagnostics -----------------------------------------------------

    def _diagnostics(self, t: str) -> Resolution | None:
        if re.search(r"\bwhat('s| is| are| isn't| aren't).{0,30}"
                     r"(broken|wrong|not working|degraded|failing)\b|"
                     r"\bwhat (isn't|isnt|aren't|arent) working\b|"
                     r"\bwhat's broken\b|\bany problems\b|"
                     r"\bself diagnos", t):
            rows = self._health_rows()
            broken = [r for r in rows
                      if r["state"] in ("broken", "unavailable", "degraded")]
            if not broken:
                return Resolution("answer", "diagnose",
                    text="Everything probed is healthy right now.",
                    truth={"kind": "health"})
            lines = "; ".join(f"{r['name']}: {r['state']}"
                              + (f" — {r['detail']}" if r.get("detail")
                                 else "")
                              for r in broken[:8])
            return Resolution(
                "answer", "diagnose",
                text=(f"{len(broken)} subsystem"
                      f"{'s are' if len(broken) > 1 else ' is'} "
                      f"degraded: {lines}."),
                actions=[{"id": "navigate", "label": "System",
                          "kind": "navigate",
                          "route": "/system.html"}],
                truth={"kind": "health"})
        if re.search(r"\bwhat needs setup\b|\bwhat isn't installed\b|"
                     r"\bwhat isnt installed\b|\bmissing\b.*\binstall|"
                     r"\bneeds (to be )?set up\b|\bsetup required\b", t):
            rows = self._state_rows()
            pending = [r for r in rows
                       if r["state"] in ("setup_required",
                                         "authorization_required")]
            if not pending:
                return Resolution("answer", "diagnose",
                    text="Nothing is waiting on setup.")
            lines = "; ".join(f"{r['name']} ({r['state']})"
                              for r in pending[:8])
            return Resolution(
                "answer", "diagnose",
                text=f"Waiting on setup: {lines}.",
                actions=[{"id": a["id"], "label": a["label"],
                          "kind": "execute"} for r in pending
                         for a in r.get("actions", [])[:1]][:4],
                truth={"kind": "setup"})
        if re.search(r"\bwhat can (you|u) do\b|\bwhat are your "
                     r"(capabilities|features)\b|\bcapabilities\b|"
                     r"\bwhat do you do\b|\bhelp\b$", t):
            return self._what_can_you_do()
        if re.search(r"\bwhat can'?t you do\b|\bwhat can you not do\b|"
                     r"\blimitations\b|\bnot implemented\b|"
                     r"\bunfinished\b|\bplanned\b", t):
            missing = [f for f in self.catalog.all()
                       if f.development_status in
                       ("planned", "not_implemented", "partial",
                        "experimental")]
            if not missing:
                return Resolution("answer", "diagnose",
                    text="Nothing is marked unimplemented.")
            lines = "; ".join(
                f"{f.name} ({f.development_status})"
                for f in missing[:10])
            return Resolution(
                "answer", "diagnose",
                text=(f"Partial or unfinished right now: {lines}."),
                truth={"kind": "dev_status"})
        if re.search(r"\bversion\b|\bwhat changed\b|\bchangelog\b|"
                     r"\bwhat's new\b", t):
            fn = self._env.get("version")
            ver = fn() if callable(fn) else None
            return Resolution(
                "answer", "diagnose",
                text=(f"Nexus Core {ver}." if ver else
                      "Version info isn't reachable."),
                truth={"kind": "version", "version": ver})
        return None

    def _what_can_you_do(self) -> Resolution:
        """Grouped by category, generated from the live catalog —
        never a static paragraph."""
        cats: dict[str, list[tuple[str, str]]] = {}
        for f in self.catalog.all():
            st = self.catalog.feature_state(f.id, self._env)
            state = st.get("runtime_state", "")
            tag = "" if state in ("verified", "available", "running",
                                  "not_applicable") else f" ({state})"
            cats.setdefault(f.category, []).append(
                (f.name + tag, state))
        order = ("development", "research", "images", "models", "git",
                 "automation", "memory", "voice", "interface",
                 "connectors", "system")
        parts = []
        for cat in order:
            items = cats.get(cat)
            if not items:
                continue
            names = ", ".join(n for n, _ in items[:6])
            more = f" +{len(items) - 6} more" if len(items) > 6 else ""
            parts.append(f"{cat.title()}: {names}{more}")
        return Resolution(
            "answer", "help",
            text="Here's what I can do right now — "
                 + ". ".join(parts) + ".",
            actions=[{"id": "navigate", "label": "Everything",
                      "kind": "navigate",
                      "route": "/index.html#capabilities"}],
            truth={"kind": "catalog"})

    # -- control ----------------------------------------------------------

    _NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
                  "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                  "nine": 9, "ten": 10, "twelve": 12, "sixteen": 16}

    def _control(self, t: str) -> Resolution | None:
        """Direct mutation requests — 'turn voice off', 'use Isabella',
        'set workers to four'."""
        # "set workers to 4" / "use four workers" / "workers to 4"
        m = re.search(
            r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"twelve|sixteen)\s+workers?\b|"
            r"\bworkers?\s+to\s+(\d+)", t)
        if m and re.search(r"\b(set|use|give|make|run|have|want)\b", t):
            raw = m.group(1) or m.group(2) or "0"
            n = int(self._NUM_WORDS.get(raw, raw)) \
                if not raw.isdigit() else int(raw)
            spec = self.actions.get("workers.set_ceiling")
            if spec and n:
                return self._run(spec, {"value": n})
        # "set X to N" / "turn the volume to 30%" / "set volume to 0.3"
        m = re.search(
            r"\b(?:set|turn|put|make)\b.{0,30}?\bto\b\s*"
            r"(\d+(?:\.\d+)?)\s*%?", t)
        if m:
            st = self.settings.find(t)
            if st and st.type in ("int", "float"):
                return self._apply_setting(
                    st, float(m.group(1)),
                    scaled=(st.key == "voice_volume"))
        st = self.settings.find(t)
        act = self.actions.find(t)
        if act and act.id == "navigate":
            act = None  # 'open/take me to' resolves in _where
        # "make it quieter" / "turn it up" — relative nudge on a
        # numeric setting.
        if st and st.type in ("int", "float") and re.search(
                r"\b(up|down|louder|quieter|higher|lower)\b", t):
            cur = self.settings.current(st.key)
            try:
                cur = float(cur)
            except (TypeError, ValueError):
                cur = ((st.minimum or 0.0) + (st.maximum or 1.0)) / 2.0
            step = ((st.maximum or 1.0) - (st.minimum or 0.0)) / 5.0 or 0.2
            down = bool(re.search(r"\b(down|quieter|lower)\b", t))
            return self._apply_setting(
                st, cur - step if down else cur + step)
        wants_on = bool(re.search(
            r"\b(turn|switch|set|put|keep).{0,15}\bon\b|\benable\b|"
            r"\bunmute\b|\bun-mute\b|\bresume\b|\bstart\b", t))
        wants_off = bool(re.search(
            r"\b(turn|switch|set).{0,15}\boff\b|\bdisable\b|\bmute\b|"
            r"\bstop\b|\bshut\b|\bquiet\b|\bpause\b|\bsilence\b", t))
        if st and (wants_on or wants_off):
            # Prefer the domain action over a bare config write when it
            # belongs to the same feature area — 'mute yourself' should
            # run voice.mute (stops playback), not just flip a flag.
            # The action's own default value encodes direction
            # (voice.mute → True, voice.unmute → False).
            if act and act.risk in ("low_risk", "read_only") and (
                    act.feature_id and
                    st.key.startswith(act.feature_id.split("_")[0])
                    or act.id.split(".")[0] == st.key.split("_")[0]):
                return self._run(act, {})
            return self._apply_setting(st, wants_on)
        wants_val = re.search(r"\buse\b\s+([a-z0-9\-']+)\b", t)
        if st and wants_val:
            val = wants_val.group(1)
            # Only claim when the captured word resolves as a real value
            # for this setting — 'use answer memory' is a how-to, not a
            # bool write.
            allowed = [str(a).lower()
                       for a in self.settings.allowed_values(st.key)]
            if st.type == "choice" and allowed and \
                    any(val in a for a in allowed):
                return self._apply_setting(st, val)
            if st.type == "bool" and val in ("on", "off", "true",
                                             "false"):
                return self._apply_setting(st, val in ("on", "true"))
        # A registered action fires ONLY on an imperative signal —
        # 'use isabella', 'pause autonomy', 'switch backend'. A bare
        # alias in a question ('what's the image backend') must never
        # mutate anything.
        if act and (wants_on or wants_off or wants_val or
                    re.search(r"^(?:please\s+)?(use|switch|set|turn|"
                              r"start|stop|pause|resume|restart|"
                              r"connect|disconnect|test|install|"
                              r"enable|disable|mute|unmute|apply|"
                              r"change)\b", t)):
            if act.risk in ("sensitive", "destructive"):
                return self._propose(act)
            params = {}
            if wants_val and act.id in ("voice.set_preset",
                                        "image.backend.set",
                                        "model.set_performance"):
                params["value"] = wants_val.group(1)
            return self._run(act, params)
        return None

    def _apply_setting(self, spec: SettingSpec, value: Any,
                       scaled: bool = False,
                       confirmed: bool = False,
                       undoing: bool = False) -> Resolution:
        """Mutate through the SettingsRegistry — verified, undoable."""
        if scaled and isinstance(value, (int, float)) and value > 1:
            value = value / 100.0
        if spec.risk in ("confirm", "sensitive") and not confirmed:
            return self._propose_setting(spec, value)
        if not getattr(self, "_execute", True):
            return Resolution(
                "confirm", "control",
                text=f"{spec.name} would become "
                     f"{_fmt_value(spec, value)}.",
                setting=spec,
                truth={"kind": "setting", "key": spec.key,
                       "dry_run": True})
        res = self.settings.set(spec.key, value)
        if not res.get("ok"):
            return Resolution(
                "answer", "control",
                text=f"I couldn't change {spec.name} — "
                     f"{res.get('error', 'unknown error')}.",
                setting=spec,
                links=[{"route": spec.ui_route,
                        "label": spec.name}] if spec.ui_route else [])
        prev = res.get("previous")
        val = res.get("value")
        self._context["last_executed"] = {
            "setting": spec.key, "value": val,
            "undo": {"action": "_set", "params": {"key": spec.key,
                                                  "value": prev}}
            if prev is not None and prev != val else None,
            "route": spec.ui_route}
        desc = _fmt_value(spec, val)
        return Resolution(
            "execute", "control",
            text=(f"Reverted — {spec.name.lower()} is back to {desc}."
                  if undoing else f"{spec.name} is now {desc}."),
            setting=spec,
            result=ActionResult(True, verified=bool(res.get("verified")),
                                previous=prev),
            controls=[_control_for(spec, self.settings)],
            links=[{"route": spec.ui_route, "label": spec.name}]
            if spec.ui_route else [],
            truth={"kind": "setting", "key": spec.key,
                   "previous": prev, "value": val})

    def _propose_setting(self, spec: SettingSpec,
                         value: Any) -> Resolution:
        self._context["proposed"] = {
            "action": "_set",
            "params": {"key": spec.key, "value": value},
            "route": spec.ui_route}
        return Resolution(
            "confirm", "control",
            text=f"{spec.name} is a {spec.risk} change — "
                 f"set it to {_fmt_value(spec, value)}?",
            setting=spec,
            actions=[{"id": "_set", "label": "Apply",
                      "kind": "execute",
                      "params": {"key": spec.key, "value": value}}],
            truth={"kind": "setting", "key": spec.key, "value": value})

    def _propose(self, spec: ActionSpec) -> Resolution:
        self._context["proposed"] = {"action": spec.id, "params": {},
                                     "route": spec.ui_route}
        links = ([{"route": spec.ui_route, "label": "Open"}]
                 if spec.ui_route else [])
        if spec.id == "github.connect":
            links = [{"route": "/settings.html#connections",
                      "label": "Connection Settings"}]
        return Resolution(
            "confirm", "control",
            text=spec.confirmation_prompt or
                 f"{spec.name} needs an explicit yes first.",
            action=spec,
            actions=[{"id": spec.id, "label": spec.name,
                      "kind": "execute"}],
            links=links,
            truth={"kind": "action", "action": spec.id})

    def _run(self, spec: ActionSpec, params: dict,
             confirmed: bool = False,
             undoing: bool = False) -> Resolution:
        if not getattr(self, "_execute", True):
            return Resolution(
                "confirm", "control",
                text=f"{spec.name} would run.",
                action=spec,
                truth={"kind": "action", "action": spec.id,
                       "dry_run": True})
        res = self.actions.execute(spec.id, params, confirmed=confirmed)
        if res.message == "needs_confirmation":
            return self._propose(spec)
        if res.message == "connect_securely":
            return Resolution(
                "answer", "control",
                text=("GitHub connects through a secure card so the "
                      "token never touches chat — I'll open Connection "
                      "Settings."),
                action=spec, result=res, links=list(res.links),
                truth={"kind": "action", "action": spec.id})
        if res.ok:
            undo = None
            if spec.reversible and res.previous is not None:
                undo = {"action": spec.id,
                        "params": {"value": res.previous}}
            self._context["last_executed"] = {
                "action": spec.id, "params": params, "undo": undo,
                "route": spec.ui_route}
            verified = " and verified" if res.verified else ""
            return Resolution(
                "execute", "control",
                text=(f"Reverted — {spec.name.lower()} undone{verified}."
                      if undoing else
                      f"Done — {spec.name.lower()} applied{verified}.")
                     + (f" {res.detail}." if res.detail else ""),
                action=spec, result=res, links=list(res.links),
                truth={"kind": "action", "action": spec.id,
                       "verified": res.verified})
        return Resolution(
            "answer", "control",
            text=f"I couldn't — {res.detail or res.message or 'it failed'}.",
            action=spec, result=res, links=list(res.links),
            truth={"kind": "action", "action": spec.id, "ok": False})

    # -- status / explain ---------------------------------------------------

    def _status_or_explain(self, t: str) -> Resolution | None:
        """'Can you X', 'what is X', 'why can't you X', 'how do I X'."""
        feature = self.catalog.find(t)
        st = self.settings.find(t)
        if feature is None and st is None:
            return None
        # "X isn't working/connected" — a state assertion about a
        # probed feature; the honest capability verdict, never a
        # config flag echo. ('github isn't connected' must not answer
        # 'github_enabled is on'.)
        if feature is not None and re.search(
                r"\b(isn'?t|is not|aren'?t|are not|won'?t|doesn'?t|"
                 r"don'?t|not)\b.{0,20}\b(working|connected|responding|"
                 r"running|installed|available|set up)\b", t):
            state = self.catalog.feature_state(feature.id, self._env)
            return self._why_not(feature, state)
        # "is voice on", "is it muted" — a setting read for enabled/
        # on/off predicates; "is github connected" / "is it running"
        # are live-state predicates → the feature's capability probe.
        live_pred = bool(re.search(
            r"\b(connected|authorized|working|running|responding|"
            r"installed|available|ready|healthy|broken|paused)\b", t))
        if feature is not None and live_pred and (
                feature.capability_id or feature.probe):
            state = self.catalog.feature_state(feature.id, self._env)
            return self._capability_answer(feature, state)
        if st is not None and re.search(
                r"^(is|are|am)\b.*\b(on|off|enabled|disabled|muted|"
                r"running|active|working|connected|paused)\b", t):
            return self._describe_setting(st)
        if st is not None and re.search(
                r"\bis\b.{0,20}\b(on|off|enabled|disabled|muted|"
                r"running|active)\b", t) and feature is None:
            return self._describe_setting(st)
        # "how many workers" / "what's the worker count" — a live count
        # from the probe detail.
        if feature is not None and re.search(
                r"\bhow many\b|\bhow much\b|\bcount\b|\bnumber of\b", t):
            state = self.catalog.feature_state(feature.id, self._env)
            detail = state.get("runtime_detail") or \
                state.get("runtime_state") or "unknown"
            return Resolution(
                "answer", "status",
                text=f"{feature.name}: {detail}.",
                feature=feature,
                links=[{"route": feature.ui_route, "label": "Open"}]
                if feature.ui_route else [],
                truth={"kind": "feature", "id": feature.id})
        # 'push to github', 'create a repo', 'run the tests' — an
        # imperative with a matched name must fall through to the
        # tools/model lane; the catalog only answers questions and
        # control requests it actually resolved.
        if re.search(r"^(?:please\s+)?"
                     r"(?:push|pull|commit|create|clone|merge|branch|"
                     r"deploy|write|build|run|execute|edit|delete|"
                     r"remove|generate|draw|paint|search|research|"
                     r"read|summari[sz]e|analy[sz]e|scaffold|add|"
                     r"modify|refactor|review|debug|fetch|download|"
                     r"upload|publish)\b", t):
            return None
        if st is not None and (feature is None or
                               not re.search(r"\b(can|could|do|does|"
                                             r"what|why|how|is|are)\b",
                                             t)):
            return self._describe_setting(st)
        if feature is None:
            return self._describe_setting(st)
        state = self.catalog.feature_state(feature.id, self._env)
        runtime = state.get("runtime_state", "")
        # "what's the image backend" — 'the' makes it a value question,
        # not a feature explain; describe the matched setting.
        if st is not None and re.search(
                r"\bwhat('s| is| are)\s+(the|your|my|current)\b", t):
            return self._describe_setting(st)
        # "what voice are you using" — current choice value on a
        # named selector ('voice', 'backend', 'model', 'persona').
        if re.search(r"\bwhat\b.{0,20}\b(using|current|running|set "
                     r"to|active)\b", t):
            if "voice" in t and "preset" not in t:
                preset = self.settings.get("voice_preset_id")
                if preset:
                    return self._describe_setting(preset)
            if ("backend" in t or "engine" in t) and "image" in t:
                spec = self.settings.get("image_backend")
                if spec:
                    return self._describe_setting(spec)
            if "persona" in t or "personality" in t:
                fn = self._env.get("probe")
                try:
                    p = fn("persona") if callable(fn) else {}
                except Exception:
                    p = {}
                detail = str((p or {}).get("detail") or "")
                return Resolution(
                    "answer", "status",
                    text=(f"Active persona: {detail}." if detail else
                          "No persona is active."),
                    feature=feature,
                    truth={"kind": "probe", "name": "persona"})
            if "model" in t:
                return self._capability_answer(feature, state)
        # "X isn't working/connected" — a state assertion that expects
        # the honest capability verdict.
        if re.search(r"\b(isn'?t|is not|aren'?t|are not|won'?t|"
                     r"doesn'?t|not)\b.{0,15}\b(working|connected|"
                     r"responding|running|available|installed)\b", t):
            return self._why_not(feature, state)
        # "can you X" / "do you have X" — capability question.
        if re.search(r"\b(can|could|do) (you|u)\b|\bdo you have\b|"
                     r"\bis there\b|\bable to\b", t):
            return self._capability_answer(feature, state)
        # "why can't you X" / "why isn't X working"
        if re.search(r"\bwhy\b.{0,30}\b(can'?t|cannot|won'?t|isn'?t|"
                     r"not|broken|fail)\b", t):
            return self._why_not(feature, state)
        # "how do I X" / "how do I use X" — usage + link
        if re.search(r"\bhow (do|can|to)\b|\bhow does\b", t):
            return self._how_to(feature, state)
        # Bare "what is X" — explain.
        if re.search(r"\bwhat('s| is| are)\b|\btell me about\b|"
                     r"\bexplain\b|\bdescribe\b", t):
            return self._explain(feature, state)
        # A setting phrase that isn't a command — describe it.
        if st is not None and feature is None:
            return self._describe_setting(st)
        return None

    def _capability_answer(self, f: FeatureSpec,
                           state: dict) -> Resolution:
        runtime = state.get("runtime_state", "not_applicable")
        detail = state.get("runtime_detail", "")
        dev = f.development_status
        actions = []
        links = ([{"route": f.ui_route, "label": "Open"}]
                 if f.ui_route else [])
        if dev in ("planned", "not_implemented"):
            return Resolution(
                "answer", "status",
                text=(f"{f.name} isn't implemented in this build yet."
                      + (f" {detail}" if detail else "")),
                feature=f, links=links,
                truth={"kind": "feature", "id": f.id, "state": runtime,
                       "dev": dev})
        if runtime in ("verified", "available", "running",
                       "not_applicable"):
            extra = f" {detail}." if detail else ""
            if dev == "experimental":
                verdict = (f"{f.name} works, but it's experimental "
                           "— expect rough edges")
            elif dev == "partial":
                verdict = (f"{f.name} works, partially"
                           + (f" — {' '.join(f.limitations)}"
                              if f.limitations else ""))
            else:
                verdict = f"{f.name} is ready"
            return Resolution(
                "answer", "status",
                text=f"Yes — {verdict}.{extra}",
                feature=f, links=links,
                actions=[{"id": a, "label": self.actions.get(a).name
                          if self.actions.get(a) else a,
                          "kind": "execute"}
                         for a in f.action_ids[:3]],
                truth={"kind": "feature", "id": f.id, "state": runtime})
        if runtime == "setup_required":
            install = next((a for a in f.action_ids
                            if "install" in a or "provision" in a), "")
            acts = ([{"id": install, "label": "Set it up",
                      "kind": "execute"}] if install else [])
            if install:
                self._context["proposed"] = {"action": install,
                                             "params": {},
                                             "route": f.ui_route}
            return Resolution(
                "answer", "status",
                text=(f"{f.name} is built in, but it needs setup "
                      f"first.{(' ' + detail) if detail else ''}"),
                feature=f, actions=acts, links=links,
                truth={"kind": "feature", "id": f.id, "state": runtime})
        if runtime == "authorization_required":
            connect = next((a for a in f.action_ids
                            if "connect" in a), "")
            acts = ([{"id": connect, "label": "Connect",
                      "kind": "execute"}] if connect else [])
            return Resolution(
                "answer", "status",
                text=(f"{f.name} is installed but not connected — "
                      f"it needs authorization."
                      + (f" {detail}" if detail else "")),
                feature=f, actions=acts, links=links,
                truth={"kind": "feature", "id": f.id, "state": runtime})
        if runtime in ("degraded", "broken", "unavailable"):
            fix = next((a for a in f.action_ids
                        if "install" in a or "start" in a
                        or "repair" in a), "")
            acts = ([{"id": fix, "label": "Fix it", "kind": "execute"}]
                    if fix else [])
            if fix:
                self._context["proposed"] = {"action": fix,
                                             "params": {},
                                             "route": f.ui_route}
            return Resolution(
                "answer", "status",
                text=(f"{f.name} is {runtime.replace('_', ' ')}"
                      + (f" — {detail}" if detail else "") + "."),
                feature=f, actions=acts, links=links,
                truth={"kind": "feature", "id": f.id, "state": runtime})
        return Resolution(
            "answer", "status",
            text=f"{f.name} is {runtime.replace('_', ' ') or 'unknown'}.",
            feature=f, links=links,
            truth={"kind": "feature", "id": f.id, "state": runtime})

    def _why_not(self, f: FeatureSpec, state: dict) -> Resolution:
        runtime = state.get("runtime_state", "")
        detail = state.get("runtime_detail", "")
        acts = []
        if f.development_status in ("planned", "not_implemented"):
            text = f"{f.name} isn't implemented in this build."
        elif runtime == "setup_required":
            text = (f"{f.name} isn't set up yet"
                    + (f" — {detail}" if detail else "") + ".")
            fix = next((a for a in f.action_ids
                        if "install" in a or "provision" in a), "")
            if fix:
                acts = [{"id": fix, "label": "Set it up",
                         "kind": "execute"}]
                self._context["proposed"] = {"action": fix,
                                             "params": {},
                                             "route": f.ui_route}
        elif runtime == "authorization_required":
            text = (f"{f.name} is installed but needs authorization"
                    + (f" — {detail}" if detail else "") + ".")
            connect = next((a for a in f.action_ids
                            if "connect" in a), "")
            if connect:
                acts = [{"id": connect, "label": "Connect",
                         "kind": "execute"}]
                self._context["proposed"] = {"action": connect,
                                             "params": {},
                                             "route": f.ui_route}
        elif runtime in ("degraded", "broken", "unavailable"):
            text = (f"{f.name} is {runtime.replace('_', ' ')}"
                    + (f" — {detail}" if detail else "") + ".")
            fix = next((a for a in f.action_ids
                        if "install" in a or "start" in a
                        or "repair" in a), "")
            if fix:
                acts = [{"id": fix, "label": "Fix it",
                         "kind": "execute"}]
                self._context["proposed"] = {"action": fix,
                                             "params": {},
                                             "route": f.ui_route}
        elif f.limitations:
            text = f"{f.name} works, but: " + " ".join(f.limitations)
        else:
            text = f"{f.name} looks fine — {runtime or 'ready'}."
        return Resolution("answer", "diagnose", text=text, feature=f,
                          actions=acts,
                          links=[{"route": f.ui_route, "label": "Open"}]
                          if f.ui_route else [],
                          truth={"kind": "feature", "id": f.id,
                                 "state": runtime})

    def _how_to(self, f: FeatureSpec, state: dict) -> Resolution:
        runtime = state.get("runtime_state", "")
        ex = f" For example: \"{f.examples[0]}\"." if f.examples else ""
        route_line = ""
        links = []
        if f.ui_route:
            route_line = f" It lives at {f.ui_route}."
            links = [{"route": f.ui_route, "label": "Open it"}]
            self._context["proposed"] = {"route": f.ui_route}
        return Resolution(
            "answer", "help",
            text=f"{f.description}{ex}{route_line}",
            feature=f, links=links,
            truth={"kind": "feature", "id": f.id, "state": runtime})

    def _explain(self, f: FeatureSpec, state: dict) -> Resolution:
        runtime = state.get("runtime_state", "")
        status = ("" if runtime in ("", "not_applicable", "verified",
                                    "available", "running")
                  else f" Right now it's {runtime.replace('_', ' ')}.")
        lim = (" Caveats: " + " ".join(f.limitations)) if f.limitations \
              else ""
        return Resolution(
            "answer", "help",
            text=f"{f.description}{status}{lim}",
            feature=f,
            links=[{"route": f.ui_route, "label": "Open"}]
            if f.ui_route else [],
            truth={"kind": "feature", "id": f.id, "state": runtime})

    def _describe_setting(self, spec: SettingSpec) -> Resolution:
        cur = self.settings.current(spec.key)
        allowed = self.settings.allowed_values(spec.key)
        opts = (f" Options: {', '.join(map(str, allowed))}."
                if allowed else "")
        return Resolution(
            "answer", "status",
            text=(f"{spec.name}: {spec.description} Currently "
                  f"{_fmt_value(spec, cur)}.{opts}"),
            setting=spec,
            controls=[_control_for(spec, self.settings)],
            links=[{"route": spec.ui_route, "label": spec.name}]
            if spec.ui_route else [],
            truth={"kind": "setting", "key": spec.key, "value": cur})

    # -- where / navigate ---------------------------------------------------

    def _where(self, t: str) -> Resolution | None:
        nav = re.search(r"\b(open|show|take me to|go to|navigate to|"
                        r"bring up)\b", t)
        where = re.search(r"\bwhere\b.{0,20}\b(is|are|do i|can i|would "
                          r"i|the)\b|\bhow do i (get to|find|open)\b|"
                          r"\bwhich page\b|\bwhere in\b", t)
        if not nav and not where:
            return None
        section = self.pages.find_section(t)
        if section:
            page, sec = section
            route = page.deep_link(sec.id)
            self._context["proposed"] = {"route": route}
            if nav:
                return self._navigate(route, label=sec.name,
                                      page=page)
            return Resolution(
                "answer", "where",
                text=f"{sec.name} is in {page.title}.",
                page=page, page_section=sec.id,
                links=[{"route": route, "label": f"Open {sec.name}"}],
                truth={"kind": "page", "route": route})
        page = self.pages.find(t)
        if page is None:
            feat = self.catalog.find(t)
            if feat and feat.ui_route:
                page = self.pages.get(feat.ui_route.split("#")[0])
                if page:
                    route = feat.ui_route
                    self._context["proposed"] = {"route": route}
                    if nav:
                        return self._navigate(route, label=feat.name,
                                              page=page)
                    return Resolution(
                        "answer", "where",
                        text=f"{feat.name} is under {page.title}.",
                        feature=feat, page=page,
                        links=[{"route": route,
                                "label": f"Open {feat.name}"}],
                        truth={"kind": "page", "route": route})
            return None
        route = page.route
        self._context["proposed"] = {"route": route}
        if nav:
            return self._navigate(route, label=page.title, page=page)
        return Resolution(
            "answer", "where",
            text=f"That's the {page.title} page — {page.purpose}",
            page=page,
            links=[{"route": route, "label": f"Open {page.title}"}],
            truth={"kind": "page", "route": route})

    def _navigate(self, route: str, label: str = "",
                  page: PageSpec | None = None) -> Resolution:
        return Resolution(
            "navigate", "where",
            text=f"Opening {label or route}.",
            page=page,
            actions=[{"id": "navigate", "label": "Open",
                      "kind": "navigate", "route": route}],
            links=[{"route": route, "label": label or route}],
            truth={"kind": "navigate", "route": route})

    # -- live state rows ----------------------------------------------------

    def _health_rows(self) -> list[dict[str, Any]]:
        rows = []
        fn = self._env.get("capabilities_all")
        if callable(fn):
            try:
                for rep in fn() or []:
                    rows.append({"name": getattr(rep, "name", "?"),
                                 "state": getattr(rep, "state", ""),
                                 "detail": getattr(rep, "detail", "")})
            except Exception:
                pass
        return rows

    def _state_rows(self) -> list[dict[str, Any]]:
        rows = []
        for f in self.catalog.all():
            st = self.catalog.feature_state(f.id, self._env)
            rows.append({"id": f.id, "name": f.name,
                         "state": st.get("runtime_state", ""),
                         "detail": st.get("runtime_detail", ""),
                         "actions": [{"id": a,
                                      "label": (self.actions.get(a).name
                                                if self.actions.get(a)
                                                else a)}
                                     for a in f.action_ids]})
        return rows

    # -- API payloads ---------------------------------------------------------

    def features_payload(self) -> dict[str, Any]:
        return self.catalog.summary(self._env)

    def pages_payload(self) -> dict[str, Any]:
        return {"pages": [p.as_dict() for p in self.pages.all()]}

    def actions_payload(self) -> dict[str, Any]:
        return {"actions": [a.as_dict() for a in self.actions.all()]}

    def settings_payload(self) -> dict[str, Any]:
        return {"settings": [self.settings.describe(s.key)
                             for s in self.settings.chat_mutable()]}

    def execute_action(self, action_id: str, params: dict | None = None,
                       confirmed: bool = False) -> dict[str, Any]:
        if action_id == "_set":
            key = str((params or {}).get("key", ""))
            value = (params or {}).get("value")
            return self.settings.set(key, value)
        res = self.actions.execute(action_id, params,
                                   confirmed=confirmed)
        return res.as_dict()

    def context(self) -> dict[str, Any]:
        return dict(self._context)


# ---------------------------------------------------------------------------

def _fmt_value(spec: SettingSpec, value: Any) -> str:
    if spec.type == "bool":
        return "on" if value else "off"
    if spec.type == "float" and spec.key == "voice_volume":
        try:
            return f"{float(value) * 100:.0f}%"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _control_for(spec: SettingSpec, reg: SettingsRegistry) -> dict:
    """Inline-control descriptor the frontend renders."""
    cur = reg.current(spec.key)
    out = {"kind": "", "key": spec.key, "label": spec.name,
           "value": cur}
    if spec.type == "bool":
        out["kind"] = "toggle"
    elif spec.type == "choice":
        out["kind"] = "select"
        out["options"] = reg.allowed_values(spec.key)
    elif spec.type in ("int", "float"):
        out["kind"] = "slider"
        out["min"] = spec.minimum
        out["max"] = spec.maximum
        out["step"] = 0.05 if spec.type == "float" else 1
    return out
