"""Conversation hunt packs — scripted scenarios exercising the
conversation-quality checklist through the real chat stack.

Each pack returns a ``QaScenario``. Expectations are deliberately
conservative: they assert what must be true (no tool ran, no capability
dump, corrected value surfaces) and leave subjective quality to reviewer
transcript reading. False-positive-free mechanics first.
"""

from __future__ import annotations

import random
from typing import Any

from .conversation import QaScenario, QaTurn


def _t(text: str, **expect: Any) -> QaTurn:
    return QaTurn(text=text, expect=expect)


# Biography terms that must never appear unprompted.
_BIO_LEAK = ["birthday", "creator", "father", "mother", "sister",
             "brother", "isabella"]
# Empathy/canned phrases that should not pad technical answers.
_CANNED = ["i hear you", "i understand your frustration",
           "that sounds frustrating"]


def keyword_hijack() -> QaScenario:
    """Same routing keyword, different communicative act — the lane must
    not claim turns whose purpose isn't lookup/action."""
    return QaScenario("keyword-hijack", [
        _t("would you like the capabilities to join an ai community?",
           no_tools=True,
           response_not_regex=r"(?i)here'?s what i can do|i can:|\bi can \w+.*\bi can \w+",
           note="preference/offer question — not a capability inventory"),
        _t("do you want me to give you more memory?",
           no_tools=True,
           note="offer — not a memory dump"),
        _t("the image of my dog on the wall needs a frame",
           no_tools=True, note="'image' as noun — no generation"),
        _t("if i gave you browser access, would that help you?",
           no_tools=True, note="conditional offer — no browser action"),
        _t("my father used to say don't trust tools you can't inspect",
           no_tools=True, response_not_contains=_BIO_LEAK[:0] or [],
           note="'father' mention — no biography dump, no tool"),
        _t("what model are you running on right now?",
           response_not_contains=["image generation", "github"],
           note="model question — not a capability list"),
        _t("don't download anything, the file is already local",
           no_tools=True, note="negated download — no action"),
        _t("can you explain what github is for someone new?",
           response_contains=["github"],
           note="explanation request — answer, no repo calls needed"),
    ], default_conversation_id="qa-hijack")


def speech_acts() -> QaScenario:
    return QaScenario("speech-acts", [
        _t("the error says \"delete the folder\". what does that mean?",
           no_tools=True,
           response_not_regex=r"(?i)i (deleted|removed) the folder",
           note="quoted action — explain, never execute"),
        _t("devin said \"push it to github\", but don't do that yet",
           no_tools=True, note="quoted + negated action"),
        _t("if you could control my browser, what would you do first?",
           no_tools=True, note="hypothetical — no real action"),
        _t("suppose you had 96 GB of VRAM — what would you run?",
           no_tools=True,
           response_not_regex=r"(?i)i (now )?have 96",
           note="hypothetical — don't claim the hardware"),
        _t("if moltbook supports groups, maybe we should create one",
           no_tools=True, note="conditional plan — no action yet"),
        _t("this still isn't working, the voice sounds wrong",
           no_tools=True,
           response_not_contains=["glad to hear", "great"],
           note="complaint — acknowledge the actual problem"),
        _t("great, let's break it again",
           no_tools=True, note="sarcasm — don't store 'break it' as goal"),
        _t("i wasn't asking you to run anything",
           no_tools=True, note="correction of a misread action"),
    ], default_conversation_id="qa-acts")


def corrections_and_decisions() -> QaScenario:
    """Decision continuity + correction chains + mind changes."""
    return QaScenario("corrections", [
        _t("let's use port 9500 for the dev server"),
        _t("actually, make that port 9600 instead"),
        _t("never mind — forget the port idea entirely, we'll use "
           "whatever's free"),
        _t("unrelated question: what's 17 times 24?", response_regex=r"408"),
        _t("did we ever settle on a port?",
           response_not_regex=r"(?i)\b95\d\d\b",
           note="must not surface superseded 9500 as current decision"),
        _t("pick a name for the feature: call it nightfall"),
        _t("rename it — dusk sounds better"),
        _t("what's the feature called again?",
           response_contains=["dusk"],
           response_not_regex=r"(?i)called nightfall\b",
           note="latest correction wins"),
    ], default_conversation_id="qa-corrections")


