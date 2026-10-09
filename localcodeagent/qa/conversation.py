"""Deterministic multi-turn conversation runner (backlog §5).

Drives AgentOrchestrator with a scripted model provider so behavior is
tested deterministically: what text reached the model, which context was
injected, what routing/tool decisions were made, and how the task closed
— not what a live model happened to say.

Per-turn assertions are plain dicts (see ASSERT KEYS below); failures are
reported per turn and optionally written to a FailureCorpus.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models.provider import ProviderResponse
from .corpus import CorpusEntry, FailureCorpus

# Assert keys evaluated per turn (all optional):
#   user_contains / user_not_contains      — last user message sent to model
#   system_contains / system_not_contains  — system prompt sent to model
#   response_contains / not_contains       — assistant result content
#   source                                 — result.response_source
#   task_status                            — result.task["status"] (default "completed")
#   tool_calls / no_tool_calls             — provider received/omitted tools=...
#   context_contains / not_contains        — any message (any role) content
#   memory_contains / not_contains         — durable store's injected
#                                            block for this query (no
#                                            transcript contamination)
#   max_sentences                          — response sentence budget
#   max_chars                              — response length budget
#   no_leading_filler                      — no stock opener ("Let me
#                                          think", "I hear you", "Certainly")
#   no_reasoning_narration                 — no "counting from...",
#                                          "I determined that" narration
#   no_trailing_question                   — response must not end with "?"
#   scope                                  — expected context/scope depth
#                                          ("exact", "brief", "explanatory",
#                                          "detailed", "open_ended")
# Values may be a string or list of strings.


@dataclass
class QaTurn:
    text: str
    conversation_id: str = ""
    expect: dict[str, Any] = field(default_factory=dict)


@dataclass
class QaScenario:
    scenario_id: str
    turns: list[QaTurn]
    seed: int = 0
    default_conversation_id: str = "qa-chat"


@dataclass
class TurnResult:
    index: int
    text: str
    conversation_id: str
    failures: list[str]
    user_content: str = ""
    system_content: str = ""
    all_content: str = ""
    response: str = ""
    response_source: str = ""
    task_status: str = ""
    tool_calls: int = 0
    elapsed_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class QaRunResult:
    scenario_id: str
    turns: list[TurnResult]

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        for turn in self.turns:
            out.extend(f"turn {turn.index} ({turn.text!r}): {f}" for f in turn.failures)
        return out

    @property
    def ok(self) -> bool:
        return not any(t.failures for t in self.turns)


def aggregate_metrics(runs: list[QaRunResult]) -> dict[str, Any]:
    """Cross-run conversation-quality report — the measurable
    dimensions the response-scope milestone tracks.

    Component rates stay separate so a regression names its own cause:
    a rising ``filler_rate`` and a rising ``over_budget_rate`` are
    different defects even when both hurt the aggregate.
    """
    turns = [tr for run in runs for tr in run.turns]
    answered = [t for t in turns if t.response]
    if not turns:
        return {"turns": 0}
    lat = sorted(t.elapsed_ms for t in turns)

    def _rate(pred) -> float:
        return round(100.0 * len([t for t in answered if pred(t)])
                     / max(1, len(answered)), 2)

    return {
        "turns": len(turns),
        "answered": len(answered),
        "model_calls": len([t for t in turns if t.all_content]),
        "builtin_answers": len([
            t for t in turns
            if not t.all_content and t.response]),
        "median_latency_ms": round(lat[len(lat) // 2], 1) if lat else 0.0,
        "p90_latency_ms": round(lat[int(len(lat) * 0.9)], 1)
        if lat else 0.0,
        "median_chars": round(
            sorted(len(t.response) for t in answered)[
                len(answered) // 2], 1) if answered else 0.0,
        "median_sentences": round(
            sorted(int(t.metrics.get("sentences") or 0)
                   for t in answered)[len(answered) // 2], 1)
            if answered else 0.0,
        "filler_rate": _rate(lambda t: t.metrics.get("leading_filler")),
        "reasoning_narration_rate": _rate(
            lambda t: t.metrics.get("reasoning_narration")),
        "trailing_question_rate": _rate(
            lambda t: t.metrics.get("trailing_question")),
        "over_budget_rate": _rate(
            lambda t: t.metrics.get("over_budget")),
        "depth_histogram": {
            d: len([t for t in turns
                    if t.metrics.get("depth") == d])
            for d in ("exact", "brief", "explanatory", "detailed",
                      "open_ended")
        },
    }


class ScriptedProvider:
    """Model provider that replays scripted answers and records calls.

    `script` may be a list of response strings (indexed by call) or a
    callable(messages, call_index) -> str. After the script runs out the
    provider returns `default`.
    """

    def __init__(self, script=None, default: str = "Here is the answer.") -> None:
        self.script = script if script is not None else []
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def _answer(self, messages) -> str:
        idx = len(self.calls)
        if callable(self.script):
            return str(self.script(messages, idx))
        if isinstance(self.script, (list, tuple)) and idx < len(self.script):
            return str(self.script[idx])
        return self.default

    def complete(self, *, messages, tools=None, max_tokens=None,
                 tool_choice=None):
        content = self._answer(messages)
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,
        })
        return ProviderResponse(
            message={"role": "assistant", "content": content}, raw={})

    # Observation helpers ------------------------------------------------
    @property
    def last_user_content(self) -> str:
        if not self.calls:
            return ""
        for msg in reversed(self.calls[-1]["messages"]):
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
        return ""

    @property
    def last_system_content(self) -> str:
        if not self.calls:
            return ""
        return "\n".join(
            str(m.get("content", "")) for m in self.calls[-1]["messages"]
            if m.get("role") == "system")

    @property
    def last_all_content(self) -> str:
        if not self.calls:
            return ""
        return "\n".join(
            str(m.get("content", "")) for m in self.calls[-1]["messages"])


class ConversationQaRunner:
    """Runs scenarios through an AgentOrchestrator and evaluates asserts."""

    def __init__(self, agent, provider: ScriptedProvider, *,
                 conversation_manager=None,
                 corpus: FailureCorpus | None = None) -> None:
        self.agent = agent
        self.provider = provider
        self.conversation_manager = conversation_manager
        self.corpus = corpus
        self._conversations: dict[str, str] = {}

    def _activate(self, logical_id: str) -> str:
        """Map a logical scenario conversation id onto a real managed chat."""
        mgr = self.conversation_manager
        if mgr is None or not logical_id:
            return ""
        real = self._conversations.get(logical_id)
        if real is None:
            real = str(mgr.create(title=f"qa:{logical_id}").get("id") or "")
            self._conversations[logical_id] = real
        active = str(mgr.active().get("id") or "")
        if active != real:
            mgr.set_active(real)
        return real

    def run(self, scenario: QaScenario) -> QaRunResult:
        results: list[TurnResult] = []
        history: list[dict[str, str]] = []
        calls_before = len(self.provider.calls)
        for index, turn in enumerate(scenario.turns):
            cid = turn.conversation_id or scenario.default_conversation_id
            self._activate(cid)
            # Mirror the server lane: agent.run(history=...) carries the
            # active conversation's transcript so follow-ups can resolve.
            try:
                hist = (self.conversation_manager.history(limit=32)
                        if self.conversation_manager is not None else [])
            except Exception:
                hist = []
            started = time.perf_counter()
            failure: list[str] = []
            result = None
            try:
                result = self.agent.run(turn.text, history=hist)
            except Exception as exc:  # noqa: BLE001 - failures are the signal
                failure.append(f"unexpected_error: {exc!r}")
            elapsed = (time.perf_counter() - started) * 1000.0
            called = len(self.provider.calls) > calls_before
            calls_before = len(self.provider.calls)

            tr = TurnResult(
                index=index, text=turn.text, conversation_id=cid,
                failures=failure, elapsed_ms=elapsed)
            if result is not None:
                tr.response = str(getattr(result, "content", "") or "")
                tr.response_source = str(getattr(result, "response_source", "") or "")
                task = getattr(result, "task", {}) or {}
                tr.task_status = str(task.get("status", ""))
            # Scope metrics — measured on the final response; assert keys
            # below turn them into failures. These diagnose scope
            # behavior; they never rewrite the answer.
            try:
                from ..context.scope import (
                    classify_scope, scope_metrics)
                tr.metrics = scope_metrics(
                    tr.response, classify_scope(turn.text))
            except Exception:
                tr.metrics = {}
            if called:
                tr.user_content = self.provider.last_user_content
                tr.system_content = self.provider.last_system_content
                tr.all_content = self.provider.last_all_content
                tr.tool_calls = int(self.provider.calls[-1].get("tools") is not None)
            history.append({"role": "user", "content": turn.text})
            history.append({"role": "assistant", "content": tr.response})
            failure.extend(self._evaluate(turn, tr, called))
            results.append(tr)

        run = QaRunResult(scenario.scenario_id, results)
        if self.corpus is not None and not run.ok:
            self._record(scenario, run, history)
        return run

    # ------------------------------------------------------------------
    def _evaluate(self, turn: QaTurn, tr: TurnResult, called: bool) -> list[str]:
        expect = turn.expect or {}
        out: list[str] = []
        status = expect.get("task_status", "completed")
        if tr.task_status and tr.task_status != status:
            out.append(f"task_status: expected {status!r}, got {tr.task_status!r}")

        def _need(key, haystack, label, negate=False):
            vals = expect.get(key)
            if not vals:
                return
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                found = str(v).lower() in haystack.lower()
                if negate and found:
                    out.append(f"{label}: unexpected {v!r} present")
                elif not negate and not found:
                    out.append(f"{label}: missing {v!r}")

        _need("user_contains", tr.user_content, "user_content")
        _need("user_not_contains", tr.user_content, "user_content", negate=True)
        _need("system_contains", tr.system_content, "system")
        _need("system_not_contains", tr.system_content, "system", negate=True)
        _need("context_contains", tr.all_content, "context")
        _need("context_not_contains", tr.all_content, "context", negate=True)
        _need("response_contains", tr.response, "response")
        _need("response_not_contains", tr.response, "response", negate=True)

        # Memory-side asserts — the durable store's injected block for
        # THIS query, independent of transcript history (which may
        # legitimately contain obsolete values the user just corrected).
        if "memory_contains" in expect or "memory_not_contains" in expect:
            mem_block = ""
            mem = getattr(self.agent, "conversation_memory", None)
            if mem is not None:
                try:
                    mem_block = str(mem.prompt_context(turn.text) or "")
                except Exception:
                    mem_block = ""
            _need("memory_contains", mem_block, "memory")
            _need("memory_not_contains", mem_block, "memory", negate=True)

        if "source" in expect and tr.response_source != expect["source"]:
            out.append(f"source: expected {expect['source']!r}, got {tr.response_source!r}")
        if expect.get("tool_calls") and not tr.tool_calls:
            out.append("tool_calls: expected provider to receive tools")
        if expect.get("no_tool_calls") and tr.tool_calls:
            out.append("no_tool_calls: provider received tools unexpectedly")

        # --- scope metrics asserts (context/scope.py) -----------------
        m = tr.metrics or {}
        if "max_sentences" in expect:
            got = int(m.get("sentences") or 0)
            if got > int(expect["max_sentences"]):
                out.append(
                    f"max_sentences: expected <={expect['max_sentences']}, "
                    f"got {got}")
        if "max_chars" in expect:
            got = int(m.get("chars") or 0)
            if got > int(expect["max_chars"]):
                out.append(
                    f"max_chars: expected <={expect['max_chars']}, got {got}")
        if expect.get("no_leading_filler") and m.get("leading_filler"):
            out.append(
                f"no_leading_filler: response opens with "
                f"{m['leading_filler']!r}")
        if expect.get("no_reasoning_narration") \
                and m.get("reasoning_narration"):
            out.append(
                f"no_reasoning_narration: found "
                f"{m['reasoning_narration']!r}")
        if expect.get("no_trailing_question") \
                and m.get("trailing_question"):
            out.append("no_trailing_question: response ends with '?'")
        if "scope" in expect and m.get("depth") != expect["scope"]:
            out.append(
                f"scope: expected depth {expect['scope']!r}, "
                f"classified {m.get('depth')!r}")
        return out

    def _record(self, scenario: QaScenario, run: QaRunResult,
                history: list[dict[str, str]]) -> None:
        for tr in run.turns:
            for fail in tr.failures:
                category = fail.split(":", 1)[0].strip()
                if category not in (
                    "task_status", "user_content", "system", "context",
                    "response", "source", "tool_calls", "no_tool_calls",
                    "unexpected_error",
                ):
                    category = "unexpected_error"
                self.corpus.record(CorpusEntry(
                    category={
                        "task_status": "task_not_completed",
                        "no_tool_calls": "unexpected_tool_call",
                        "tool_calls": "bad_tool_choice",
                    }.get(category, category),
                    conversation=list(history),
                    failed_turn_index=tr.index,
                    user_text=tr.text,
                    expected=str((scenario.turns[tr.index].expect or {})),
                    actual=fail,
                    seed=scenario.seed,
                    scenario_id=scenario.scenario_id,
                ))


# ----------------------------------------------------------------------
# Seeded scenario generation

_POOL: dict[str, list[str]] = {
    "everyday": [
        "what's a good weeknight dinner I can make in 20 minutes",
        "how do I get a coffee stain out of a shirt",
        "what's the weather usually like in Lisbon in April",
        "recommend a book like The Name of the Wind",
    ],
    "science": [
        "why is the sky blue",
        "explain the difference between viruses and bacteria",
        "what does the liver actually do",
        "how do vaccines train the immune system",
    ],
    "history": [
        "what caused the fall of the Roman Empire",
        "who was the first person to circumnavigate the globe",
        "what was the printing press's effect on Europe",
    ],
    "technology": [
        "what's the difference between TCP and UDP",
        "how does HTTPS keep data private",
        "what is a GPU bottleneck",
        "explain what an operating system kernel does",
    ],
    "programming": [
        "what is the difference between a list and a tuple in python",
        "explain what a race condition is",
        "what does 'idempotent' mean for an API",
        "how do I reverse a string in python",
    ],
    "math": [
        "what is 17 times 24",
        "how do I compute compound interest",
        "explain what a prime number is",
        "what's the derivative of x squared",
    ],
    "cooking": [
        "give me a recipe for crawfish feticcinii",
        "how do I make chiken alfredo from scratch",
        "what can I substitute for buttermilk in a recipie",
        "how long should I rest steak after cooking",
    ],
    "writing": [
        "help me write a thank-you note to my neighbor",
        "give me a one-sentence tagline for a hiking app",
        "how do I make this sentence less wordy",
    ],
    "troubleshooting": [
        "my laptop fan runs loud even when idle, what gives",
        "wifi keeps dropping every few minutes",
        "my printer says offline but it's on",
    ],
    "comparisons": [
        "compare postgres and sqlite for a small desktop app",
        "electric vs gas cars for a 15 mile commute",
        "is a standing desk actually better",
    ],
    "hypothetical": [
        "what would happen if the moon disappeared",
        "if you could only keep one app on your phone which would it be",
        "what if gravity were twice as strong",
    ],
    "typos": [
        "whats the wether like this week",
        "how much ram dose a browser use",
        "chek the recpie for missing steps",
        "wich is faster, lists or arrays",
    ],
    "topic_switch": [
        "anyway, changing the subject — what's for dinner",
        "ok forget that, tell me something about jazz",
        "different question — how do I back up my photos",
    ],
    "references": [
        "tell me more about that",
        "what about the second one",
        "go back to what we were talking about",
        "can you explain that differently",
    ],
    "corrections": [
        "no, I meant the previous one",
        "that's not quite right, try again",
        "actually I wanted the other option",
    ],
    "memory_teach": [
        "remember that my favorite editor is neovim",
        "Project Meridian uses MongoDB",
        "remember that I prefer metric units",
    ],
    "memory_recall": [
        "what editor did I say I like",
        "what database does Meridian use",
        "do I prefer metric or imperial",
    ],
}

_GENERIC_EXPECT = {"task_status": "completed", "no_tool_calls": True}


def generate_scenarios(
    seed: int,
    *,
    turns: int = 10,
    count: int = 1,
    categories: list[str] | None = None,
    scenario_prefix: str = "gen",
) -> list[QaScenario]:
    """Deterministically generate scenarios by mixing utterance pools.

    Reproducible by seed — the same seed produces the same scenarios,
    so a generated failure can be replayed verbatim as a regression.
    """
    rng = random.Random(seed)
    cats = categories or list(_POOL)
    scenarios: list[QaScenario] = []
    for i in range(max(1, int(count))):
        seq: list[QaTurn] = []
        for _ in range(max(1, int(turns))):
            cat = rng.choice(cats)
            text = rng.choice(_POOL[cat])
            seq.append(QaTurn(text=text, expect=dict(_GENERIC_EXPECT)))
        scenarios.append(QaScenario(
            scenario_id=f"{scenario_prefix}-{seed}-{i}",
            turns=seq, seed=seed,
            default_conversation_id=f"{scenario_prefix}-chat-{seed}-{i}",
        ))
    return scenarios


def pool_categories() -> list[str]:
    return list(_POOL)


# ----------------------------------------------------------------------
# §24 — composed hard-pattern generation
#
# generate_scenarios mixes single utterances with generic asserts; these
# patterns compose structured sequences across conversations with
# assertions that actually catch behavior failures: supersession,
# intrusion control, long-distance recall, interruption return.

_HARD_FACT_SUBJECTS = ["Orion", "Meridian", "Atlas", "Vega", "Lumen"]
_HARD_FACT_PREDS = [
    ("database", "PostgreSQL", "SQLite", "what database does %s use"),
    ("language", "Python", "Rust", "what language is %s written in"),
    ("deploy target", "Kubernetes", "bare metal", "where does %s deploy"),
]
_HARD_DISTRACTORS = (
    _POOL["everyday"] + _POOL["science"] + _POOL["technology"])


def generate_hard_scenarios(
    seed: int,
    *,
    count: int = 4,
    scenario_prefix: str = "hard",
) -> list[QaScenario]:
    """Composed difficulty patterns — each is a real failure class:

    - supersession: teach → supersede → recall must yield the NEW value
      and never inject the old one (context_not_contains old value).
    - intrusion: teach fact A + unrelated fact B, then query A — B's
      value must not ride the prompt.
    - long_distance: teach a fact, bury it under N distractor turns in a
      different conversation, then recall in the original chat.
    - interruption: topic A turns → topic switch → "go back to what we
      were talking about" — the return turn's prompt must still carry
      conversation context (non-empty user content), and the run must
      complete without errors.
    """
    rng = random.Random(seed)
    out: list[QaScenario] = []
    for i in range(max(1, int(count))):
        subject = rng.choice(_HARD_FACT_SUBJECTS)
        pred, old_v, new_v, query_fmt = rng.choice(_HARD_FACT_PREDS)
        project = f"Project {subject}"
        query = query_fmt % project

        # Pattern 1 — supersession: teach + switch in s1a, recall in s1b.
        # s1b's own history never mentions either value, so a hit on
        # context means the durable memory record won — and the superseded
        # value must stay inactive.
        out.append(QaScenario(f"{scenario_prefix}-supersede-{i}", [
            QaTurn(f"{project} uses {old_v}.", conversation_id="s1a"),
            QaTurn(f"we switched {project} to {new_v}.", conversation_id="s1a"),
            QaTurn(query, conversation_id="s1b", expect={
                "context_contains": new_v,
                "context_not_contains": old_v,
                "task_status": "completed",
            }),
        ], seed=seed))

        # Pattern 2 — intrusion: unrelated fact in ANOTHER chat must not
        # ride this prompt (s2's history legitimately contains only its
        # own turns — the distractor lives in s2b's transcript).
        other = rng.choice([s for s in _HARD_FACT_SUBJECTS
                            if s != subject])
        distractor_fact = f"Project {other} uses Fortran."
        out.append(QaScenario(f"{scenario_prefix}-intrusion-{i}", [
            QaTurn(f"{project} uses {new_v}.", conversation_id="s2"),
            QaTurn(distractor_fact, conversation_id="s2b"),
            QaTurn(query, conversation_id="s2", expect={
                "context_contains": new_v,
                "context_not_contains": "Fortran",
            }),
        ], seed=seed))

        # Pattern 3 — cross-chat recall: teach in s3a, bury the memory
        # under distractor turns elsewhere, recall in s3b. context must
        # carry the fact even though s3b's own history never mentions it.
        turns: list[QaTurn] = [
            QaTurn(f"{project} uses {new_v}.", conversation_id="s3a"),
        ]
        for _ in range(8):
            turns.append(QaTurn(rng.choice(_HARD_DISTRACTORS),
                                conversation_id="s3c"))
        turns.append(QaTurn(query, conversation_id="s3b", expect={
            "context_contains": new_v,
        }))
        out.append(QaScenario(f"{scenario_prefix}-distance-{i}",
                              turns, seed=seed))

        # Pattern 4 — interruption and return.
        out.append(QaScenario(f"{scenario_prefix}-interrupt-{i}", [
            QaTurn("explain how photosynthesis works",
                   conversation_id="s4"),
            QaTurn("what wavelengths do plants absorb",
                   conversation_id="s4"),
            QaTurn("anyway — what's a good pasta shape for alfredo",
                   conversation_id="s4"),
            QaTurn("go back to what we were talking about",
                   conversation_id="s4", expect={
                       # The model must see the interrupted topic in the
                       # transcript — a return-without-history is the
                       # failure this pattern exists to catch.
                       "context_contains": "photosynthesis",
                       "task_status": "completed",
                   }),
        ], seed=seed))
    return out


# ----------------------------------------------------------------------
# Response-scope scenarios (context/scope.py milestone)
#
# Composed multi-turn sequences that test the reveal contract: requested
# facts appear, supporting facts stay hidden, disclosure is progressive,
# superseded values never resurface, and exact answers carry no filler.
# ----------------------------------------------------------------------

def generate_scope_scenarios(
    *,
    scenario_prefix: str = "scope",
) -> list[QaScenario]:
    """Fixed scope-ladder scenarios — permanent regressions, not seeds.

    Deterministic identity answers mean these assert the REAL shipped
    contract end-to-end (no scripted model involved on the identity
    turns — they bypass the provider entirely).
    """
    out: list[QaScenario] = []

    # The observed defect — permanent regression. Age question reveals
    # ONLY the age; birthday/calculation/filler are all forbidden.
    out.append(QaScenario(f"{scenario_prefix}-age-ladder", [
        QaTurn("how old are you?", conversation_id="scope-a", expect={
            "response_contains": "old",
            "response_not_contains": [
                "september 30", "birthday", "born", "counting",
                "let me think", "i hear you", "sept 30"],
            "max_sentences": 2,
            "no_leading_filler": True,
            "no_reasoning_narration": True,
            "no_trailing_question": True,
            "task_status": "completed",
        }),
        # Disclosure is progressive — the same fact revealed on request.
        QaTurn("when is your birthday?", conversation_id="scope-a",
               expect={
                   "response_contains": "september 30",
                   "response_not_contains": ["days old", "years old",
                                             "months old"],
                   "no_trailing_question": True,
               }),
        # And the derivation on request — reasoning becomes content.
        QaTurn("how did you calculate your age?",
               conversation_id="scope-a", expect={
                   "response_contains": "september 30",
               }),
    ]))

    # Persona disclosure ladder — existence → name → open invitation.
    out.append(QaScenario(f"{scenario_prefix}-father-ladder", [
        # Existence is the requested fact — the name is the next rung.
        QaTurn("do you have a father?", conversation_id="scope-f",
               expect={
                   "response_not_contains": [
                       "john hamburn", "john", "hamburn", "nexus core"],
                   "max_sentences": 2,
                   "no_trailing_question": True,
               }),
        QaTurn("who is your father?", conversation_id="scope-f", expect={
            "response_contains": "john hamburn",
            "max_sentences": 3,
            "no_trailing_question": True,
        }),
        QaTurn("tell me about your father", conversation_id="scope-f",
               expect={
                   "response_contains": ["nexus core", "built"],
                   "task_status": "completed",
               }),
    ]))

    # Memory single-fact recall — teach facts across separate chats so
    # the query chat's transcript carries only the question; whatever
    # reaches the model comes from durable memory retrieval. The
    # relevance gate must surface the color and keep steak/GPU off.
    out.append(QaScenario(f"{scenario_prefix}-memory-scope", [
        QaTurn("my favorite color is blue", conversation_id="scope-m1"),
        QaTurn("my favorite food is steak", conversation_id="scope-m2"),
        QaTurn("i have an rtx 3080", conversation_id="scope-m3"),
        QaTurn("what's my favorite color?", conversation_id="scope-m4",
               expect={
                   # Deterministic recall lane owns the turn — assert
                   # the answer and the memory block it used.
                   "response_contains": "blue",
                   "response_not_contains": ["steak", "rtx 3080"],
                   "memory_contains": "blue",
                   "memory_not_contains": ["steak", "rtx 3080"],
                   "no_trailing_question": True,
               }),
    ]))

    # Corrections supersede — teach+supersede in c1, recall in c2 so the
    # recall transcript carries neither value; injected memory must hold
    # the new value only.
    out.append(QaScenario(f"{scenario_prefix}-correction", [
        QaTurn("use port 8080", conversation_id="scope-c1"),
        QaTurn("actually make that 8090", conversation_id="scope-c1"),
        QaTurn("what port are we using?", conversation_id="scope-c2",
               expect={
                   "response_contains": "8090",
                   "response_not_contains": "8080",
                   "memory_contains": "8090",
                   "memory_not_contains": "8080",
               }),
    ]))

    # Compound self-correction — "I like red. Actually no, blue. Never
    # mind, make it green." The settled value is the only live fact;
    # obsolete values retire even though they have no supersession slot.
    out.append(QaScenario(f"{scenario_prefix}-correction-chain", [
        QaTurn("i like red. actually no, blue. never mind, make it green",
               conversation_id="scope-cc"),
        QaTurn("what do i like?", conversation_id="scope-cc2",
               expect={
                   "memory_contains": "i like green",
                   "memory_not_contains": ["i like red", "i like blue"],
               }),
    ]))

    # Stacked markers — "wait, actually make that monday" chains two
    # discourse markers before the reset verb.
    out.append(QaScenario(f"{scenario_prefix}-correction-stacked", [
        QaTurn("the deadline is friday. wait, actually make that monday",
               conversation_id="scope-cs"),
        QaTurn("when is the deadline?", conversation_id="scope-cs2",
               expect={
                   "memory_contains": "monday",
                   "memory_not_contains": "friday",
               }),
    ]))

    # Cross-form preference supersession — "my favorite color is blue"
    # then "actually i prefer green now" re-states the same attribute
    # with different phrasing; the slot still supersedes.
    out.append(QaScenario(f"{scenario_prefix}-correction-crossform", [
        QaTurn("my favorite color is blue",
               conversation_id="scope-cf1"),
        QaTurn("actually i prefer green now", conversation_id="scope-cf1"),
        QaTurn("what's my favorite color?", conversation_id="scope-cf2",
               expect={
                   "memory_contains": "green",
                   "memory_not_contains": "blue",
               }),
    ]))

    # Repeated questions answer cleanly every time — no "I already
    # said", no escalation, no extra disclosure on the repeat.
    out.append(QaScenario(f"{scenario_prefix}-repeat-question", [
        QaTurn("when is your birthday?", conversation_id="scope-rq",
               expect={"response_contains": "september 30"}),
        QaTurn("what was your birthday again?", conversation_id="scope-rq",
               expect={
                   "response_contains": "september 30",
                   "response_not_contains": ["already", "i said",
                                             "i told you", "as i said"],
                   "no_trailing_question": True,
               }),
    ]))

    # Genuine ambiguity — a bare anaphora with no referent in context
    # must surface the unresolved-referent advisory AND the scoped
    # clarify rule so the model asks one question instead of guessing.
    out.append(QaScenario(f"{scenario_prefix}-ambiguity-fresh", [
        QaTurn("make that bigger", conversation_id="scope-am",
               expect={
                   "context_contains": ["unresolved referent",
                                        "clarifying"],
               }),
        QaTurn("what's his name?", conversation_id="scope-am2",
               expect={
                   "context_contains": ["unresolved referent",
                                        "clarifying"],
               }),
    ]))

    # Pronoun with an established referent — the transcript carries the
    # father answer forward, so 'his name' is resolvable from history
    # even though the durable store holds nothing.
    out.append(QaScenario(f"{scenario_prefix}-pronoun-followup", [
        QaTurn("who is your father?", conversation_id="scope-pf",
               expect={"response_contains": "john hamburn"}),
        QaTurn("what is his name?", conversation_id="scope-pf",
               expect={
                   "context_contains": "john hamburn",
                   "task_status": "completed",
               }),
    ]))

    # Exact-fact breadth — informal/typo forms resolve the same slot
    # with the same discipline.
    out.append(QaScenario(f"{scenario_prefix}-exact-informal", [
        QaTurn("how old r u", conversation_id="scope-ei", expect={
            "response_contains": "days old",
            "response_not_contains": ["september", "birthday", "born"],
            "no_leading_filler": True,
        }),
        QaTurn("when were u born", conversation_id="scope-ei", expect={
            "response_contains": "september 30",
            "response_not_contains": ["days old", "years old"],
        }),
        QaTurn("who built you", conversation_id="scope-ei", expect={
            "response_contains": "john hamburn",
            "no_leading_filler": True,
            "no_trailing_question": True,
        }),
    ]))

    # The scope directive must actually reach the model — an EXACT
    # question carries the one-fact budget into the prompt.
    out.append(QaScenario(f"{scenario_prefix}-directive-injected", [
        QaTurn("what's the capital of France?", conversation_id="scope-d",
               expect={
                   "context_contains": "Answer scope",
                   "task_status": "completed",
               }),
    ]))

    # Exact-fact turns must not get filler openers or forced questions
    # even when the model lane answers them.
    out.append(QaScenario(f"{scenario_prefix}-filler-discipline", [
        QaTurn("what time is it?", conversation_id="scope-t", expect={
            "no_leading_filler": True,
            "no_trailing_question": True,
            "max_sentences": 2,
        }),
        QaTurn("what's today's date?", conversation_id="scope-t", expect={
            "no_leading_filler": True,
            "no_trailing_question": True,
        }),
        QaTurn("who created you?", conversation_id="scope-t", expect={
            "response_contains": "john hamburn",
            "no_leading_filler": True,
            "no_reasoning_narration": True,
        }),
    ]))

    # Memory inspection — "what do you know about me" lists active
    # user-taught facts in second person via the deterministic lane;
    # superseded values never appear (live dogfood found the model
    # improvising persona lore here instead).
    out.append(QaScenario(f"{scenario_prefix}-facts-recall", [
        QaTurn("my favorite color is blue", conversation_id="scope-fr"),
        QaTurn("the deadline is friday", conversation_id="scope-fr"),
        QaTurn("what do you know about me?", conversation_id="scope-fr",
               expect={
                   "response_contains": ["your favorite color is blue",
                                         "deadline is friday"],
                   "no_leading_filler": True,
                   "no_reasoning_narration": True,
               }),
    ]))

    # Single-fact recall is deterministic — the stored fact answers
    # directly in the correct person, superseded values stay hidden,
    # and general-knowledge questions sharing a term never hijack.
    out.append(QaScenario(f"{scenario_prefix}-fact-recall", [
        QaTurn("my favorite color is blue", conversation_id="scope-fc"),
        QaTurn("use port 8080", conversation_id="scope-fc"),
        QaTurn("actually use port 9000", conversation_id="scope-fc"),
        QaTurn("whats my favorite color?", conversation_id="scope-fc",
               expect={
                   "response_contains": "your favorite color is blue",
                   "response_not_contains": ["my favorite color",
                                             "don't know",
                                             "haven't told"],
                   "no_leading_filler": True,
                   "no_reasoning_narration": True,
               }),
        QaTurn("what port are we using?", conversation_id="scope-fc",
               expect={
                   "response_contains": "9000",
                   "response_not_contains": "8080",
               }),
        QaTurn("what color is the sky?", conversation_id="scope-fc",
               expect={
                   # General knowledge — the color fact must not answer.
                   "response_not_contains": ["your favorite",
                                             "favorite color"],
               }),
    ]))

    # Re-teaching an already-active fact must still ack — the first
    # dogfood run answered a restatement with a model essay.
    out.append(QaScenario(f"{scenario_prefix}-restatement", [
        QaTurn("my favorite color is blue", conversation_id="scope-rs"),
        QaTurn("my favorite color is blue", conversation_id="scope-rs",
               expect={
                   "response_contains": "already noted",
                   "no_leading_filler": True,
               }),
        QaTurn("what's my favorite color?", conversation_id="scope-rs",
               expect={"response_contains": "blue"}),
    ]))

    # Topic-shift advisories reach the model — a forward shift tells it
    # to answer the new turn on its own; a return tells it to resume.
    out.append(QaScenario(f"{scenario_prefix}-topic-shift", [
        QaTurn("new topic — what's the capital of france?",
               conversation_id="scope-ts",
               expect={"context_contains": "Topic shift"}),
        # A return with no retained referent surfaces the ambiguity
        # notice instead of guessing one.
        QaTurn("back to that earlier thing",
               conversation_id="scope-ts",
               expect={"context_contains": "Ambiguity notice"}),
    ]))

    # Person-directed wheres are not UI navigation — "where are you"
    # asks about the assistant; it must never resolve a page.
    out.append(QaScenario(f"{scenario_prefix}-where-person", [
        QaTurn("where are you?", conversation_id="scope-wp", expect={
            "response_not_contains": ["Speech Lab", "under Chat",
                                      "Personality Studio"],
        }),
        QaTurn("what's the weather like where you are?",
               conversation_id="scope-wp", expect={
            "response_not_contains": ["Speech Lab", "under Chat",
                                      "Personality Studio"],
        }),
    ]))

    # Long conversation — 60+ turns mixing exact questions, topic
    # switches, corrections, and callbacks in one chat. Per-turn
    # invariants hold the WHOLE session: exact facts stay bare,
    # superseded values never inject, and every question turn carries
    # its scope directive. This is the harness-side half of the
    # 50+-turn dogfood requirement.
    out.append(_long_conversation_scenario(f"{scenario_prefix}-long"))
    return out


def _long_conversation_scenario(scenario_id: str) -> QaScenario:
    """Interleaved long-session scenario — deterministic, ~64 turns."""
    turns: list[QaTurn] = []
    cid = "scope-long"
    # Act 1 — establish facts and topics.
    turns.append(QaTurn("my favorite color is blue", conversation_id=cid))
    turns.append(QaTurn("use port 8080", conversation_id=cid))
    turns.append(QaTurn("Project Orion uses PostgreSQL",
                        conversation_id=cid))
    turns.append(QaTurn("how old are you?", conversation_id=cid, expect={
        "response_contains": "days old",
        "response_not_contains": ["september", "birthday", "born"],
        "no_leading_filler": True,
        "no_trailing_question": True,
    }))
    # Act 2 — distractor churn (topic drift must not resurrect facts
    # the turn never asked about).
    distractors = [
        "what's a good weeknight dinner",
        "explain the difference between viruses and bacteria",
        "how do I reverse a string in python",
        "what's the weather usually like in lisbon in april",
        "electric vs gas cars for a 15 mile commute",
        "what would happen if the moon disappeared",
        "how much ram does a browser use",
        "tell me something about jazz",
    ]
    for i in range(24):
        q = distractors[i % len(distractors)]
        turns.append(QaTurn(q, conversation_id=cid, expect={
            # Every question turn must carry its answer-size budget.
            "context_contains": "Answer scope",
        }))
    # Act 3 — corrections mid-session.
    turns.append(QaTurn("actually use port 9000", conversation_id=cid))
    turns.append(QaTurn("my favorite color is blue. actually i prefer "
                        "green now", conversation_id=cid))
    # Act 4 — recall: settled values only, never the superseded ones.
    turns.append(QaTurn("what port are we using?", conversation_id=cid,
                        expect={
                            "response_contains": "9000",
                            "response_not_contains": "8080",
                            "memory_contains": "9000",
                            "memory_not_contains": "8080",
                        }))
    turns.append(QaTurn("what's my favorite color?", conversation_id=cid,
                        expect={
                            "response_contains": "green",
                            "response_not_contains": ["steak", "blue"],
                            "memory_contains": "green",
                            "memory_not_contains": "steak",
                        }))
    turns.append(QaTurn("what database does Orion use?",
                        conversation_id=cid,
                        expect={
                            "context_contains": "PostgreSQL",
                            "context_not_contains": ["SQLite", "steak"],
                        }))
    # Act 5 — identity facts stay disciplined deep into the session.
    turns.append(QaTurn("how old are you?", conversation_id=cid, expect={
        "response_contains": "days old",
        "response_not_contains": ["september", "birthday", "born",
                                  "already", "i said"],
        "no_leading_filler": True,
        "no_trailing_question": True,
    }))
    turns.append(QaTurn("when is your birthday?", conversation_id=cid,
                        expect={
                            "response_contains": "september 30",
                            "response_not_contains": "days old",
                        }))
    # Act 6 — more churn, then a callback to the FIRST act's port.
    for i in range(24):
        turns.append(QaTurn(distractors[(i + 3) % len(distractors)],
                            conversation_id=cid))
    turns.append(QaTurn("remind me — what port did we settle on?",
                        conversation_id=cid,
                        expect={
                            # State-graph decision recall answers this
                            # deterministically now — the settled value
                            # must appear in the response and never the
                            # superseded one (context_contains held until
                            # the state lane took over the recall path).
                            "response_contains": "9000",
                            "response_not_contains": "8080",
                        }))
    turns.append(QaTurn("how old are ya", conversation_id=cid, expect={
        "response_contains": "days old",
        "response_not_contains": ["birthday", "september"],
        "no_leading_filler": True,
    }))
    return QaScenario(scenario_id, turns, default_conversation_id=cid)
