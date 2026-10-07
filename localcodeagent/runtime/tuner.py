"""Runtime performance tuner for managed llama.cpp models.

Determines a fast, stable launch configuration for the current machine and
llama.cpp build. Results persist to ``data/runtime_tuning.json`` keyed by a
fingerprint of (GPU, llama.cpp build, model file, context), so benchmarked
choices are reused across launches and automatically invalidated when the
hardware, runtime, or model changes.

Nothing is benchmarked at call time unless explicitly requested: tuned_flags()
returns persisted benchmark results when valid, else safe heuristic defaults
derived from detected llama.cpp flag support.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..fsutil import atomic_write_text
from ..procutil import no_window_flags
from typing import Any, Callable

from ..config import AgentConfig, ModelProfile


TUNER_VERSION = 1
PROBE_TIMEOUT = 20.0

# Failure signatures seen in llama-server probe crashes. Benchmarking classifies
# each candidate failure so OOM/instability is recorded distinctly from a plain
# launch error — that feeds mark_bad() and the operator-facing status.
_ERROR_CLASSES = (
    ("out of memory", "oom"), ("cudaMalloc", "oom"), ("failed to allocate", "oom"),
    ("insufficient memory", "oom"), ("cannot allocate", "oom"),
    ("access violation", "crash"), ("assertion failed", "crash"),
    ("cuda error", "crash"), ("ggml_backend_cuda", "crash"),
    ("timed out", "timeout"), ("timeout", "timeout"),
)


def classify_launch_error(message: str) -> str:
    low = str(message or "").lower()
    for pattern, kind in _ERROR_CLASSES:
        if pattern.lower() in low:
            return kind
    return "error"


# Task-class → context sizing. The resident llama-server keeps the ctx it was
# launched with; when a task genuinely needs a larger window, ensure_ready
# restarts the model at the larger class rather than silently truncating.
CONTEXT_CLASSES = {
    "small": 8192,        # ordinary conversation / quick answers
    "medium": 16384,      # everyday technical/coding work
    "large": 24576,       # multi-file coding, moderate repo context
    "xlarge": 32768,      # repo-scale/research — only when needed
}


def context_class(need_tokens: int) -> str:
    """Smallest context class that covers the estimated need."""
    for name in ("small", "medium", "large", "xlarge"):
        if need_tokens <= CONTEXT_CLASSES[name] * 0.9:
            return name
    return "xlarge"


def context_class_size(name: str) -> int:
    return CONTEXT_CLASSES.get(name, CONTEXT_CLASSES["medium"])


@dataclass(slots=True)
class TuningResult:
    args: list[str]
    source: str = "heuristic"  # heuristic | benchmarked | manual
    metrics: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    tuned_at: float = 0.0


class RuntimeTuner:
    def __init__(
        self,
        runtime_root: Path,
        config: AgentConfig,
        *,
        runtime=None,
        storage: str = "data/runtime_tuning.json",
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.config = config
        self.runtime = runtime
        self.storage_path = self.runtime_root / storage
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._capabilities: dict[str, Any] | None = None
        self._data = self._load()
        self._data.setdefault("bad_results", {})

    # ------------------------------------------------------------------
    # persistence

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == TUNER_VERSION:
                return raw
        except Exception:
            pass
        return {"version": TUNER_VERSION, "results": {}, "benchmarks": []}

    def _save(self) -> None:
        atomic_write_text(self.storage_path, json.dumps(self._data, indent=2) + "\n")

    def reset(self, model_id: str | None = None) -> None:
        if model_id:
            self._data["results"].pop(model_id, None)
        else:
            self._data["results"] = {}
            self._data["benchmarks"] = []
        self._save()

    # ------------------------------------------------------------------
    # fingerprinting / invalidation

    def fingerprint(self, profile: ModelProfile) -> str:
        hw = getattr(self.runtime, "hardware", None)
        gpu_names = ",".join(sorted(g.name for g in getattr(hw, "gpus", []) or [])) or "cpu-only"
        caps = self.capabilities()
        build = str(caps.get("build") or "unknown")
        try:
            st = (self.runtime_root / profile.model_path).resolve().stat() if profile.model_path else None
            model_sig = f"{profile.model_path}:{st.st_size}:{int(st.st_mtime)}" if st else profile.model_path
        except OSError:
            model_sig = profile.model_path
        key = "|".join([
            gpu_names, build, model_sig,
            str(profile.context_window), str(profile.gpu_layers),
            "v1",
        ])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------
    # llama.cpp capability detection

    def capabilities(self) -> dict[str, Any]:
        if self._capabilities is not None:
            return self._capabilities
        caps: dict[str, Any] = {"supported": set(), "build": ""}
        exe = self._find_llama()
        if exe:
            try:
                help_text = ""
                # Cold binary loads (first run, AV/Defender scan) can exceed the
                # short probe timeout; retry once so capabilities don't silently
                # degrade to 'unavailable' for the whole session.
                for _attempt in range(2):
                    try:
                        proc = subprocess.run(
                            [exe, "--help"], capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                            creationflags=no_window_flags(),
                        )
                        help_text = (proc.stdout or "") + (proc.stderr or "")
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if not help_text:
                    raise TimeoutError("llama --help probe timed out")
                supported = set()
                for flag in (
                    "--flash-attn", "--cache-reuse", "--batch-size", "--ubatch-size",
                    "--threads", "--gpu-layers", "--ctx-size", "--fit-target",
                    "--model-draft", "--draft-max", "--draft-min",
                    "--cache-type-k", "--cache-type-v", "--swa-full", "--mlock",
                ):
                    token = flag.lstrip("-").replace("-", "[-_ ]?")
                    if re.search(token, help_text, re.IGNORECASE):
                        supported.add(flag)
                caps["supported"] = supported
                ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT, creationflags=no_window_flags())
                caps["build"] = ((ver.stdout or "") + (ver.stderr or "")).strip().splitlines()[0] if (ver.stdout or ver.stderr) else "unknown"
            except Exception:
                caps["build"] = "unavailable"
        self._capabilities = caps
        return caps

    def _find_llama(self) -> str | None:
        if self.runtime is not None:
            try:
                for profile in self.config.models:
                    if profile.runtime == "llama_cpp":
                        exe = self.runtime.discover_llama_server(profile)
                        if exe:
                            return str(exe)
            except Exception:
                pass
        return None

    def supports(self, flag: str) -> bool:
        return flag in self.capabilities().get("supported", set())

    # ------------------------------------------------------------------
    # tuning resolution

    def tuned_flags(self, profile: ModelProfile, *, mode: str = "auto") -> list[str]:
        """Extra llama-server args for this profile — benchmarked when valid,
        else safe heuristics derived from detected flag support."""
        fp = self.fingerprint(profile)
        stored = self._data["results"].get(profile.id)
        if (
            stored
            and stored.get("fingerprint") == fp
            and tuple(stored.get("args") or ()) not in self._bad_for(profile)
        ):
            return list(stored.get("args") or [])
        args: list[str] = []
        if mode in {"auto", "balanced", "max"}:
            if self.supports("--flash-attn"):
                args += ["--flash-attn", "auto"]
            if self.supports("--cache-reuse"):
                args += ["--cache-reuse", "256"]
        if mode == "quiet":
            args += ["--batch-size", "256", "--ubatch-size", "128"]
        elif mode == "max":
            args += ["--batch-size", "1024", "--ubatch-size", "512"]
        else:
            args += ["--batch-size", "512", "--ubatch-size", "256"]
        if profile.threads <= 0:
            threads = self._default_threads(mode)
            if threads:
                args += ["--threads", str(threads)]
        return args

    def _bad_for(self, profile: ModelProfile) -> set[tuple]:
        """Arg-tuples that provably crashed/OOM'd this model+runtime."""
        fp = self.fingerprint(profile)
        return {
            tuple(row.get("args") or ())
            for row in self._data.get("bad_results", {}).get(profile.id, [])
            if row.get("fingerprint") == fp
        }

    def mark_bad(
        self, profile: ModelProfile, args: list[str], error: str,
    ) -> None:
        """Record a tuned configuration that failed a real launch.

        Benchmarks only prove a candidate can serve a probe — a crash on the
        production launch path is stronger evidence. The config is dropped
        from results so tuned_flags() never re-picks it.
        """
        entry = {
            "args": list(args),
            "error": str(error)[:400],
            "error_kind": classify_launch_error(error),
            "fingerprint": self.fingerprint(profile),
            "at": time.time(),
        }
        bad = self._data.setdefault("bad_results", {}).setdefault(profile.id, [])
        if not any(row.get("args") == entry["args"] for row in bad):
            bad.append(entry)
            del bad[:-20]
        stored = self._data["results"].get(profile.id)
        if stored and list(stored.get("args") or []) == list(args):
            self._data["results"].pop(profile.id, None)
        self._save()

    def _default_threads(self, mode: str) -> int:
        import os
        logical = os.cpu_count() or 4
        physical = max(1, logical // 2)
        if mode == "quiet":
            return max(2, physical // 2)
        if mode == "max":
            return logical
        return physical

    def record_result(self, profile: ModelProfile, args: list[str], metrics: dict[str, Any], source: str = "benchmarked") -> None:
        self._data["results"][profile.id] = {
            "args": list(args),
            "source": source,
            "metrics": metrics,
            "fingerprint": self.fingerprint(profile),
            "tuned_at": time.time(),
        }
        self._save()

    def status(self) -> dict[str, Any]:
        caps = self.capabilities()
        return {
            "storage": str(self.storage_path),
            "capabilities": sorted(caps.get("supported", set())),
            "build": caps.get("build"),
            "performance_mode": getattr(self.config, "performance_mode", "auto"),
            "speculative_decoding": self.speculative_status(),
            "results": {
                mid: {"source": r.get("source"), "metrics": r.get("metrics"), "tuned_at": r.get("tuned_at")}
                for mid, r in self._data["results"].items()
            },
            "bad_results": {
                mid: [
                    {"args": r.get("args"), "error_kind": r.get("error_kind"),
                     "error": str(r.get("error") or "")[:160], "at": r.get("at")}
                    for r in rows
                ]
                for mid, rows in self._data.get("bad_results", {}).items()
            },
        }

    def _draft_model(self):
        """A speculative-decoding draft is any configured model whose id
        names it (…-draft) or which carries the 'draft' role marker."""
        return next(
            (m for m in self.config.models
             if getattr(m, "model_path", "")
             and ("draft" in str(m.id).lower()
                  or "draft" in (getattr(m, "roles", None) or []))),
            None)

    def speculative_status(self) -> str:
        if not self.supports("--model-draft"):
            return "not_supported"
        if self._draft_model() is None:
            return "no_draft_model"
        return "available"

    # ------------------------------------------------------------------
    # benchmarking

    def benchmark(
        self,
        profile: ModelProfile,
        *,
        launch: Callable[[ModelProfile, int, list[str]], Any] | None = None,
        candidates: list[list[str]] | None = None,
        prompt_tokens: int = 256,
        gen_tokens: int = 64,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Benchmark candidate flag sets against the real llama-server.

        ``launch(profile, port, extra_args)`` must start a llama-server and
        return an object exposing ``endpoint`` and a ``stop()`` method. Each
        candidate is timed with a bounded prompt; failures roll back to the
        previously stored (or heuristic) configuration.
        """
        if launch is None and self.runtime is not None:
            launch = getattr(self.runtime, "launch_probe", None)
        if launch is None:
            return {"status": "unavailable", "reason": "no llama.cpp launch path"}
        fp = self.fingerprint(profile)
        bad = self._bad_for(profile)
        candidates = [
            c for c in (candidates or self._default_candidates(profile))
            if tuple(c) not in bad
        ]
        if not candidates:
            candidates = [self.tuned_flags_heuristic_safe()]
        results: list[dict[str, Any]] = []
        for args in candidates:
            row = {"args": args, "ok": False}
            proc = None
            try:
                proc = launch(profile, 0, args)
                endpoint = getattr(proc, "endpoint", None) or "http://127.0.0.1:0/v1"
                metrics = self._measure(endpoint, prompt_tokens, gen_tokens, timeout)
                # Second identical request: --cache-reuse should surface cached
                # prompt tokens and a faster TTFT, proving prompt caching works.
                try:
                    metrics["warm"] = dict(self._measure(endpoint, prompt_tokens, gen_tokens, timeout))
                except Exception:
                    pass
                row.update({"ok": True, "metrics": metrics})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["error_kind"] = classify_launch_error(str(exc))
                # A candidate that OOM'd or crashed must never be re-picked
                # while this fingerprint is valid — record it now, not only
                # when the production launcher hits it.
                if row["error_kind"] in {"oom", "crash"}:
                    self.mark_bad(profile, args, str(exc))
            finally:
                try:
                    if proc is not None:
                        proc.stop()
                except Exception:
                    pass
            results.append(row)
        best = self._pick_best(results)
        self._data["benchmarks"].append({
            "model_id": profile.id, "fingerprint": fp, "at": time.time(),
            "results": results,
        })
        if best is not None:
            self.record_result(profile, best["args"], best["metrics"])
        self._save()
        out = {"status": "ok" if best is not None else "all_failed", "results": results, "best": best}
        try:
            bench_dir = self.runtime_root / "data" / "benchmarks"
            bench_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("runtime-%Y%m%d-%H%M%S")
            artifact = bench_dir / f"{stamp}-{profile.id}.json"
            artifact.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
            out["artifact"] = str(artifact)
        except OSError:
            pass
        return out

    def _default_candidates(self, profile: ModelProfile | None = None) -> list[list[str]]:
        """Bounded sweep across the launch dimensions that matter.

        Ordered cheapest-first. Each candidate is a complete extra-args set
        (batch/ubatch/threads/FA/KV-type) launched by launch_probe with tuning
        bypassed, so the measurement reflects the candidate alone.
        """
        import os
        logical = os.cpu_count() or 4
        physical = max(1, logical // 2)
        fa = ["--flash-attn", "auto"] if self.supports("--flash-attn") else []
        reuse = ["--cache-reuse", "256"] if self.supports("--cache-reuse") else []
        kv = (
            ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
            if self.supports("--cache-type-k") and self.supports("--cache-type-v")
            else []
        )

        def flags(batch: int, ubatch: int, threads: int,
                  extra: list[str] | None = None) -> list[str]:
            out = [*fa, *reuse, "--batch-size", str(batch),
                   "--ubatch-size", str(ubatch)]
            if self.supports("--threads"):
                out += ["--threads", str(threads)]
            return out + list(extra or [])

        candidates = [
            flags(512, 256, physical),                 # balanced baseline
            flags(1024, 512, physical),                # prompt-throughput heavy
            flags(256, 128, physical),                 # conservative
            flags(512, 256, max(physical + 1, logical - 1)),  # more threads
        ]
        if kv:
            candidates.append(flags(512, 256, physical, kv))  # KV-quantized q8
            # §15 — q4/q4 halves KV memory again vs q8; the sweep measures
            # whether the quality/stability tradeoff is acceptable, so it
            # runs as a candidate rather than an adopted default.
            kv4 = ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]
            candidates.append(flags(512, 256, physical, kv4))
        if fa:
            candidates.append([a for a in flags(512, 256, physical)
                               if a not in {"--flash-attn", "auto"}])
        # Speculative decoding candidate only when the runtime supports it and
        # a draft model is actually configured.
        if (
            profile is not None
            and self.supports("--model-draft")
            and self.speculative_status() == "available"
        ):
            draft = self._draft_model()
            if draft is not None and self.runtime is not None:
                draft_path = getattr(self.runtime, "_resolve", lambda p: p)(
                    draft.model_path)
                candidates.append(flags(
                    512, 256, physical,
                    ["--model-draft", str(draft_path),
                     "--draft-max", "16", "--draft-min", "4"]))
        return candidates

    def tuned_flags_heuristic_safe(self) -> list[str]:
        args: list[str] = []
        if self.supports("--flash-attn"):
            args += ["--flash-attn", "auto"]
        if self.supports("--cache-reuse"):
            args += ["--cache-reuse", "256"]
        args += ["--batch-size", "512", "--ubatch-size", "256"]
        return args

    def _measure(self, endpoint: str, prompt_tokens: int, gen_tokens: int, timeout: float) -> dict[str, Any]:
        prompt = "Explain local AI inference in detail. " * max(1, prompt_tokens // 8)
        payload = json.dumps({
            "model": "probe",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": gen_tokens,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        start = time.monotonic()
        ttft = None
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if ttft is None and (chunk.get("choices") or [{}])[0].get("delta", {}).get("content"):
                    ttft = time.monotonic() - start
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                if isinstance(chunk.get("timings"), dict):
                    timings = chunk["timings"]
        elapsed = time.monotonic() - start
        return {
            "ttft_ms": round((ttft or elapsed) * 1000, 1),
            "elapsed_seconds": round(elapsed, 3),
            "completion_tokens": int(usage.get("completion_tokens") or timings.get("predicted_n") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0),
            "prompt_per_second": float(timings.get("prompt_per_second") or 0.0),
            "predicted_per_second": float(timings.get("predicted_per_second") or 0.0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
        }

    @staticmethod
    def _pick_best(results: list[dict[str, Any]]) -> dict[str, Any] | None:
        ok = [r for r in results if r.get("ok")]
        if not ok:
            return None
        return max(ok, key=lambda r: (
            float((r.get("metrics") or {}).get("predicted_per_second") or 0.0),
            -float((r.get("metrics") or {}).get("ttft_ms") or 1e9),
        ))


# Recommended context windows per routing role — smaller KV cache for the
# fast lane, headroom for deep work. Applied at profile launch, not per call.
ROLE_CONTEXT_HINTS = {
    "utility": 8192,
    "fast_coder": 24576,
    "primary_coder": 24576,
    "deep_reasoner": 32768,
    "reviewer": 32768,
    "vision": 16384,
}


def recommended_context(profile: ModelProfile) -> int:
    hints = [ROLE_CONTEXT_HINTS.get(r, 16384) for r in profile.roles]
    hint = max(hints) if hints else 16384
    return min(profile.context_window, hint) if profile.context_window else hint