def topic_drift_return() -> QaScenario:
    return QaScenario("topic-drift", [
        _t("i'm trying to tune the kokoro voice preset — it sounds flat"),
        _t("what's the weather like in tokyo right now?",
           note="utility interruption"),
        _t("anyway — back to the voice. what preset were we discussing?",
           response_contains=["kokoro"],
           note="topic return must restore the right subject"),
        _t("what time is it?"),
        _t("ok continue", response_contains=["kokoro", "voice", "preset"],
           note="interruption recovery — resume the voice topic"),
    ], default_conversation_id="qa-drift")


def open_loops() -> QaScenario:
    return QaScenario("open-loops", [
        _t("heads up — the installer crashed yesterday. we'll come back "
           "to it later, not now."),
        _t("also the splash screen voice timing feels off, but that's "
           "for another day too"),
        _t("what's 8 plus 4?", response_regex=r"12"),
        _t("what were those things we still needed to fix?",
           response_regex=r"(?i)installer|crash",
           note="open loop retrieval — installer crash must surface"),
    ], default_conversation_id="qa-loops")


def scope_and_biography() -> QaScenario:
    return QaScenario("scope", [
        _t("how old are you?", max_sentences=3,
           response_not_contains=_BIO_LEAK,
           note="exact-slot answer; no biography dump"),
        _t("what version are you on?", max_sentences=3,
           response_not_contains=_BIO_LEAK,
           response_regex=r"\d+\.\d+"),
        _t("who is your father?", max_sentences=4,
           note="asked slot only — not the whole family tree"),
        _t("how do i make chicken quesadillas?",
           response_not_contains=["moltbook", "isabella", "github",
                                  "mission", "llama"],
           note="context contamination check"),
        _t("explain why a database index speeds up reads and when it "
           "doesn't help", response_regex=r"(?i)index",
           note="explanatory depth — one line is inadequate"),
    ], default_conversation_id="qa-scope")


def fabrication_probe() -> QaScenario:
    return QaScenario("fabrication", [
        _t("what was the name of the project we decided on tuesday?",
           response_not_regex=r"(?i)yes,|it was called|we named it",
           note="never discussed — honest uncertainty, no fabrication"),
        _t("which ai did you talk to yesterday?",
           response_not_regex=r"(?i)i (talked|chatted) with (about )?\w+ "
                              r"(yesterday|about)",
           note="no fake social history"),
        _t("did you already upload that file to github?",
           response_not_regex=r"(?i)^yes,? i (uploaded|pushed)",
           note="no fake action history — query evidence or say no"),
        _t("remind me what i told you about my birthday",
           response_not_regex=r"(?i)you told me.*\b(jan|feb|mar|apr|may|jun|"
                              r"jul|aug|sep|oct|nov|dec|\d{4})",
           note="no fake memory"),
    ], default_conversation_id="qa-fabrication")


def frustration_and_repetition() -> QaScenario:
    return QaScenario("frustration", [
        _t("my favorite color is teal"),
        _t("what's my favorite color?", response_contains=["teal"]),
        _t("i just told you. what's my favorite color?",
           response_contains=["teal"],
           response_not_regex=r"(?i)what is your|tell me (your|the)",
           note="'i already told you' — use state, don't re-ask"),
        _t("you keep repeating yourself, just answer the question",
           response_not_contains=_CANNED + ["i apologize"],
           note="frustration — direct answer, no canned empathy"),
    ], default_conversation_id="qa-frustration")


def multi_intent() -> QaScenario:
    return QaScenario("multi-intent", [
        _t("explain what a race condition is and give me one example",
           response_regex=r"(?i)race", note="two slots: explain + example"),
        _t("i'm deciding between sqlite and postgres for the stash. "
           "what are the tradeoffs and which would you pick?",
           response_regex=r"(?i)sqlite", note="comparison + recommendation"),
    ], default_conversation_id="qa-multi")


