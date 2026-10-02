"""Exact / lexical / semantic lookup over stored answers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import confidence, embeddings
from .normalization import (
    canonical_tokens,
    content_parts,
    content_tokens,
    is_action,
    is_concept,
    normalize_question,
    opposed_conflict,
    proper_tokens,
    token_set,
)


@dataclass(slots=True)
class MemoryMatch:
    kind: str = "no_match"  # exact | semantic | possible | no_match | suppressed
    answer: dict[str, Any] | None = None
    similarity: float = 0.0
    latency_ms: float = 0.0
    reason: str = ""
    context_answers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return self.answer is not None and self.kind in {"exact", "semantic"}


class Retriever:
    """Query path: exact normalized match → alias → semantic similarity → FTS."""

    def __init__(
        self,
        store,
        *,
        semantic_enabled: bool = True,
        semantic_threshold: float = 0.86,
        possible_threshold: float = 0.72,
        max_semantic_candidates: int = 4000,
    ) -> None:
        self.store = store
        self.semantic_enabled = semantic_enabled
        self.semantic_threshold = semantic_threshold
        self.possible_threshold = possible_threshold
        self.max_semantic_candidates = max_semantic_candidates
        self._embed_cache: list[tuple[str, Any]] | None = None

    # -- internals -----------------------------------------------------------

    def _answers(self, project_id: str) -> list[dict[str, Any]]:
        return [
            r for r in self.store.cached("answers")
            if not r.get("invalidated")
            and (r.get("project_scope") == "global" or r.get("project_id") == project_id)
        ]

    def _candidates(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._answers(project_id)
        rows.sort(key=lambda r: r.get("confidence") or 0.0, reverse=True)
        return rows[: self.max_semantic_candidates]

    def _embed_index(self, project_id: str) -> list[tuple[dict[str, Any], Any]]:
        rows = self._candidates(project_id)
        out = []
        for row in rows:
            vec = embeddings.decode(row.get("embedding"))
            if vec is not None:
                out.append((row, vec))
        return out

    # -- public --------------------------------------------------------------

    def exact(self, normalized: str, project_id: str) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for row in self._answers(project_id):
            if row.get("normalized_question") != normalized:
                continue
            if best is None or (row.get("confidence") or 0.0) > (best.get("confidence") or 0.0):
                best = row
        if best is not None:
            return best
        target = None
        for alias in self.store.cached("aliases"):
            if alias.get("normalized_question") == normalized:
                target = alias.get("answer_id")
                break
        if target is None:
            return None
        for row in self._answers(project_id):
            if row.get("id") == target:
                row = dict(row)
                row["_via_alias"] = True
                return row
        return None

    def _score_pair(self, query: str, stored_canonical: str, stored_normalized: str, cos: float) -> float:
        """Blended similarity with discriminative-token gating.

        Returns -1.0 when a hard gate fails (proper/digit/identifier mismatch
        or conflicting uncovered content words on both sides).
        """
        q_content = content_tokens(query)
        s_content = content_tokens(stored_canonical or stored_normalized)
        q_canon = canonical_tokens(query)
        s_canon = canonical_tokens(stored_canonical or stored_normalized)
        q_parts = content_parts(q_content)
        s_parts = content_parts(s_content)
        # Gate 1: discriminative tokens (proper nouns, digits, identifiers)
        # must be mutually covered — 'capital of France' ≠ 'capital of Italy'.
        pq = proper_tokens(query)
        ps = proper_tokens(stored_canonical or stored_normalized)
        if (pq - s_parts) or (ps - q_parts):
            return -1.0
        # Gate 2: opposite actions are never equivalent —
        # 'start ComfyUI' ≠ 'stop ComfyUI' even though the vocab overlaps.
        if opposed_conflict(q_canon, s_canon):
            return -1.0
        uncovered_q = q_canon - s_canon
        uncovered_s = s_canon - q_canon
        # Gate 3: a symmetric one-for-one content swap is a value substitution
        # ('image model' vs 'voice model') — block regardless of overlap.
        # Swaps that differ only in action verbs are paraphrase rewording,
        # not entity substitution, so they are exempt.
        if (
            uncovered_q and uncovered_s
            and len(uncovered_q) == len(uncovered_s) <= 2
            and not (all(is_action(t) for t in uncovered_q)
                     and all(is_action(t) for t in uncovered_s))
        ):
            return -1.0
        shared = q_canon & s_canon
        cov_q = len(shared) / len(q_canon) if q_canon else 0.0
        cov_s = len(shared) / len(s_canon) if s_canon else 0.0
        jaccard = len(shared) / len(q_canon | s_canon) if (q_canon or s_canon) else 0.0
        # Stored-question coverage matters most: a paraphrase may add words,
        # but the canonical question's substance must be expressed.
        blend = 0.40 * cos + 0.25 * jaccard + 0.35 * cov_s
        # Uncovered canonical content lowers confidence. Vocabulary terms
        # (soft concept words) cost little; unknown words cost more — they
        # may be entities the gate couldn't recognize.
        vocab_unc = sum(1 for t in uncovered_q | uncovered_s if is_concept(t))
        other_unc = len(uncovered_q | uncovered_s) - vocab_unc
        blend -= 0.02 * vocab_unc + 0.08 * other_unc
        return blend

    def semantic(self, text: str, project_id: str) -> tuple[dict[str, Any] | None, float, list[dict[str, Any]]]:
        """Best semantic match. Returns (row, score, near-miss context rows)."""
        if not self.semantic_enabled:
            return None, 0.0, []
        vec = embeddings.decode(embeddings.embedder().embed(text))
        if vec is None:
            return None, 0.0, []
        best: dict[str, Any] | None = None
        best_score = 0.0
        near: list[tuple[float, dict[str, Any]]] = []
        for row, rvec in self._embed_index(project_id):
            cos = embeddings.cosine(vec, rvec)
            score = self._score_pair(
                text,
                row.get("canonical_question") or "",
                row.get("normalized_question") or "",
                cos,
            )
            if score > best_score:
                best, best_score = row, score
            elif score >= self.possible_threshold:
                near.append((score, row))
        near.sort(key=lambda t: t[0], reverse=True)
        return best, best_score, [r for _s, r in near[:3]]

    def fts_search(self, text: str, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.store.fts:
            return []
        try:
            terms = " OR ".join(
                f'"{t}"' for t in token_set(text) if len(t) > 1
            ) or '""'
            return self.store.query(
                "SELECT a.* FROM answers_fts f JOIN answers a ON a.rowid=f.rowid "
                "WHERE answers_fts MATCH ? AND a.invalidated=0 "
                "AND (a.project_scope='global' OR a.project_id=?) LIMIT ?",
                (terms, project_id, limit),
            )
        except Exception:
            return []

    def lookup(
        self,
        question: str,
        *,
        project_id: str = "",
        now: float | None = None,
    ) -> MemoryMatch:
        started = time.monotonic()
        normalized = normalize_question(question)
        if not normalized:
            return MemoryMatch(reason="empty")

        row = self.exact(normalized, project_id)
        if row is not None:
            result = MemoryMatch(
                kind="exact", answer=row, similarity=1.0,
                latency_ms=(time.monotonic() - started) * 1000,
            )
            return result

        best, score, near = self.semantic(question, project_id)
        if best is not None and score >= self.semantic_threshold:
            return MemoryMatch(
                kind="semantic", answer=best, similarity=score,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        context = ([best] if best is not None else []) + [
            r for r in near if best is None or r.get("id") != best.get("id")
        ]
        context = [r for r in context if confidence.usable(r)][:3]
        if best is not None and score >= self.possible_threshold:
            return MemoryMatch(
                kind="possible", answer=best, similarity=score,
                context_answers=context,
                latency_ms=(time.monotonic() - started) * 1000,
                reason="below bypass threshold",
            )
        fts = self.fts_search(question, project_id)
        if fts:
            context = fts
        return MemoryMatch(
            kind="possible" if context else "no_match",
            context_answers=context[:3],
            similarity=score,
            latency_ms=(time.monotonic() - started) * 1000,
            reason="no confident match" if not context else "lexical candidates only",
        )
