"""Nexus Answer Memory — persistent learned Q&A over local experience.

Public facade used by the orchestrator and the HTTP API:

    lookup()              tier-1/2 retrieval (exact → semantic)
    record_experience()   called after every completed response
    apply_feedback()      thumbs/correction signals
    handle_command()      "learn this answer", "forget the answer for X", ...
    learn() / forget() / mark_incorrect() / edit() / merge() / refresh()
    stats() / list_answers() / export() / import_() / maintenance()

Everything is local. Failure of any part degrades to normal model routing —
the caller treats a raised exception or ``hit=None`` identically.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import confidence, embeddings, feedback, invalidation, learning, ttl, validation
from .normalization import normalize_answer_key, normalize_question
from .retrieval import MemoryMatch, Retriever
from .store import AnswerMemoryStore

log = logging.getLogger(__name__)


class AnswerMemory:
    def __init__(
        self,
        path: str,
        *,
        enabled: bool = True,
        semantic_enabled: bool = True,
        auto_learn: bool = True,
        auto_promote: bool = True,
        semantic_threshold: float = 0.50,
        possible_threshold: float = 0.30,
        max_experiences: int = 20000,
        retention_days: int = 90,
        max_db_mb: int = 256,
        workspace: str = "",
        config_fingerprint_fn: Callable[[], str] | None = None,
        repo_head_fn: Callable[[], str] | None = None,
        handlers: dict[str, Callable[[], str]] | None = None,
        brain_revision_fn: Callable[[], str] | None = None,
        profile_id_fn: Callable[[], str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.auto_learn = auto_learn
        self.auto_promote = auto_promote
        self.max_experiences = max_experiences
        self.retention_days = retention_days
        self.max_db_mb = max_db_mb
        self.workspace = workspace
        self._config_fp_fn = config_fingerprint_fn or (lambda: "")
        self._repo_head_fn = repo_head_fn or (lambda: "")
        self._brain_rev_fn = brain_revision_fn or (lambda: "")
        self._profile_id_fn = profile_id_fn or (lambda: "")
        self.handlers = dict(handlers or {})
        self._embedder = embeddings.embedder()
        self._last_hit: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.store: AnswerMemoryStore | None = None
        self.retriever: Retriever | None = None
        self.available = False
        self.error = ""
        if not enabled:
            return
        try:
            self.store = AnswerMemoryStore(path)
            self.retriever = Retriever(
                self.store,
                semantic_enabled=semantic_enabled,
                semantic_threshold=semantic_threshold,
                possible_threshold=possible_threshold,
            )
            self.available = True
            self._enforce_locked_identity()
        except Exception as exc:  # never take chat down with us
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("answer memory unavailable: %s", self.error)

    def _enforce_locked_identity(self) -> None:
        """Invalidate any stored answer targeting a creator-locked identity
        fact — they can only enter via imports/older versions, and the lock
        means they must never be served."""
        try:
            from ..identity import locked_topic
            rows = self.store.query(
                "SELECT id, canonical_question FROM answers WHERE invalidated=0"
            )
            for row in rows:
                if locked_topic(row.get("canonical_question", "")):
                    learning.invalidate_answer(
                        self.store, row["id"], "creator-locked identity fact"
                    )
        except Exception:
            pass  # enforcement failure must never take memory offline

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    # -- dependency fingerprints --------------------------------------------

    def _deps(self) -> dict[str, str]:
        return {
            "git_commit": _safe(self._repo_head_fn),
            "config_fingerprint": _safe(self._config_fp_fn),
            "brain_revision": _safe(self._brain_rev_fn),
        }

    # -- lookup --------------------------------------------------------------

    def lookup(
        self, question: str, *, project_id: str = "", record: bool = True
    ) -> MemoryMatch:
        """Try to answer without a model. Never raises.

        ``record=False`` peeks without touching hit/usage stats — used by the
        chat readiness gate so gating doesn't double-count the real lookup.
        """
        if not self.available or self.retriever is None:
            return MemoryMatch(reason="unavailable")
        started = time.monotonic()
        try:
            cacheability = validation.classify_cacheability(question)
            if cacheability in {"live", "task_specific", "transformation"}:
                return MemoryMatch(
                    kind="no_match", reason=f"cacheability={cacheability}",
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            match = self.retriever.lookup(
                question, project_id=project_id, profile_id=_safe(self._profile_id_fn)
            )
            if match.answer is not None:
                match = self._resolve_dependency(match, project_id)
                if match.hit and not confidence.may_bypass(match.answer):
                    # observed/candidate answers inform context, never bypass.
                    match = MemoryMatch(
                        kind="possible", answer=match.answer,
                        similarity=match.similarity,
                        context_answers=[match.answer],
                        reason=f"trust:{match.answer.get('trust_state')}",
                        latency_ms=match.latency_ms,
                    )
                if match.hit and match.answer is not None and record:
                    self._on_hit(match, question, project_id)
            match.latency_ms = match.latency_ms or (time.monotonic() - started) * 1000
            if record:
                self.store.bump("lookup_count")
                self.store.bump("lookup_latency_ms_total", match.latency_ms)
            return match
        except Exception as exc:
            log.warning("answer memory lookup failed: %s", exc)
            return MemoryMatch(reason=f"error:{type(exc).__name__}")

    def _resolve_dependency(self, match: MemoryMatch, project_id: str) -> MemoryMatch:
        row = match.answer
        if row is None:
            return match
        status = invalidation.dependency_status(
            row,
            config_fp=_safe(self._config_fp_fn),
            repo_head_sha=_safe(self._repo_head_fn),
        )
        if status == "current" or row.get("handler_key"):
            return match
        if not confidence.may_bypass(row):
            # Already non-bypassing — annotate only.
            match.reason = f"dependency:{status}"
            return match
        if status in {"stale", "expired"}:
            self.store.execute(
                "UPDATE answers SET trust_state='stale', invalidation_reason=? WHERE id=?",
                (f"dependency {status} — verification required", row["id"]),
            )
            self.store.bump("stale_revalidations")
            possible_min = self.retriever.possible_threshold if self.retriever else 0.7
            demoted = MemoryMatch(
                kind="possible" if match.similarity >= possible_min else "no_match",
                answer=row, similarity=match.similarity,
                context_answers=[row],
                reason=f"dependency:{status}",
                latency_ms=match.latency_ms,
            )
            return demoted
        return match

    def _on_hit(self, match: MemoryMatch, question: str, project_id: str) -> None:
        row = match.answer
        if row is None or self.store is None:
            return
        # Dynamic handler answers render fresh content at hit time.
        handler = self.handlers.get(row.get("handler_key") or "")
        if handler is not None:
            try:
                rendered = handler()
                if rendered.strip():
                    row["answer_text"] = rendered
            except Exception:
                pass
        self.store.defer(
            "UPDATE answers SET use_count=use_count+1, successful_use_count=successful_use_count+1,"
            " last_used_at=? WHERE id=?",
            (time.time(), row["id"]),
            invalidate=False,
        )
        if match.answer is not None and match.answer.get("_via_alias"):
            self.store.defer(
                "UPDATE aliases SET use_count=use_count+1 WHERE normalized_question=?",
                (normalize_question(question),),
                invalidate=False,
            )
        elif match.kind == "semantic":
            learning.add_alias(self.store, question=question, answer_id=row["id"])
        self.store.bump("memory_hits")
        self.store.bump(f"hits_{match.kind}")
        self.store.bump("model_calls_avoided")
        self._last_hit[project_id or "_"] = {
            "question": question,
            "canonical": row.get("canonical_question"),
            "match": match.kind,
            "similarity": match.similarity,
            "trust": row.get("trust_state"),
            "latency_ms": match.latency_ms,
        }

    def explain_last_hit(self, project_id: str = "") -> str:
        hit = self._last_hit.get(project_id or "_")
        if not hit:
            return "I did not answer the previous question from Answer Memory."
        return (
            f"I answered from Answer Memory because your question matched the learned "
            f"question '{hit['canonical']}' ({hit['match']} match, "
            f"{hit['similarity']:.0%} similarity, trust={hit['trust']}) in "
            f"{hit['latency_ms']:.0f} ms. Model inference was skipped."
        )

    # -- learning ------------------------------------------------------------

    def record_exchange(
        self,
        question: str,
        answer: str,
        *,
        conversation_id: str = "",
        response_id: str = "",
        model_id: str = "",
        model_role: str = "",
        inference_time_ms: float = 0.0,
        tools_used: list[str] | None = None,
        research_used: bool = False,
        sources: list[str] | None = None,
        project_id: str = "",
        project_scope: str = "global",
        repository: str = "",
        outcome: str = "",
        source_type: str = "model",
        response_source: str = "model",
    ) -> dict[str, Any]:
        """Record a completed exchange + promote into answers when eligible."""
        out: dict[str, Any] = {"experience_id": None, "answer_id": None, "suppressed": False}
        if not self.available or self.store is None or not self.auto_learn:
            return out
        if response_source == "answer_memory":
            return out  # a memory hit is not new evidence
        if validation.contains_secret(question) or validation.contains_secret(answer):
            out["suppressed"] = True
            self.store.bump("suppressed_experiences")
            return out
        try:
            deps = self._deps()
            profile_id = _safe(self._profile_id_fn)
            exp_id = learning.record_experience(
                self.store,
                question=question, answer=answer,
                conversation_id=conversation_id, response_id=response_id,
                model_id=model_id, model_role=model_role,
                inference_time_ms=inference_time_ms,
                tools_used=tools_used, research_used=research_used,
                sources=sources, project_id=project_id, repository=repository,
                git_commit=deps["git_commit"], brain_revision=deps["brain_revision"],
                outcome=outcome, profile_id=profile_id, embedder=self._embedder,
            )
            out["experience_id"] = exp_id
            if exp_id is None:
                return out
            cacheability = validation.classify_cacheability(question)
            if cacheability not in {"reusable", "contextual"}:
                return out
            normalized = normalize_question(question)
            existing = self.store.query_one(
                "SELECT * FROM answers WHERE normalized_question=? AND project_id=? "
                "AND profile_id IN ('', ?) ORDER BY confidence DESC LIMIT 1",
                (normalized, project_id, profile_id),
            )
            if existing is not None:
                learning.add_alias(self.store, question=question, answer_id=existing["id"])
                state = learning.bump_occurrence(
                    self.store, existing["id"], auto_promote=self.auto_promote
                )
                out.update(answer_id=existing["id"], trust_state=state)
                return out
            # Confirmed-paraphrase bridge: if a possible-band semantic match
            # exists and the fresh answer is equivalent to the stored one, the
            # new phrasing is a proven alias — learn it so the next equivalent
            # question resolves instantly instead of paying for inference again.
            if self.retriever is not None:
                try:
                    best, score, _near = self.retriever.semantic(
                        question, project_id, profile_id
                    )
                    if (
                        best is not None
                        and score >= self.retriever.possible_threshold
                        and confidence.usable(best)
                        and normalize_answer_key(best.get("answer_text", ""))
                        == normalize_answer_key(answer)
                    ):
                        learning.add_alias(
                            self.store, question=question, answer_id=best["id"]
                        )
                        state = learning.bump_occurrence(
                            self.store, best["id"],
                            positive=True, auto_promote=self.auto_promote,
                        )
                        out.update(
                            answer_id=best["id"], trust_state=state,
                            alias_confirmed=True,
                        )
                        return out
                except Exception:
                    pass
            initial_state = "verified" if source_type == "deterministic" else "observed"
            freshness = (
                "application_state" if source_type == "deterministic"
                else ttl.infer_freshness(question, cacheability)
            )
            if freshness == "live":
                return out
            ans_id = learning.upsert_answer(
                self.store,
                question=question, answer=answer,
                trust_state=initial_state, source_type=source_type,
                project_id=project_id, project_scope=project_scope,
                profile_id=profile_id,
                repository=repository, git_commit=deps["git_commit"],
                config_fingerprint=deps["config_fingerprint"],
                brain_revision=deps["brain_revision"],
                freshness=freshness, embedder=self._embedder,
            )
            out.update(answer_id=ans_id, trust_state=initial_state)
            self._maybe_prune()
            return out
        except Exception as exc:
            log.warning("answer memory record failed: %s", exc)
            return out

    def learn(
        self,
        question: str,
        answer: str,
        *,
        scope: str = "global",
        project_id: str = "",
        freshness: str = "user_defined",
        answer_type: str = "fact",
    ) -> dict[str, Any]:
        """Explicit user-approved learn → trusted answer."""
        if not self.available or self.store is None:
            return {"ok": False, "error": "answer memory unavailable"}
        from ..identity import locked_topic, locked_refusal
        topic = locked_topic(question)
        if topic:
            return {"ok": False, "error": locked_refusal(topic), "locked": topic}
        if validation.contains_secret(question) or validation.contains_secret(answer):
            return {"ok": False, "error": "refusing to persist a possible secret"}
        if freshness not in ttl.FRESHNESS_CLASSES:
            freshness = "user_defined"
        if scope not in {"global", "workspace", "project", "repository"}:
            scope = "global"
        deps = self._deps()
        ans_id = learning.upsert_answer(
            self.store, question=question, answer=answer,
            trust_state="trusted", source_type="user",
            project_id=project_id if scope != "global" else "",
            project_scope=scope,
            profile_id=_safe(self._profile_id_fn),
            git_commit=deps["git_commit"],
            config_fingerprint=deps["config_fingerprint"],
            brain_revision=deps["brain_revision"],
            freshness=freshness, answer_type=answer_type,
            embedder=self._embedder,
        )
        self.store.bump("manual_learns")
        return {"ok": True, "id": ans_id, "trust_state": "trusted"}

    def forget(self, *, answer_id: str = "", question: str = "") -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"ok": False}
        if question:
            from ..identity import locked_topic, locked_refusal
            topic = locked_topic(question)
            if topic:
                return {"ok": False, "error": locked_refusal(topic), "locked": topic}
        target = self._find(answer_id=answer_id, question=question)
        if target is None:
            return {"ok": False, "error": "no matching answer"}
        learning.invalidate_answer(self.store, target["id"], "user requested forget")
        return {"ok": True, "id": target["id"]}

    def mark_incorrect(
        self,
        *,
        answer_id: str = "",
        question: str = "",
        correction: str = "",
        conversation_id: str = "",
        project_id: str = "",
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"ok": False}
        exp_id = ""
        if conversation_id:
            exp = learning.last_experience(self.store, conversation_id)
            exp_id = exp["id"] if exp else ""
        if not question and exp_id:
            exp = self.store.query_one("SELECT * FROM experiences WHERE id=?", (exp_id,))
            question = exp["raw_question"] if exp else ""
        from ..identity import locked_topic, locked_refusal
        topic = locked_topic(question)
        if topic:
            return {"ok": False, "error": locked_refusal(topic), "locked": topic}
        result = learning.apply_correction(
            self.store,
            experience_id=exp_id,
            correction_text=correction or "marked incorrect by user",
            corrected_question=question,
            embedder=self._embedder,
            project_id=project_id,
            profile_id=_safe(self._profile_id_fn),
            auto_promote=self.auto_promote,
            **self._deps(),
        )
        if answer_id and not result.get("invalidated"):
            learning.invalidate_answer(self.store, answer_id, "marked incorrect")
            self.store.execute(
                "UPDATE answers SET correction_count=correction_count+1 WHERE id=?",
                (answer_id,),
            )
            result["invalidated"] = True
        result["ok"] = True
        return result

    def apply_feedback(
        self,
        rating: str,
        *,
        conversation_id: str = "",
        question: str = "",
        answer_id: str = "",
    ) -> dict[str, Any]:
        """Thumbs up/down and semantic feedback on the last exchange."""
        if not self.available or self.store is None:
            return {"ok": False}
        exp = learning.last_experience(self.store, conversation_id) if conversation_id else None
        if rating in {"up", "positive"}:
            if exp is not None:
                learning.apply_positive_feedback(
                    self.store, exp["id"], auto_promote=self.auto_promote,
                    profile_id=exp.get("profile_id") or _safe(self._profile_id_fn),
                )
            if answer_id:
                learning.bump_occurrence(self.store, answer_id, positive=True,
                                         auto_promote=self.auto_promote)
            self.store.bump("positive_feedback")
            return {"ok": True}
        if rating in {"down", "incorrect", "negative"}:
            result = self.mark_incorrect(
                answer_id=answer_id, question=question or (exp["raw_question"] if exp else ""),
                conversation_id=conversation_id,
            )
            self.store.bump("negative_feedback")
            return result
        return {"ok": False, "error": "unknown rating"}

    # -- natural-language commands -------------------------------------------

    def handle_command(
        self,
        text: str,
        *,
        conversation_id: str = "",
        project_id: str = "",
        last_exchange: tuple[str, str] | None = None,
    ) -> str | None:
        """Parse "learn this answer" style commands → response text or None."""
        if not self.available or self.store is None:
            return None
        cmd = feedback.parse_command(text)
        if cmd is None:
            return None
        op = cmd.get("op")
        try:
            if op == "learn":
                res = self.learn(cmd["question"], cmd["answer"], project_id=project_id)
                if res.get("ok"):
                    return (
                        f"Learned: when you ask \"{cmd['question']}\" I will answer "
                        f"\"{cmd['answer'][:160]}\". Stored as a trusted user-defined answer."
                    )
                return f"Could not learn that answer: {res.get('error', 'unknown error')}"
            if op == "replace":
                self.forget(question=cmd["question"])
                res = self.learn(cmd["question"], cmd["answer"], project_id=project_id)
                return (
                    f"Replaced the stored answer for \"{cmd['question']}\"."
                    if res.get("ok")
                    else f"Could not store the replacement: {res.get('error', 'unknown error')}"
                )
            if op == "learn_last":
                if not last_exchange or not last_exchange[0] or not last_exchange[1]:
                    return "There is no previous answer in this conversation for me to learn."
                res = self.learn(last_exchange[0], last_exchange[1], project_id=project_id)
                return (
                    "Learned the previous answer — future equivalent questions can be "
                    "answered from memory without loading a model."
                    if res.get("ok")
                    else f"Could not learn that answer: {res.get('error', 'unknown error')}"
                )
            if op == "forget":
                res = self.forget(question=cmd.get("question", ""))
                return (
                    "Forgot the stored answer. I will not reuse it."
                    if res.get("ok")
                    else "I could not find a stored answer matching that."
                )
            if op == "list":
                topic = cmd.get("topic") or ""
                rows = self.list_answers(query=topic, limit=10)
                if not rows:
                    return f"I have no learned answers{f' about {topic}' if topic else ''} yet."
                lines = [
                    f"Learned answers{f' about {topic}' if topic else ''} ({len(rows)}):"
                ]
                for r in rows[:10]:
                    lines.append(
                        f"• {r['canonical_question'][:80]} → {r['answer_text'][:120]} "
                        f"[{r['trust_state']}, used {r['use_count']}×]"
                    )
                return "\n".join(lines)
            if op == "explain":
                return self.explain_last_hit(project_id)
        except Exception as exc:
            log.warning("answer memory command failed: %s", exc)
            return None
        return None

    # -- admin / UI -----------------------------------------------------------

    def _find(self, *, answer_id: str = "", question: str = "") -> dict[str, Any] | None:
        if self.store is None:
            return None
        if answer_id:
            return self.store.query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
        if question:
            return self.store.query_one(
                "SELECT * FROM answers WHERE normalized_question=? AND invalidated=0 "
                "AND profile_id IN ('', ?) ORDER BY confidence DESC LIMIT 1",
                (normalize_question(question), _safe(self._profile_id_fn)),
            )
        return None

    def list_answers(
        self,
        *,
        query: str = "",
        trust: str = "",
        project_id: str = "",
        limit: int = 200,
        offset: int = 0,
        use_fts: bool = True,
    ) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        clauses, params = ["a.profile_id IN ('', ?)"], [_safe(self._profile_id_fn)]
        if trust:
            clauses.append("a.trust_state=?")
            params.append(trust)
        if project_id:
            clauses.append("(a.project_scope='global' OR a.project_id=?)")
            params.append(project_id)
        where = " AND ".join(clauses)
        used_fts = False
        if query and self.store.fts and use_fts:
            terms = " OR ".join(f'"{t}"' for t in query.split() if len(t) > 1)
            if terms:
                sql = (
                    "SELECT a.* FROM answers a "
                    "WHERE a.rowid IN (SELECT rowid FROM answers_fts WHERE answers_fts MATCH ?) "
                    f"AND {where}"
                )
                params = [terms, *params]
                used_fts = True
            else:
                sql = f"SELECT a.* FROM answers a WHERE {where}"
        else:
            sql = f"SELECT a.* FROM answers a WHERE {where}"
            if query:
                sql += " AND (a.canonical_question LIKE ? OR a.answer_text LIKE ?)"
                like = f"%{query}%"
                params.extend([like, like])
        sql += " ORDER BY (a.last_used_at IS NULL), a.last_used_at DESC, a.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        try:
            rows = self.store.query(sql, params)
            for row in rows:
                row.pop("embedding", None)  # blob — not JSON serializable
            return rows
        except Exception:
            if used_fts:
                # Fall back to LIKE if the FTS query failed for any reason.
                return self.list_answers(
                    query=query, trust=trust, project_id=project_id,
                    limit=limit, offset=offset, use_fts=False,
                )
            return []

    def edit(self, answer_id: str, *, answer_text: str = "", canonical_question: str = "",
             trust_state: str = "", freshness: str = "") -> dict[str, Any]:
        if self.store is None:
            return {"ok": False}
        row = self.store.query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
        if row is None:
            return {"ok": False, "error": "not found"}
        sets, params = ["updated_at=?"], [time.time()]
        if answer_text:
            sets += ["answer_text=?", "content_hash=?"]
            params += [answer_text.strip(), learning.content_hash(answer_text)]
        if canonical_question:
            from ..identity import locked_topic, locked_refusal
            topic = locked_topic(canonical_question)
            if topic:
                return {"ok": False, "error": locked_refusal(topic), "locked": topic}
            sets += ["canonical_question=?", "normalized_question=?"]
            params += [canonical_question.strip(), normalize_question(canonical_question)]
            try:
                sets.append("embedding=?")
                params.append(self._embedder.embed(canonical_question))
            except Exception:
                pass
        if trust_state in confidence.TRUST_ORDER:
            sets += ["trust_state=?", "confidence=?", "invalidated=?"]
            params += [trust_state, confidence.confidence_for(trust_state),
                       1 if trust_state == "invalidated" else 0]
        if freshness in ttl.FRESHNESS_CLASSES:
            sets += ["freshness=?", "expires_at=?"]
            params += [freshness, ttl.expiry_for(freshness)]
        params.append(answer_id)
        self.store.execute(f"UPDATE answers SET {', '.join(sets)} WHERE id=?", params)
        return {"ok": True}

    def merge(self, from_id: str, into_id: str) -> dict[str, Any]:
        if self.store is None:
            return {"ok": False}
        src = self.store.query_one("SELECT * FROM answers WHERE id=?", (from_id,))
        dst = self.store.query_one("SELECT * FROM answers WHERE id=?", (into_id,))
        if src is None or dst is None:
            return {"ok": False, "error": "not found"}
        self.store.execute(
            "UPDATE aliases SET answer_id=? WHERE answer_id=?", (into_id, from_id)
        )
        learning.add_alias(self.store, question=src["canonical_question"], answer_id=into_id)
        learning.add_alias(self.store, question=src["normalized_question"], answer_id=into_id)
        self.store.execute(
            "UPDATE answers SET use_count=use_count+?, occurrence_count=occurrence_count+?,"
            " successful_use_count=successful_use_count+? WHERE id=?",
            (src["use_count"], src["occurrence_count"], src["successful_use_count"], into_id),
        )
        learning.invalidate_answer(self.store, from_id, f"merged into {into_id}")
        return {"ok": True}

    def refresh(self, answer_id: str) -> dict[str, Any]:
        """Re-verify an answer against current dependencies → clears staleness."""
        if self.store is None:
            return {"ok": False}
        deps = self._deps()
        self.store.execute(
            "UPDATE answers SET trust_state='verified', confidence=?, last_verified_at=?,"
            " git_commit=?, config_fingerprint=?, invalidated=0, invalidation_reason='',"
            " updated_at=? WHERE id=?",
            (0.99, time.time(), deps["git_commit"], deps["config_fingerprint"],
             time.time(), answer_id),
        )
        return {"ok": True}

    def stats(self) -> dict[str, Any]:
        if self.store is None:
            return {"enabled": self.enabled, "available": False, "error": self.error}
        m = self.store.metrics()
        counts = self.store.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN trust_state IN ('trusted','verified') AND invalidated=0 THEN 1 ELSE 0 END) AS trusted,"
            " SUM(CASE WHEN trust_state='candidate' AND invalidated=0 THEN 1 ELSE 0 END) AS candidates,"
            " SUM(CASE WHEN trust_state='observed' AND invalidated=0 THEN 1 ELSE 0 END) AS observed,"
            " SUM(CASE WHEN trust_state='stale' THEN 1 ELSE 0 END) AS stale,"
            " SUM(invalidated) AS invalidated FROM answers"
        ) or {}
        exp = self.store.query_one(
            "SELECT COUNT(*) AS n, AVG(inference_time_ms) AS avg_ms FROM experiences"
        ) or {}
        lookups = m.get("lookup_count", 0)
        hits = m.get("memory_hits", 0)
        latency_count = m.get("lookup_count", 0) or 1
        avoided = int(m.get("model_calls_avoided", 0))
        est_saved = int(avoided * float(exp.get("avg_ms") or 0.0))
        return {
            "enabled": self.enabled,
            "available": self.available,
            "error": self.error,
            "corrupt_quarantine": self.store.corrupt_quarantine,
            "db_bytes": self.store.size_bytes(),
            "fts": self.store.fts,
            "embedder": self._embedder.id,
            "embedder_dim": self._embedder.dim,
            "answers": int(counts.get("total") or 0),
            "trusted": int(counts.get("trusted") or 0),
            "candidates": int(counts.get("candidates") or 0),
            "observed": int(counts.get("observed") or 0),
            "stale": int(counts.get("stale") or 0),
            "invalidated": int(counts.get("invalidated") or 0),
            "experiences": int(exp.get("n") or 0),
            "questions_seen": int(m.get("questions_seen", 0)),
            "lookups": int(lookups),
            "exact_hits": int(m.get("hits_exact", 0)),
            "semantic_hits": int(m.get("hits_semantic", 0)),
            "hit_rate": (hits / lookups) if lookups else 0.0,
            "model_calls_avoided": int(m.get("model_calls_avoided", 0)),
            "corrections": int(m.get("corrections", 0)),
            "stale_revalidations": int(m.get("stale_revalidations", 0)),
            "avg_lookup_ms": round(m.get("lookup_latency_ms_total", 0) / latency_count, 2),
            "est_time_saved_ms": est_saved,
            "suppressed_experiences": int(m.get("suppressed_experiences", 0)),
            "aliases": int((self.store.query_one("SELECT COUNT(*) AS n FROM aliases") or {}).get("n") or 0),
        }

    # -- export / import -------------------------------------------------------

    def export(self) -> dict[str, Any]:
        if self.store is None:
            return {"version": 1, "answers": []}
        answers = self.store.query("SELECT * FROM answers")
        for row in answers:
            row.pop("embedding", None)  # rebuildable — do not export blobs
        aliases = self.store.query("SELECT * FROM aliases")
        return {
            "version": 1,
            "embedder": self._embedder.id,
            "exported_at": time.time(),
            "answers": answers,
            "aliases": aliases,
        }

    def import_(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            return {"ok": False, "error": "unavailable"}
        imported = skipped = conflicts = 0
        from ..identity import locked_topic
        for row in payload.get("answers", []):
            if locked_topic(row.get("canonical_question") or row.get("normalized_question", "")):
                skipped += 1  # locked identity facts cannot be imported over
                continue
            if validation.contains_secret(row.get("canonical_question", "")) or \
               validation.contains_secret(row.get("answer_text", "")):
                skipped += 1
                continue
            existing = self._find(question=row.get("normalized_question") or row.get("canonical_question", ""))
            if existing is not None:
                if existing.get("content_hash") == row.get("content_hash"):
                    skipped += 1
                    continue
                if confidence.rank(existing["trust_state"]) >= confidence.rank(row.get("trust_state", "")):
                    conflicts += 1  # keep the stronger existing answer
                    continue
            learning.upsert_answer(
                self.store,
                question=row.get("canonical_question") or row.get("normalized_question", ""),
                answer=row.get("answer_text", ""),
                trust_state=row.get("trust_state", "candidate"),
                source_type="import",
                project_scope=row.get("project_scope", "global"),
                project_id=row.get("project_id", ""),
                profile_id=row.get("profile_id", ""),
                freshness=row.get("freshness", "static"),
                answer_type=row.get("answer_type", "fact"),
                topic=row.get("topic", ""),
                embedder=self._embedder,
            )
            imported += 1
        return {"ok": True, "imported": imported, "skipped": skipped, "conflicts": conflicts}

    # -- maintenance -----------------------------------------------------------

    def rebuild_index(self) -> dict[str, Any]:
        if self.store is None:
            return {"ok": False}
        rows = self.store.query("SELECT id, normalized_question, canonical_question FROM answers")
        n = 0
        for row in rows:
            try:
                self.store.execute(
                    "UPDATE answers SET embedding=?, embedding_model=? WHERE id=?",
                    (self._embedder.embed(row["canonical_question"] or row["normalized_question"]),
                     self._embedder.id, row["id"]),
                )
                n += 1
            except Exception:
                pass
        fts = self.store.rebuild_fts()
        return {"ok": True, "reembedded": n, "fts_rebuilt": fts}

    def clear(self, scope: str) -> dict[str, Any]:
        """scope: experiences | candidates | trusted | all"""
        if self.store is None:
            return {"ok": False}
        if scope == "experiences":
            self.store.execute("DELETE FROM experiences")
        elif scope == "candidates":
            self.store.execute(
                "DELETE FROM answers WHERE trust_state IN ('observed','candidate')"
            )
        elif scope == "trusted":
            self.store.execute(
                "DELETE FROM answers WHERE trust_state IN ('trusted','verified','stale')"
            )
        elif scope == "all":
            self.store.execute("DELETE FROM aliases")
            self.store.execute("DELETE FROM answers")
            self.store.execute("DELETE FROM experiences")
        else:
            return {"ok": False, "error": "unknown scope"}
        return {"ok": True}

    def _maybe_prune(self) -> None:
        if self.store is None:
            return
        try:
            cutoff = time.time() - self.retention_days * 86400
            self.store.execute("DELETE FROM experiences WHERE ts < ?", (cutoff,))
            count = (self.store.query_one("SELECT COUNT(*) AS n FROM experiences") or {}).get("n", 0)
            if count and count > self.max_experiences:
                self.store.execute(
                    "DELETE FROM experiences WHERE id IN (SELECT id FROM experiences"
                    " ORDER BY ts ASC LIMIT ?)",
                    (int(count - self.max_experiences),),
                )
            if self.max_db_mb and self.store.size_bytes() > self.max_db_mb * 1024 * 1024:
                self.store.execute(
                    "DELETE FROM experiences WHERE id IN (SELECT id FROM experiences"
                    " ORDER BY ts ASC LIMIT 2000)"
                )
        except Exception:
            pass


def _safe(fn) -> str:
    try:
        return str(fn() or "")
    except Exception:
        return ""