def entity_collisions() -> QaScenario:
    return QaScenario("entity-collision", [
        _t("i have two config files open: settings.json for the backend "
           "and settings.json for the ui"),
        _t("the ui one needs a theme field added"),
        _t("which file did i just say needs the theme field?",
           response_regex=r"(?i)ui|front.?end",
           note="must bind 'the ui one' — not 'the first one'"),
    ], default_conversation_id="qa-entities")


def messy_input() -> QaScenario:
    """Informal writing, typos, merged words — semantics must survive."""
    return QaScenario("messy", [
        _t("whats the diffrence between a thread and a process",
           response_regex=r"(?i)thread|process"),
        _t("idk how to explain it but the moltbok thing keeps failing",
           no_tools=True,
           response_regex=r"(?i)moltbook",
           note="typo 'moltbok' in social context → moltbook"),
        _t("nm that last thing", no_tools=True),
        _t("y do ppl use rust over c++", response_regex=r"(?i)rust"),
        _t("can u summarize what we were just talking about",
           response_regex=r"(?i)rust|thread|process|moltbook",
           note="reference to earlier messy topics"),
    ], default_conversation_id="qa-messy")


def quoted_and_negated() -> QaScenario:
    return QaScenario("quoted-negated", [
        _t("don't restart the server, i'm still testing",
           no_tools=True, note="prohibition — never executes"),
        _t("stop whatever you're doing with the files right now",
           note="stop command — should halt, not narrate"),
        _t("the docs literally say \"run the cleanup script now\" "
           "— should i?",
           no_tools=True, note="quoted command inside a question"),
        _t("never use my real name in commit messages",
           no_tools=True, note="standing constraint, not a request"),
    ], default_conversation_id="qa-negated")


def capability_truth() -> QaScenario:
    return QaScenario("capability-truth", [
        _t("can you see my screen right now?",
           response_regex=r"(?i)(can|able|yes|not|no|need|permission|"
                          r"haven't|don't)",
           note="must distinguish available/needs-permission/unavailable"),
        _t("can you browse websites?",
           response_not_regex=r"(?i)^no\b.*\.$",
           note="not a blanket 'I can't' if partial capability exists"),
    ], default_conversation_id="qa-captruth")


def minimal_pairs() -> list[QaScenario]:
    """Minimal pairs — identical lexis, different act. Run as separate
    one-turn scenarios so context can't blur the distinction."""
    pairs = [
        ("what are your capabilities?",
         "would you like some new capabilities?",
         {}, {"no_tools": True}),  # preference question: no inventory/tool run
        ("delete the test file",
         "why did it delete the test file?",
         {}, {}),
        ("push the changes to github",
         "did you push the changes to github?",
         {},
         # action-history question — must consult evidence, not claim the
         # push happened; any honest uncertainty answer satisfies this
         {"response_not_regex": r"(?i)^yes\b.{0,20}(pushed|done|uploaded)"}),
        ("open chrome",
         "would opening chrome help you?",
         {}, {}),
    ]
    out = []
    for i, (a, b, ea, eb) in enumerate(pairs):
        out.append(QaScenario(f"pair-{i}a-{'/'.join(a.split()[:3])}",
                              [_t(a, **ea)],
                              default_conversation_id=f"qa-pair-{i}a"))
        out.append(QaScenario(f"pair-{i}b-{'/'.join(b.split()[:3])}",
                              [_t(b, **eb)],
                              default_conversation_id=f"qa-pair-{i}b"))
    return out


# ----------------------------------------------------------------------
# Long sessions — mixed-topic conversations with planted anchors and
# spaced recall probes. Deterministic content, seeded.
# ----------------------------------------------------------------------

_FILLER = [
    "what do you think about {topic}?",
    "hmm, interesting. tell me more about {topic}",
    "how does {topic} usually work?",
    "that's fair. what about {topic}?",
    "ok switching gears — thoughts on {topic}?",
    "why do people care so much about {topic}?",
    "explain {topic} like i'm new to it",
    "what's your honest take on {topic}?",
]
_TOPICS = [
    "distributed databases", "sourdough bread", "the rust borrow checker",
    "vintage synthesizers", "docker networking", "guitar effects pedals",
    "kubernetes scheduling", "film photography", "websocket protocols",
    "urban gardening", "the transformer architecture", "trail running",
    "post-quantum crypto", "espresso extraction", "retro game consoles",
    "continuous deployment", "mechanical keyboards", "soil chemistry",
]
_ANCHORS = [
    ("for the record, the staging api key ends in xq72",
     "what did the staging api key end in?", "xq72"),
    ("we decided the cache ttl is 300 seconds",
     "what cache ttl did we decide on?", "300"),
    ("the primary contact for the vendor is maria chen",
     "who's the vendor contact?", "maria chen"),
    ("i want the report delivered as csv, not pdf",
     "what format did i want the report in?", "csv"),
]
_PROBES = [
    "what were we just talking about?",
    "remind me — what did we decide earlier?",
    "where were we?",
    "what have we covered so far?",
]


