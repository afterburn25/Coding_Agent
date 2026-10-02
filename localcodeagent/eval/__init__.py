"""Nexus Evaluation Lab + experiment store (Parts G & O).

``EvalLab`` runs named benchmarks (a suite is a list of cases with a
runner callable) and persists every run to ``data/eval/history.jsonl``
with benchmark metrics — model/prompt/routing comparisons become
data, not impressions.

``ExperimentStore`` records controlled A/B experiments: hypothesis,
configuration, measured results per arm, and a conclusion.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..fsutil import atomic_write_text


class EvalLab:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.root / "history.jsonl"
        self._lock = threading.RLock()

    def run_suite(self, suite: str, cases: list[dict],
                  runner: Callable[[dict], dict],
                  *, subject: str = "", config: dict | None = None) -> dict:
        """Run cases through runner(case)->{'passed':bool,'score':float,
        'latency_s':float,'detail':str}. Persists the run."""
        results = []
        for case in cases:
            t0 = time.time()
            try:
                r = runner(case)
            except Exception as exc:
                r = {"passed": False, "score": 0.0,
                     "detail": f"{type(exc).__name__}: {exc}"}
            r.setdefault("latency_s", round(time.time() - t0, 3))
            r["case"] = case.get("id") or case.get("name") or "?"
            results.append(r)
        scores = [float(r.get("score", 1.0 if r.get("passed") else 0.0))
                  for r in results]
        lat = [float(r.get("latency_s", 0)) for r in results]
        run = {
            "id": f"ev-{uuid.uuid4().hex[:10]}",
            "suite": suite, "subject": subject,
            "config": dict(config or {}),
            "ts": time.time(),
            "cases": len(results),
            "passed": sum(1 for r in results if r.get("passed")),
            "mean_score": round(statistics.fmean(scores), 4) if scores else 0,
            "p50_latency_s": (round(statistics.median(lat), 3) if lat else 0),
            "results": results,
        }
        with self._lock:
            with self.history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(run, default=str) + "\n")
        return run

    def history(self, *, suite: str = "", subject: str = "",
                limit: int = 50) -> list[dict]:
        rows = []
        try:
            for line in self.history_path.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if suite and r.get("suite") != suite:
                    continue
                if subject and r.get("subject") != subject:
                    continue
                rows.append(r)
        except OSError:
            pass
        return rows[-limit:]

    def compare(self, suite: str, subject_a: str, subject_b: str) -> dict:
        """Compare the latest run of two subjects on the same suite."""
        a = self.history(suite=suite, subject=subject_a, limit=1)
        b = self.history(suite=suite, subject=subject_b, limit=1)
        if not a or not b:
            return {"ok": False, "error": "missing runs for comparison"}
        a, b = a[-1], b[-1]
        return {"ok": True, "suite": suite,
                "a": {"subject": subject_a, "score": a["mean_score"],
                      "latency": a["p50_latency_s"]},
                "b": {"subject": subject_b, "score": b["mean_score"],
                      "latency": b["p50_latency_s"]},
                "delta_score": round(b["mean_score"] - a["mean_score"], 4),
                "delta_latency": round(b["p50_latency_s"]
                                       - a["p50_latency_s"], 3)}


class ExperimentStore:
    """Controlled experiments: hypothesis → arms → measured results →
    conclusion. Reproducible because config is stored with the run."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "experiments.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._rows: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text("utf-8"))["experiments"]
        except (OSError, ValueError, KeyError):
            return []

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            {"version": 1, "experiments": self._rows[-300:]}, indent=2))

    def create(self, hypothesis: str, *, arms: list[dict],
               metric: str = "score") -> dict:
        exp = {"id": f"exp-{uuid.uuid4().hex[:10]}",
               "hypothesis": str(hypothesis)[:1000],
               "metric": metric,
               "arms": [{"name": a.get("name", f"arm{i}"),
                         "config": a.get("config", {}),
                         "eval_run_id": a.get("eval_run_id"),
                         "result": None} for i, a in enumerate(arms)],
               "conclusion": "", "status": "running",
               "created_at": time.time()}
        with self._lock:
            self._rows.append(exp)
            self._save()
        return dict(exp)

    def record_result(self, exp_id: str, arm: str, *,
                      eval_run_id: str = "", metrics: dict) -> bool:
        with self._lock:
            for e in self._rows:
                if e["id"] == exp_id:
                    for a in e["arms"]:
                        if a["name"] == arm:
                            a["result"] = dict(metrics)
                            a["eval_run_id"] = eval_run_id
                            self._save()
                            return True
        return False

    def conclude(self, exp_id: str, conclusion: str) -> bool:
        with self._lock:
            for e in self._rows:
                if e["id"] == exp_id:
                    e["conclusion"] = str(conclusion)[:2000]
                    e["status"] = "concluded"
                    e["concluded_at"] = time.time()
                    self._save()
                    return True
        return False

    def list(self, limit: int = 50) -> list[dict]:
        return [dict(e) for e in self._rows[-limit:]]
