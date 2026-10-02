"""Experience recording and trust promotion.

Experience Memory stores what happened. Trusted Answer Memory stores what may
be reused. Promotion between them is deliberately conservative: a single
model answer is ``observed``, repetition or explicit approval moves it up.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from . import confidence, embeddings, feedback, ttl, validation
from .normalization import normalize_answer_key, normalize_question


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_answer_key(text).encode("utf-8")).hexdigest()[:24]


def record_experience(
    store,
    *,
    question: str,
    answer: str,
    conversation_id: str = "",
    response_id: str = "",
    model_id: str = "",
    model_role: str = "",
    inference_time_ms: float = 0.0,
    tools_used: list[str] | None = None,
    research_used: bool = False,
    sources: list[str] | None = None,
    project_id: str = "",
    repository: str = "",
    git_commit: str = "",
    brain_revision: str = "",
    outcome: str = "",
    embedder=None,
) -> str | None:
    """Persist an experience. Returns experience id or None when suppressed."""
    if validation.is_noise(question) or not str(answer or "").strip():
        return None
    if validation.contains_secret(question) or validation.contains_secret(answer):
        return None  # secrets are never persisted into memory
    cacheability = validation.classify_cacheability(question)
    emb = None
    if cacheability in {"reusable", "contextual"} and embedder is not None:
        try:
            emb = embedder.embed(question)
        except Exception:
            emb = None
    exp_id = uuid.uuid4().hex[:16]
    store.execute(
        "INSERT INTO experiences(id, conversation_id, response_id, ts, raw_question,"
        " normalized_question, embedding, raw_answer, model_id, model_role,"
        " inference_time_ms, tools_used, research_used, sources, project_id,"
        " repository, git_commit, brain_revision, final_outcome, cacheability)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            exp_id, conversation_id, response_id, time.time(), question.strip(),
            normalize_question(question), emb, answer.strip(), model_id, model_role,
            float(inference_time_ms), json.dumps(tools_used or []),
            1 if research_used else 0, json.dumps(sources or []),
            project_id, repository, git_commit, brain_revision, outcome, cacheability,
        ),
    )
    store.bump("questions_seen")
    return exp_id


def upsert_answer(
    store,
    *,
    question: str,
    answer: str,
    trust_state: str,
    source_type: str,
    project_id: str = "",
    project_scope: str = "global",
    repository: str = "",
    git_commit: str = "",
    config_fingerprint: str = "",
    brain_revision: str = "",
    freshness: str = "static",
    answer_type: str = "fact",
    topic: str = "",
    handler_key: str = "",
    source_metadata: dict[str, Any] | None = None,
    embedder=None,
    now: float | None = None,
) -> str:
    """Insert or refresh a canonical answer; returns answer id."""
    now = now or time.time()
    normalized = normalize_question(question)
    existing = store.query_one(
        "SELECT * FROM answers WHERE normalized_question=? AND project_id=? "
        "ORDER BY confidence DESC LIMIT 1",
        (normalized, project_id),
    )
    if existing is None:
        alias = store.query_one(
            "SELECT answer_id FROM aliases WHERE normalized_question=?", (normalized,)
        )
        if alias:
            existing = store.query_one("SELECT * FROM answers WHERE id=?", (alias["answer_id"],))
    emb = None
    if embedder is not None:
        try:
            emb = embedder.embed(question)
        except Exception:
            pass
    if existing is not None:
        ans_id = existing["id"]
        new_state = (
            existing["trust_state"]
            if confidence.rank(existing["trust_state"]) > confidence.rank(trust_state)
            else trust_state
        )
        # A lower-trust source must not silently overwrite a trusted answer.
        overwrite = confidence.rank(trust_state) >= confidence.rank(
            existing["trust_state"]
        ) or source_type in {"user", "correction"}
        store.execute(
            "UPDATE answers SET answer_text=?, embedding=COALESCE(?,embedding),"
            " trust_state=?, confidence=?, source_type=?, source_metadata=?,"
            " updated_at=?, last_verified_at=?, expires_at=?, occurrence_count=occurrence_count+1,"
            " git_commit=?, config_fingerprint=?, brain_revision=?, content_hash=?,"
            " freshness=?, invalidated=0, invalidation_reason='' WHERE id=?",
            (
                answer.strip() if overwrite else existing["answer_text"],
                emb, new_state,
                max(float(existing["confidence"]), confidence.confidence_for(new_state)),
                source_type, json.dumps(source_metadata or {}), now, now,
                ttl.expiry_for(freshness, now), git_commit, config_fingerprint,
                brain_revision, content_hash(answer), freshness, ans_id,
            ),
        )
        return ans_id
    ans_id = uuid.uuid4().hex[:16]
    store.execute(
        "INSERT INTO answers(id, canonical_question, normalized_question, answer_text,"
        " embedding, embedding_model, topic, answer_type, confidence, trust_state,"
        " source_type, source_metadata, created_at, updated_at, last_verified_at,"
        " expires_at, project_scope, project_id, repository, git_commit,"
        " config_fingerprint, brain_revision, content_hash, freshness, handler_key)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ans_id, question.strip(), normalized, answer.strip(), emb,
            getattr(embedder, "id", "") if embedder else "", topic, answer_type,
            confidence.confidence_for(trust_state), trust_state, source_type,
            json.dumps(source_metadata or {}), now, now, now,
            ttl.expiry_for(freshness, now), project_scope, project_id,
            repository, git_commit, config_fingerprint, brain_revision,
            content_hash(answer), freshness, handler_key,
        ),
    )
    return ans_id


def add_alias(store, *, question: str, answer_id: str) -> None:
    normalized = normalize_question(question)
    if not normalized:
        return
    store.execute(
        "INSERT INTO aliases(normalized_question, answer_id, use_count, created_at)"
        " VALUES(?,?,0,?) ON CONFLICT(normalized_question) DO UPDATE SET use_count=use_count",
        (normalized, answer_id, time.time()),
    )


def bump_occurrence(store, answer_id: str, *, positive: bool = False, auto_promote: bool = True) -> str:
    row = store.query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
    if row is None:
        return ""
    occurrences = int(row.get("occurrence_count") or 0) + 1
    state = row["trust_state"]
    if auto_promote:
        promoted = confidence.promoted_state(occurrences, positive_feedback=positive)
        if promoted and confidence.rank(promoted) > confidence.rank(state):
            state = promoted
    store.execute(
        "UPDATE answers SET occurrence_count=?, trust_state=?, confidence=?, updated_at=? WHERE id=?",
        (occurrences, state, confidence.confidence_for(state, row["confidence"]), time.time(), answer_id),
    )
    return state


def invalidate_answer(store, answer_id: str, reason: str) -> None:
    store.execute(
        "UPDATE answers SET invalidated=1, trust_state='invalidated',"
        " invalidation_reason=?, updated_at=? WHERE id=?",
        (reason[:400], time.time(), answer_id),
    )


def apply_positive_feedback(store, experience_id: str, *, auto_promote: bool = True) -> None:
    exp = store.query_one("SELECT * FROM experiences WHERE id=?", (experience_id,))
    if exp is None:
        return
    store.execute(
        "UPDATE experiences SET user_feedback='positive' WHERE id=?", (experience_id,)
    )
    answer = store.query_one(
        "SELECT * FROM answers WHERE normalized_question=? AND invalidated=0",
        (exp["normalized_question"],),
    )
    if answer is not None:
        bump_occurrence(store, answer["id"], positive=True, auto_promote=auto_promote)


def apply_correction(
    store,
    *,
    experience_id: str,
    correction_text: str,
    corrected_question: str,
    embedder=None,
    project_id: str = "",
    project_scope: str = "global",
    auto_promote: bool = True,
    **deps,
) -> dict[str, Any]:
    """Invalidate the corrected answer; store the user-supplied replacement.

    A correction the user typed is an explicit instruction (same authority as
    ``learn``), so the replacement is stored as trusted — not a candidate.
    """
    exp = store.query_one("SELECT * FROM experiences WHERE id=?", (experience_id,))
    result: dict[str, Any] = {"invalidated": False, "promoted_answer_id": ""}
    if exp is not None:
        store.execute(
            "UPDATE experiences SET corrected=1, user_feedback='corrected',"
            " correction_text=? WHERE id=?",
            (correction_text, experience_id),
        )
        target_norm = exp["normalized_question"]
    else:
        target_norm = normalize_question(corrected_question)
    old = store.query_one(
        "SELECT * FROM answers WHERE normalized_question=? AND invalidated=0",
        (target_norm,),
    )
    if old is not None:
        invalidate_answer(store, old["id"], "user correction")
        store.execute(
            "UPDATE answers SET correction_count=correction_count+1 WHERE id=?",
            (old["id"],),
        )
        result["invalidated"] = True
    store.bump("corrections")
    replacement = feedback.correction_answer(correction_text)
    if replacement and not validation.contains_secret(replacement):
        freshness = ttl.infer_freshness(corrected_question, "reusable")
        result["promoted_answer_id"] = upsert_answer(
            store,
            question=corrected_question or (exp["raw_question"] if exp else ""),
            answer=replacement,
            trust_state="trusted",
            source_type="correction",
            project_id=project_id,
            project_scope=project_scope,
            embedder=embedder,
            freshness=freshness,
            **deps,
        )
    return result


def last_experience(store, conversation_id: str) -> dict[str, Any] | None:
    return store.query_one(
        "SELECT * FROM experiences WHERE conversation_id=? AND suppressed=0"
        " ORDER BY ts DESC LIMIT 1",
        (conversation_id,),
    )