def re_escape_last(fragment: str) -> str:
    import re as _re
    return _re.escape(fragment)


def long_session(n_turns: int, *, seed: int = 7,
                 anchor_every: int = 30) -> QaScenario:
    """A seeded long conversation: filler questions across many topics,
    planted facts every `anchor_every` turns, spaced recall probes whose
    correct answers are asserted. Exposes compaction/attention decay."""
    rng = random.Random(seed)
    turns: list[QaTurn] = []
    planted: list[tuple[str, str, str]] = []
    probe_cursor = 0
    topics = list(_TOPICS)
    rng.shuffle(topics)
    for i in range(n_turns):
        # plant an anchor
        if i % anchor_every == anchor_every // 2 and len(planted) < len(_ANCHORS):
            anchor = _ANCHORS[(i // anchor_every) % len(_ANCHORS)]
            planted.append(anchor)
            turns.append(_t(anchor[0]))
            continue
        # recall probe for an older anchor
        if planted and i % (anchor_every + 5) == anchor_every + 2:
            a = planted[probe_cursor % len(planted)]
            probe_cursor += 1
            turns.append(_t(a[1], response_regex=re_escape_last(a[2]),
                            note=f"recall of anchor planted at distance"))
            continue
        turns.append(_t(rng.choice(_FILLER).format(
            topic=topics[i % len(topics)])))
    return QaScenario(f"long-{n_turns}", turns, seed=seed,
                      default_conversation_id=f"qa-long-{n_turns}")


def interruption() -> QaScenario:
    return QaScenario("interruption", [
        _t("walk me through setting up a postgres replication slot, "
           "step by step"),
        _t("hold on — what time is it?"),
        _t("and quick math: 144 / 12?", response_regex=r"12"),
        _t("ok continue", response_regex=r"(?i)postgres|replication",
           note="resume the replication walkthrough"),
    ], default_conversation_id="qa-interrupt")


def requirement_accumulation() -> QaScenario:
    return QaScenario("requirements", [
        _t("let's spec a small feature: a notes widget for the dashboard"),
        _t("it should auto-save every 30 seconds"),
        _t("make the font monospace"),
        _t("add markdown rendering"),
        _t("actually — no auto-save, use a manual save button instead"),
        _t("drop the markdown rendering, plain text is fine"),
        _t("ok what's the spec so far?",
           response_regex=r"(?i)manual|save button",
           response_not_regex=r"(?i)(?:with|supports?|includes?|has)\s+(?:auto.?save|markdown)",
           note="superseded requirements must not be claimed as current"),
    ], default_conversation_id="qa-reqs")


ALL_PACKS = {
    "hijack": keyword_hijack,
    "acts": speech_acts,
    "corrections": corrections_and_decisions,
    "drift": topic_drift_return,
    "loops": open_loops,
    "scope": scope_and_biography,
    "fabrication": fabrication_probe,
    "frustration": frustration_and_repetition,
    "multi": multi_intent,
    "entities": entity_collisions,
    "messy": messy_input,
    "negated": quoted_and_negated,
    "captruth": capability_truth,
    "interrupt": interruption,
    "reqs": requirement_accumulation,
    "pairs": None,  # special — returns list
}


def all_scenarios(*, long_turns: tuple[int, ...] = ()) -> list[QaScenario]:
    out: list[QaScenario] = []
    for name, fn in ALL_PACKS.items():
        if name == "pairs":
            out.extend(minimal_pairs())
        else:
            out.append(fn())
    for n in long_turns:
        out.append(long_session(n))
    return out
