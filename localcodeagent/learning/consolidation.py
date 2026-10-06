"""Sleep-cycle consolidation (Parts 5–7, 63–64).

A bounded pass over recent experience that:

- deduplicates/cluster lessons by signature,
- promotes repeated verified lessons toward SUPPORTED/VERIFIED,
- turns recurring success patterns into procedure candidates,
- surfaces contradictions (never erases them),
- marks stale records via FreshnessPolicy,
- preserves provenance: consolidated records keep source ids.

Runs on demand (/consolidate) or during idle under budget. Never runs
indefinitely — every cycle is capped by records, writes, and wall time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import taxonomy as t
from .freshness import FreshnessPolicy


@dataclass
class ConsolidationBudget:
    records: int = 400        # experience records examined per cycle
    writes: int = 60          # memory writes per cycle
    seconds: float = 20.0     # wall-clock cap
    model_calls: int = 0      # consolidation is deterministic — 0 by default

    def as_dict(self) -> dict:
        return {"records": self.records, "writes": self.writes,
                "seconds": self.seconds, "model_calls": self.model_calls}


@dataclass
class ConsolidationReport:
    examined: int = 0
    clustered: int = 0
    promoted: int = 0
    procedure_candidates: int = 0
    contradictions: int = 0
    expired: int = 0
    pruned: int = 0
    writes: int = 0
    notes: list[str] = field(default_factory=list)
    stopped_by_budget: bool = False
    took_s: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class ConsolidationEngine:
    def __init__(self, *, lessons, procedures, strategies=None,
                 knowledge_memory=None, freshness: FreshnessPolicy | None = None,
                 promotion_policy=None) -> None:
        self.lessons = lessons
        self.procedures = procedures
        self.strategies = strategies
        self.knowledge_memory = knowledge_memory
        self.freshness = freshness or FreshnessPolicy()
        self.policy = promotion_policy

    def run(self, budget: ConsolidationBudget | None = None,
            *, now: float | None = None) -> ConsolidationReport:
        budget = budget or ConsolidationBudget()
        started = time.time()
        report = ConsolidationReport()

        def over_budget() -> bool:
            return (report.examined >= budget.records
                    or report.writes >= budget.writes
                    or time.time() - started >= budget.seconds)

        # 1) Gather + cluster lessons. Exact-signature match is too
        # strict — the same problem phrased differently ("cmake link
        # error" vs "unresolved symbol again") still one experience.
        # Greedy Jaccard clustering on signature words within the same
        # problem class.
        clusters: list[list[dict]] = []
        sigs: list[set[str]] = []

        def words(rec: dict) -> set[str]:
            import re as _re
            sig = str(rec.get("signature") or "")
            return {w for w in _re.split(r"[^a-z0-9]+", sig.lower())
                    if len(w) > 3 and w not in
                    ("with", "does", "this", "that", "what", "have",
                     "from", "when", "where", "which", "another", "again")}

        for rec in self.lessons.recent(budget.records):
            report.examined += 1
            if over_budget():
                report.stopped_by_budget = True
                break
            rec_words = words(rec)
            placed = False
            for i, cluster in enumerate(clusters):
                if (cluster[0].get("problem_class")
                        != rec.get("problem_class")):
                    continue
                union = rec_words | sigs[i]
                jacc = (len(rec_words & sigs[i]) / len(union)
                        if union else 0.0)
                if jacc >= 0.4 or not rec_words:
                    cluster.append(rec)
                    sigs[i] |= rec_words
                    placed = True
                    break
            if not placed:
                clusters.append([rec])
                sigs.append(set(rec_words))

        # 2) Per cluster: repeated outcomes consolidate. Successes that
        #    recur feed procedure candidates; failures feed strategy
        #    demotion; mixed outcomes become CONFLICTED.
        for ci, recs in enumerate(clusters):
            if over_budget():
                report.stopped_by_budget = True
                break
            if len(recs) < 2:
                continue
            # Canonical signature for the cluster: problem class + the
            # words shared by most members — keeps procedure signatures
            # stable across phrasings.
            sig = str(recs[0].get("problem_class") or "general") + ":" + \
                " ".join(sorted(sigs[ci])[:6])
            report.clustered += len(recs)
            succ = [r for r in recs if r.get("outcome") == "success"]
            fail = [r for r in recs if r.get("outcome") == "failure"]

            if succ:
                # Repeated success → promote the latest lesson, register a
                # procedure candidate with provenance links.
                latest = succ[-1]
                n = len(succ)
                state = t.SUPPORTED if n >= 2 else t.CANDIDATE
                if n >= 3:
                    state = t.VERIFIED
                # Lessons without a promotion tag yet are promotable; a
                # TRUSTED/CONFLICTED record is never downgraded.
                cur = latest.get("promotion") or t.RAW
                promotable = {t.RAW, t.CANDIDATE, t.SUPPORTED, t.VERIFIED}
                if cur in promotable and cur != state:
                    self.lessons.update(latest["id"], promotion=state,
                                        consolidated_count=n)
                    report.promoted += 1
                    report.writes += 1
                cand = None
                for r in succ:
                    cand = self.procedures.propose_candidate(
                        sig, lesson_id=r["id"],
                        steps=[r.get("successful_strategy") or ""] or None)
                if cand and cand.get("promoted"):
                    report.procedure_candidates += 1
                if self.strategies and latest.get("successful_strategy"):
                    for r in succ:
                        self.strategies.record(
                            r.get("problem_class") or "general",
                            r["successful_strategy"], ok=True)
                    report.writes += 1

            if fail:
                # Failure memory: each failed strategy loses standing.
                latest = fail[-1]
                for r in fail:
                    for st in r.get("failed_strategies") or []:
                        if self.strategies:
                            self.strategies.record(
                                r.get("problem_class") or "general", st,
                                ok=False)
                self.lessons.update(latest["id"], consolidated_count=len(fail))
                report.writes += 1

            if succ and fail:
                # Same signature, divergent outcomes — contradiction is a
                # learning event; both sides stay on record.
                self.lessons.update(recs[-1]["id"], promotion=t.CONFLICTED,
                                    conflict_note=f"{len(succ)} success / "
                                                  f"{len(fail)} failure")
                report.contradictions += 1
                report.writes += 1

        # 3) Stale marking on knowledge memory — mark EXPIRED, don't delete.
        if self.knowledge_memory is not None and not over_budget():
            try:
                recs = getattr(self.knowledge_memory, "records", None)
                if recs is None and hasattr(self.knowledge_memory, "list"):
                    recs = self.knowledge_memory.list(limit=budget.records)
                for rec in (recs or []):
                    if over_budget():
                        report.stopped_by_budget = True
                        break
                    report.examined += 1
                    if self.freshness.is_stale(rec, now=now):
                        mark = getattr(self.knowledge_memory, "mark_expired", None)
                        if callable(mark):
                            mark(rec.get("id"))
                            report.expired += 1
                            report.writes += 1
            except Exception:
                pass

        report.took_s = round(time.time() - started, 3)
        return report
