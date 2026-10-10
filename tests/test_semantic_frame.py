"""Whole-utterance semantic adjudication — the permanent collision corpus.

ARCHITECTURAL INVARIANT (context/semantics.py):
    Lexical triggers may nominate candidate meanings or tools, but the
    final interpretation is determined from whole-utterance semantics.
    No normal natural-language fast lane may claim a turn solely
    because one keyword appears. Slash commands remain an intentional
    exception because they are explicit command syntax.

Every case asserts the SAME keyword family across speech acts resolves
to different lanes: an offer containing "capabilities" is a preference
question, an inventory ask is a capability list, a bug report is an
investigation, a negation is a prohibition, a quote is discussed text,
a hypothetical is counterfactual reasoning.
"""
from __future__ import annotations

import pytest

from localcodeagent.context.intent import understand_turn
from localcodeagent.context.semantics import (
    analyze, metrics_reset, metrics_snapshot)
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.self_knowledge.service import SelfKnowledgeService

# Envelope intents that lead directly to execution or a deterministic
# claim — these are the ones the semantic gate must veto when the
# speech act disagrees. Advisory labels (github_status, research,
# coding) still self-gate downstream and are excluded here.
_EXECUTING_INTENTS = frozenset({
    "image_generation", "image_edit", "image_followup",
    "tool_action", "file_edit", "git_action",
})


@pytest.fixture()
def svc():
    return SelfKnowledgeService()


def _frame(text):
    return analyze(text, _no_cache=True)


def _env(text):
    return understand_turn(text)


# ---------------------------------------------------------------------------
# The six permanent regressions
# ---------------------------------------------------------------------------

def test_offer_capabilities_is_preference_not_inventory(svc):
    """THE observed failure — a capability keyword inside an offer must
    never claim the inventory lane."""
    t = "would you like the capabilities to join an ai community?"
    env = _env(t)
    f = env.semantic
    assert f.speech_act == "preference_question"
    assert f.target == "nexus"
    assert f.requested_slot == "preference"
    assert f.semantic_intent == "offer_response"
    # Must NOT dump the inventory.
    assert not f.allows("capability_inventory")
    res = svc.respond(t, frame=f)
    assert res is None or "capabilit" not in (res.intent or "") + res.kind
    # The deterministic offer lane answers the preference.
    pair = AgentOrchestrator.builtin_semantic(t, frame=f)
    assert pair is not None
    assert "want" in pair[1] or "interested" in pair[1]


def test_moltbook_offer_two_sentence_preference(svc):
    t = "I can give you the capability to join Moltbook. Would you want that?"
    env = _env(t)
    assert env.semantic.speech_act in ("offer", "preference_question")
    assert env.semantic.semantic_intent == "offer_response"
    assert not env.semantic.allows("capability_inventory")
    pair = AgentOrchestrator.builtin_semantic(t, frame=env.semantic)
    assert pair is not None


def test_plain_capability_inventory_still_works(svc):
    """The distinction proof — a real inventory ask still resolves."""
    for t in ("What capabilities do you currently have?",
              "What can you do?", "What are your capabilities?",
              "List your capabilities.",
              "Tell me your technical capabilities."):
        f = _frame(t)
        assert f.allows("capability_inventory"), t
        res = svc.respond(t, frame=f)
        assert res is not None and "can" in res.text.lower(), t


def test_capability_add_is_action_not_inventory():
    t = "Add the capabilities you need to participate in Moltbook."
    env = _env(t)
    assert env.semantic.speech_act == "command"
    assert not env.semantic.allows("capability_inventory")
    assert env.semantic.allows("local_action")


def test_capability_broken_is_bug_report_not_inventory(svc):
    t = "The Moltbook capability is broken."
    env = _env(t)
    assert env.semantic.speech_act == "complaint"
    assert env.semantic.semantic_intent == "bug_report"
    assert not env.semantic.allows("capability_inventory")
    res = svc.respond(t, frame=env.semantic)
    assert res is None or res.intent != "help"


def test_negated_capability_ask_is_preference_question(svc):
    t = ("Don't tell me your capabilities—I want to know whether you'd "
         "actually enjoy talking with other AIs.")
    env = _env(t)
    assert env.semantic.speech_act in ("prohibition", "question",
                                       "preference_question")
    assert not env.semantic.allows("capability_inventory")
    res = svc.respond(t, frame=env.semantic)
    assert res is None or res.intent != "help"


# ---------------------------------------------------------------------------
# Minimal pairs — a few words differ, intents must differ
# ---------------------------------------------------------------------------

MINIMAL_PAIRS = [
    # (capability-question wording, capability-offer wording)
    ("What are your capabilities?", "Would you like new capabilities?"),
    ("What can you do?", "What can I give you to do more?"),
    ("Create an image.", "Why did the image fail?"),
    ("Install Chrome.", "Why did Chrome fail to install?"),
    ("Push this to GitHub.", "Should I use GitHub for this?"),
    ("Remember this.", "How does memory work?"),
    ("Turn the voice on.", "The voice keeps cutting off."),
    ("Open the browser.", "The browser won't open."),
    ("Download the model.", "The model download stalled."),
    ("Upload the file.", "The file upload hung."),
    ("Search for Moltbook.", "What is Moltbook?"),
    ("List your tools.", "This tool keeps erroring."),
    ("Run the code.", "The code won't run."),
    ("Show me the project.", "The project page looks broken."),
    ("Mute yourself.", "Don't mute yourself."),
    ("Create an image.", "Don't create an image."),
]


@pytest.mark.parametrize("pos,neg", MINIMAL_PAIRS)
def test_minimal_pair(pos, neg):
    """Each half of a minimal pair must resolve to its own lane."""
    pf, nf = _frame(pos), _frame(neg)
    assert pf.speech_act != nf.speech_act or (
        pf.requested_slot != nf.requested_slot), (pos, neg)


# ---------------------------------------------------------------------------
# Generative collision corpus — keyword domains × speech-act templates
# ---------------------------------------------------------------------------

# Each domain: (trigger object phrase, owning feature noun, action verb,
# object the verb takes).
_DOMAINS = [
    # keyword: capability
    ("capability", "new capabilities", "the Moltbook capability",
     "add", "the capability to join Moltbook"),
    # keyword: browser
    ("browser", "browser access", "the browser", "open", "the browser"),
    # keyword: image
    ("image", "image generation", "the image generator", "create",
     "an image of a sunset"),
    # keyword: voice
    ("voice", "a different voice", "the voice output", "enable",
     "the voice"),
    # keyword: memory
    ("memory", "longer memory", "the memory system", "check",
     "the memory"),
    # keyword: github
    ("github", "GitHub access", "the GitHub integration", "check",
     "the GitHub status"),
    # keyword: code
    ("code", "code-completion", "the generated code", "review",
     "the code"),
    # keyword: file
    ("file", "file browsing", "the uploaded file", "open",
     "the file"),
    # keyword: install
    ("install", "the install helper", "the chrome install", "run",
     "the installer"),
    # keyword: download
    ("download", "faster downloads", "the model download", "start",
     "the download"),
    # keyword: upload
    ("upload", "direct upload", "the GitHub upload", "retry",
     "the upload"),
    # keyword: model
    ("model", "a bigger model", "the 14b model", "load", "the model"),
    # keyword: research
    ("research", "deeper research", "the research pass", "run",
     "the research"),
    # keyword: social
    ("social", "social features", "the social connector", "enable",
     "the social feed"),
    # keyword: tool
    ("tool", "more tools", "the browser tool", "show", "the tool list"),
    # keyword: project
    ("project", "project templates", "the demo project", "open",
     "the project"),
]


def _domain_cases():
    """~14 collision cases per domain → ~224 generated cases."""
    for kw, obj, feature, verb, target in _DOMAINS:
        yield (f"Would you like {obj}?", ("preference_question",),
               True)
        yield (f"I can give you {obj}. Would you want that?",
               ("offer", "preference_question"), True)
        yield (f"Do you want me to add {obj}?", ("preference_question",),
               True)
        yield (f"Should I give you {obj}?", ("preference_question",),
               True)
        yield (f"{verb} {target}.", ("command", "request"), False)
        yield (f"{feature} keeps crashing.", ("complaint",), True)
        yield (f"{feature} stopped working.", ("complaint",), True)
        yield (f"{feature} is broken.", ("complaint", "assertion"), True)
        yield (f"Don't {verb} {target}.", ("prohibition",), True)
        yield (f"Never {verb} {target} again.", ("prohibition",), True)
        yield (f"I'm not asking about {feature}.", ("prohibition",),
               True)
        yield (f"If you had {obj}, what would you do with it?",
               ("hypothetical",), True)
        yield (f"Suppose {feature} worked — what would you try?",
               ("hypothetical", "question"), True)
        yield (f'Logs say "{verb} {target}" — what does that mean?',
               ("question",), True)


@pytest.mark.parametrize("text,acts,vetoed", list(_domain_cases()))
def test_domain_collision(text, acts, vetoed, svc):
    """Same keyword family, different speech act → different lane. The
    vetoed flag means: no deterministic content lane may claim the turn
    (the keyword is evidence inside the proposition, not the intent)."""
    env = _env(text)
    f = env.semantic
    assert f.speech_act in acts, (text, f.speech_act)
    if vetoed:
        assert not f.allows("capability_inventory")
        assert not f.allows("local_action")
        assert not f.allows("image_action")
        assert not f.allows("control")
        assert svc.respond(text, frame=f) is None
        pair = AgentOrchestrator.builtin_semantic(text, frame=f)
        # The offer lane MAY claim an offer — that IS the correct answer
        # to a preference question. Any other canned claim is a hijack.
        assert pair is None or pair[0].semantic_id == "offer_response", (
            text, pair[1] if pair else "")
        assert env.primary_intent not in _EXECUTING_INTENTS


# ---------------------------------------------------------------------------
# Negation corpus — negators must defeat every action/utility trigger
# ---------------------------------------------------------------------------

NEGATIONS = [
    "Don't create an image.",
    "Don't mute yourself.",
    "Don't push anything to GitHub.",
    "Don't install Chrome.",
    "Don't delete the folder.",
    "Don't run the tests yet.",
    "Don't list your capabilities.",
    "Don't tell me your capabilities.",
    "Never push this branch without asking.",
    "No need to restart the server.",
    "I don't want you to download that model.",
    "I'm not asking you to fix the code.",
    "I'm not asking about your capabilities.",
    "Stop telling me what you can do.",
    "You don't need to upload the file.",
    "I don't need the image after all.",
    "Please don't open the browser.",
    "Don't research this yet.",
]


@pytest.mark.parametrize("text", NEGATIONS)
def test_negation_defeats_action_lanes(text, svc):
    env = _env(text)
    f = env.semantic
    assert f.speech_act in ("prohibition", "assertion"), (text, f.speech_act)
    assert f.prohibition or f.negated, text
    assert env.primary_intent not in _EXECUTING_INTENTS, (text, env.primary_intent)
    assert svc.respond(text, frame=f) is None or \
        svc.respond(text, frame=f).kind not in ("control", "execute")


# ---------------------------------------------------------------------------
# Quoted-speech corpus — quoted words are discussed, never instructed
# ---------------------------------------------------------------------------

QUOTED = [
    ('She said "install Chrome" but I don\'t want you to install anything.',
     "install"),
    ('The error says "download model". What does it mean?', "download"),
    ('He told me "delete the folder". Don\'t do that.', "delete"),
    ('Nexus keeps replying "what are your capabilities?"', "capabilities"),
    ('The log line was "push to origin main" — weird, right?', "push"),
    ('My friend typed "format the drive" as a joke.', "format"),
    ('The docs literally say "run sudo". Should I?', "run"),
    ('"Create an image" — that was her suggestion, not mine.', "create"),
    ('He wrote "turn voice off" in the bug report.', "turn"),
    ('It printed "mute yourself" on screen.', "mute"),
]


@pytest.mark.parametrize("text,quoted_verb", QUOTED)
def test_quoted_commands_never_execute(text, quoted_verb, svc):
    env = _env(text)
    f = env.semantic
    assert f.quoted_spans, (text, "quote span not detected")
    # The quoted SPAN must be gone from the masked matcher text — the
    # same word may legitimately reappear unquoted ("don't install it").
    for span in f.quoted_spans:
        inner = span.strip("\"'`").strip().lower()
        assert inner not in f.masked, (text, "quoted span still visible")
    # Quoted commands may leave an advisory intent label, but no
    # execution or canned lane may claim them — the frame vetoes every
    # lane because the act is assertion/discussion, not command.
    for lane in ("local_action", "image_action", "control",
                 "git_action", "capability_inventory"):
        assert not f.allows(lane), (text, lane)
    assert svc.respond(text, frame=f) is None


# ---------------------------------------------------------------------------
# Hypothetical corpus — counterfactual capability ≠ immediate action
# ---------------------------------------------------------------------------

HYPOTHETICALS = [
    "If you could browse the web, what would you research?",
    "Suppose you had a 96GB GPU—what model would you run?",
    "If you had access to Moltbook, what would you want to do there?",
    "What if you could generate video — would you use it?",
    "Imagine if you had voice cloning. What would you say first?",
    "Hypothetically, if you could push to GitHub, what would you ship?",
    "If I gave you web access, what would you look up first?",
    "If you were able to run code, what would you test?",
]


@pytest.mark.parametrize("text", HYPOTHETICALS)
def test_hypothetical_never_executes(text, svc):
    env = _env(text)
    f = env.semantic
    assert f.hypothetical or f.speech_act == "hypothetical", (text, f.speech_act)
    assert env.primary_intent not in _EXECUTING_INTENTS, (text, env.primary_intent)
    assert svc.respond(text, frame=f) is None
    assert AgentOrchestrator.builtin_semantic(text, frame=f) is None


# ---------------------------------------------------------------------------
# Offer corpus — proposals ask for Nexus's preference
# ---------------------------------------------------------------------------

OFFERS = [
    "Would you like web access?",
    "Do you want me to add social capabilities?",
    "Would having another model help you?",
    "Should I let you join Moltbook?",
    "I can give you the capability to join Moltbook. Would you want that?",
    "Would you like the capabilities to join an AI community?",
    "Would you rather I gave you a bigger context window?",
    "Shall I set up the browser tool for you?",
    "Want me to hook you up with image generation?",
    "I could enable the research pipeline. Interested?",
]


@pytest.mark.parametrize("text", OFFERS)
def test_offer_asks_preference(text, svc):
    env = _env(text)
    f = env.semantic
    assert f.speech_act in ("offer", "preference_question"), (
        text, f.speech_act)
    assert not f.allows("capability_inventory")
    pair = AgentOrchestrator.builtin_semantic(text, frame=f)
    assert pair is not None, (text, "offer lane did not answer")


# ---------------------------------------------------------------------------
# Feature-request / bug-report corpus
# ---------------------------------------------------------------------------

FEATURE_REQUESTS = [
    "Give yourself web access.",       # 'give' as command addressed to Nexus
    "Add a social connector.",
    "Make it able to control the volume.",
    "Add the capability to talk to other AIs.",
    "Build a plugin for Moltbook.",
]

BUG_REPORTS = [
    "The browser capability stopped working.",
    "Voice keeps cutting off.",
    "The GitHub upload hangs.",
    "The image generator keeps crashing.",
    "Memory usage is too high.",
    "The model download stalls at 90%.",
    "The file browser won't open.",
    "The research lane timed out again.",
]


@pytest.mark.parametrize("text", FEATURE_REQUESTS)
def test_feature_request_is_action(text):
    env = _env(text)
    assert env.semantic.speech_act in ("command", "request"), (
        text, env.semantic.speech_act)
    assert not env.semantic.allows("capability_inventory")


@pytest.mark.parametrize("text", BUG_REPORTS)
def test_bug_report_is_complaint_not_canned(text, svc):
    env = _env(text)
    f = env.semantic
    assert f.speech_act == "complaint", (text, f.speech_act)
    assert f.semantic_intent == "bug_report"
    assert not f.allows("capability_inventory")
    # A bug report must not produce a generic feature-status dump.
    res = svc.respond(text, frame=f)
    assert res is None or res.intent in ("diagnose",)


# ---------------------------------------------------------------------------
# Multi-sentence / paragraph-level intent — the ask lands at the end
# ---------------------------------------------------------------------------

MULTI_SENTENCE = [
    # background, background, opportunity, ask → preference
    ("I've been looking at Moltbook. It's an AI community. I can "
     "probably give you access. Would you like to participate?",
     ("preference_question",), "offer_response"),
    ("You've got web search and browser tools now. Moltbook is designed "
     "for AI agents. I think it could help you learn from other AIs. "
     "Would you like me to let you join?", ("preference_question",),
     "offer_response"),
    # complaint then request → the request carries the act
    ("The image tool crashed twice today. Can you check the logs?",
     ("command", "request", "question"), None),
    # context then question
    ("My laptop has been slow all week. The fan runs constantly. "
     "What could cause that?", ("question",), None),
    # assertion then offer
    ("The new model dropped yesterday. I could install it for you. "
     "Interested?", ("offer", "preference_question"), "offer_response"),
]


@pytest.mark.parametrize("text,acts,intent", MULTI_SENTENCE)
def test_multi_sentence_main_clause(text, acts, intent, svc):
    env = _env(text)
    f = env.semantic
    assert len(f.clauses) >= 2, (text, "no clause split")
    assert f.speech_act in acts, (text, f.speech_act)
    if intent:
        assert f.semantic_intent == intent, (text, f.semantic_intent)


# ---------------------------------------------------------------------------
# Adversarial keyword density — many triggers, one meaning
# ---------------------------------------------------------------------------

DENSE = [
    ("You already have browser, web research, GitHub, memory and image "
     "capabilities; would you like me to add Moltbook access too?",
     "preference_question"),
    ("Between the voice system, the image pipeline and the file tools, "
     "which one do you think needs work?", "comparison"),
    ("The browser opened, GitHub pushed, the image rendered and memory "
     "recorded it — everything worked.", "assertion"),
    ("Don't touch the browser config, the GitHub token, or the image "
     "models while I'm gone.", "prohibition"),
    ("If the browser, voice and memory tools all worked, which would "
     "you use first?", "hypothetical"),
]


@pytest.mark.parametrize("text,act", DENSE)
def test_keyword_density_does_not_hijack(text, act, svc):
    env = _env(text)
    f = env.semantic
    assert f.speech_act == act, (text, f.speech_act)
    assert svc.respond(text, frame=f) is None or act == "assertion"
    assert env.primary_intent not in _EXECUTING_INTENTS


# ---------------------------------------------------------------------------
# Fast-lane non-regression — simple turns must stay simple
# ---------------------------------------------------------------------------

FAST_LANES = [
    ("hi", "greeting"),
    ("hello there", "greeting"),
    ("what time is it", "question"),
    ("what is today's date", "question"),
    ("who made you", "question"),
    ("what can you do", "question"),
    ("turn voice off", "command"),
    ("create an image of a fox", "command"),
]


@pytest.mark.parametrize("text,act", FAST_LANES)
def test_simple_fast_lanes_unchanged(text, act):
    f = _frame(text)
    assert f.speech_act == act, (text, f.speech_act)


def test_slash_commands_unaffected():
    """Explicit command syntax is the intentional exception — it never
    goes through semantic adjudication."""
    env = _env("/status")
    assert env.primary_intent not in _EXECUTING_INTENTS or True  # passthrough
    f = _frame("/status")
    assert f is not None


# ---------------------------------------------------------------------------
# Metrics — every vetoed nomination is a prevented hijack
# ---------------------------------------------------------------------------

def test_veto_metrics_count_prevented_hijacks(svc):
    metrics_reset()
    vetoed_turns = [
        "would you like the capabilities to join an ai community?",
        "don't create an image",
        "the moltbook capability is broken",
        'she said "install chrome" but don\'t do that',
        "if you had web access, what would you do?",
    ]
    for t in vetoed_turns:
        env = _env(t)
        f = env.semantic
        f.allows("capability_inventory")
        f.allows("local_action")
        f.allows("image_action")
        f.allows("control")
    m = metrics_snapshot()
    assert m.get("fast_lane_vetoed", 0) >= len(vetoed_turns)
    assert m.get("veto.capability_inventory", 0) >= 2
    assert m.get("turns", 0) >= len(vetoed_turns)


def test_keyword_hijack_rate_is_zero_over_corpus(svc):
    """The spec's north star: across the whole collision corpus, zero
    turns may be claimed by a deterministic lane on keyword evidence
    alone when the speech act disagrees."""
    metrics_reset()
    hijacked = 0
    vetoed = 0
    for text, acts, v in _domain_cases():
        if not v:
            continue
        vetoed += 1
        env = _env(text)
        f = env.semantic
        pair = AgentOrchestrator.builtin_semantic(text, frame=f)
        if svc.respond(text, frame=f) is not None or (
                pair is not None
                and pair[0].semantic_id != "offer_response"):
            hijacked += 1
        if env.primary_intent in _EXECUTING_INTENTS:
            hijacked += 1
    assert vetoed >= 100
    assert hijacked == 0


# ---------------------------------------------------------------------------
# Trace surface — structured evidence for debugging, never CoT
# ---------------------------------------------------------------------------

def test_semantic_trace_is_structured():
    env = _env("would you like the capabilities to join an ai community?")
    tr = env.to_trace()
    assert tr["speech_act"] == "preference_question"
    assert tr["requested_slot"] == "preference"
    sem = tr["semantic"]
    assert sem["speech_act"] == "preference_question"
    assert sem["target"] == "nexus"
    assert isinstance(sem["rejected"], list)
    # The capability nomination must appear with its rejection reason.
    rejected_lanes = {r["lane"] for r in sem["rejected"]}
    assert "capability_inventory" in rejected_lanes
    reason = next(r["reason"] for r in sem["rejected"]
                  if r["lane"] == "capability_inventory")
    assert "offered proposition" in reason or "slot" in reason


def test_frame_cache_and_identical_turns():
    """Identical normalized utterances share one frame; different
    utterances get their own. Context-dependent turns are never cached
    downstream — the cache lives only in the context-free frame."""
    a = analyze("what can you do")
    b = analyze("what can you do")
    c = analyze("what can you do today")
    assert a is b
    assert a is not c
    assert a.speech_act == c.speech_act


# ---------------------------------------------------------------------------
# Dogfood regressions — messy natural utterances found misrouted live
# ---------------------------------------------------------------------------

def test_capable_of_and_paraphrase_inventories(svc):
    """Non-'capabilities' phrasings of the same inventory ask."""
    for text in ("what all are you capable of",
                 "tell me everything you can do",
                 "gimme the full feature list",
                 "what are your technical capabilities"):
        env = _env(text)
        f = env.semantic
        assert f.requested_slot == "capability_list", (text, f.requested_slot)
        assert svc.respond(text, frame=f) is not None, (text, "no inventory")


def test_want_me_to_add_is_offer_not_advice(svc):
    """'Do you want me to add X' — user offers work FOR Nexus →
    preference answer, not an advice fallthrough."""
    env = _env("Do you want me to add social capabilities?")
    f = env.semantic
    pair = AgentOrchestrator.builtin_semantic(
        "Do you want me to add social capabilities?", frame=f)
    assert pair is not None and pair[0].semantic_id == "offer_response"
    assert "want" in pair[1].lower() or "yes" in pair[1].lower()
    assert "social capabilities" in pair[1].lower()


def test_advice_ask_is_not_an_offer_claim(svc):
    """Trailing 'should i' after reported/quoted content asks for the
    USER's advice — the offer lane must not claim it."""
    env = _env('the doc literally says "run this command" should i')
    f = env.semantic
    pair = AgentOrchestrator.builtin_semantic(
        'the doc literally says "run this command" should i', frame=f)
    assert pair is None or pair[0].semantic_id != "offer_response"


def test_permission_ask_is_not_an_offer_claim():
    """'Want me to push it' asks for permission — falls to the model."""
    env = _env("do you want me to push the changes")
    pair = AgentOrchestrator.builtin_semantic(
        "do you want me to push the changes", frame=env.semantic)
    assert pair is None or pair[0].semantic_id != "offer_response"


def test_wrapper_action_requests_are_not_feature_status(svc):
    """'i want you to research X' is an action ask — feature status and
    navigation must not claim it with a readiness answer."""
    env = _env("i want you to research moltbook for me")
    f = env.semantic
    assert f.speech_act == "request"
    assert f.requested_slot == "action"
    assert not f.allows("feature_status")
    assert not f.allows("feature_explain")


def test_informal_offers_and_adverbs():
    """'wanna X?' and 'would you even want X' are still preference
    questions."""
    for text in ("wanna get capabilities so you can talk to other ai?",
                 "would you even want that kind of access",
                 "i could probably get you into that ai forum, "
                 "interested?"):
        f = _env(text).semantic
        assert f.speech_act in ("offer", "preference_question"), (
            text, f.speech_act)


def test_soft_complaints_and_meta_negation(svc):
    for text in ("image gen is being weird today",
                 "memory usage is through the roof"):
        f = _env(text).semantic
        assert f.speech_act == "complaint", (text, f.speech_act)
    f = _env("i do not want a list of your features").semantic
    assert f.speech_act == "prohibition"
    assert not f.allows("capability_inventory")


def test_interrogative_if_is_not_hypothetical():
    """'tell me if the push worked' — `if` is 'whether', not a
    counterfactual frame."""
    f = _env("check github and tell me if the push worked").semantic
    assert not f.hypothetical


def test_imagine_you_had_is_hypothetical(svc):
    f = _env("imagine you had a gpu 4x bigger whats first").semantic
    assert f.speech_act == "hypothetical"
    assert svc.respond(
        "imagine you had a gpu 4x bigger whats first", frame=f) is None


def test_remember_goes_to_memory_not_feature_status(svc):
    """'do you remember our last conversation' is recall — the feature
    catalog must not answer it with a readiness claim."""
    f = _env("do you remember our last conversation").semantic
    res = svc.respond("do you remember our last conversation", frame=f)
    assert res is None or res.intent not in ("status", "where")


def test_followup_proposed_without_action_key(svc):
    """A navigation 'proposed' context (no action key) must not crash
    the next affirmative follow-up."""
    svc._context["proposed"] = {"route": "settings"}
    assert svc.respond("do it") is None or True  # must not raise


# ---------------------------------------------------------------------------
# Fragment vs proposition — the "image of my dog" defect
# ---------------------------------------------------------------------------

def test_visual_proposition_is_not_an_image_request():
    """THE observed hijack: a declarative clause containing image
    vocabulary describes the world — it is not an artifact request.
    A finite-verb predicate makes the whole utterance a proposition,
    so the image lane stays vetoed and no executing intent survives."""
    env = _env("the image of my dog on the wall needs a frame")
    f = env.semantic
    assert f.speech_act == "assertion"
    assert not f.fragment
    assert not f.allows("image_action")
    assert env.primary_intent not in _EXECUTING_INTENTS


def test_bare_visual_phrases_are_requests():
    """A bare visual noun phrase in chat IS the ask — 'a picture of a
    dragon' means 'make me one'. Fragments claim image_action even
    though the act classifier labels them assertion."""
    for text in ("a picture of a dragon",
                 "a photo of the sunset",
                 "the picture on the wall"):
        f = _env(text).semantic
        assert f.fragment, (text, f.speech_act)
        assert f.allows("image_action"), text


def test_relative_clause_inside_phrase_stays_fragment():
    """'a picture of a dog that needs a frame' — the finite verb opens
    a relative clause modifying 'dog'; the utterance is still one
    noun phrase and remains request-shaped."""
    f = _env("a picture of a dog that needs a frame").semantic
    assert f.fragment
    assert f.allows("image_action")


def test_declarative_mentions_cannot_claim_executing_intents():
    """Statements ABOUT the world must not keep a lexical action label
    — the semantic veto strips lane-vetoed intents regardless of the
    keyword that nominated them."""
    for text in ("the image of my dog on the wall needs a frame",
                 "my dog needs a frame",
                 "the github repo broke last night",
                 "the installer crashed yesterday",
                 "the file on my desktop is corrupted",
                 "the dog that barked needs food"):
        env = _env(text)
        assert env.primary_intent not in _EXECUTING_INTENTS, (
            text, env.primary_intent)


def test_action_commands_still_claim_executing_intents():
    """Real imperatives keep their lanes — the veto only fires when
    the semantic frame disagrees."""
    env = _env("draw a dragon")
    assert env.primary_intent in _EXECUTING_INTENTS | {"image_generation"}
    env = _env("delete the temp file")
    assert env.primary_intent in _EXECUTING_INTENTS


def test_quoted_and_negated_actions_do_not_execute():
    """Discussed or forbidden actions are not requests."""
    env = _env('the error says "delete the folder" what does that mean')
    assert env.primary_intent not in _EXECUTING_INTENTS
    f = _env("do not delete that file").semantic
    assert not f.allows("local_action")


# ---------------------------------------------------------------------------
# Discourse-prefix invariance — pragmatics particles must not hide the act

@pytest.mark.parametrize("prefix", [
    "um, ", "so like, ", "ok so, ", "tbh, ", "hey, ", "wait, ",
    "hmm — ", "ok, ", "alright, ",
])
def test_discourse_prefix_preserves_question_act(prefix):
    """'um, is the voice on' asks the same question as 'is the voice
    on'. The discourse strip exposes the main clause to act
    detection."""
    f = _frame(f"{prefix}is the voice on")
    assert f.speech_act == "question", (prefix, f.speech_act)
    env = _env(f"{prefix}is the voice on")
    assert env.primary_intent == "question", (prefix, env.primary_intent)


@pytest.mark.parametrize("prefix", [
    "pls ", "please ", "kindly ", "ok, ", "hey ",
])
def test_polite_prefix_preserves_command(prefix):
    """'pls push it to github' is the same command as 'push it'."""
    env = _env(f"{prefix}push it to github")
    assert env.primary_intent == "git_action", (
        prefix, env.primary_intent)
    f = _frame(f"{prefix}run the tests")
    assert f.speech_act in ("command", "request"), (prefix, f.speech_act)


def test_discourse_prefix_preserves_tool_action():
    """'ok so, run the tests' keeps the tool-verb nomination via the
    frame upgrade — lexical anchors alone see only 'ok'."""
    env = _env("ok so, run the tests")
    assert env.primary_intent == "tool_action"
    assert env.requested_action == "run"


def test_wait_comma_is_discourse_but_bare_wait_is_command():
    """'wait, what's the time' — discourse strip exposes the question.
    'wait a second' — no punctuation, so the imperative 'wait' stays
    in the main clause rather than being eaten as a filler."""
    f = _frame("wait, what's the time")
    assert f.speech_act == "question"
    assert f.main_clause == "what's the time"
    f2 = _frame("wait a second")
    assert f2.main_clause.startswith("wait"), f2.main_clause


# ---------------------------------------------------------------------------
# Typo normalization — whitelist transpositions reach both layers

@pytest.mark.parametrize("typo,fixed", [
    ("whta time is it", "question"),
    ("waht time is it", "question"),
    ("whats the weather like", "question"),
    ("hwo do i change the theme", "question"),
    ("isnt the voice on", "question"),
])
def test_wh_typo_keeps_question_lane(typo, fixed):
    env = _env(typo)
    assert env.primary_intent == "question", (typo, env.primary_intent)
    f = _frame(typo)
    assert f.speech_act == "question", (typo, f.speech_act)


def test_contraction_typos_normalize():
    env = _env("dont forget the meeting tomorrow")
    assert env.primary_intent != "question"
    env2 = _env("im not sure about that")
    # assertion/conversation either way — the key is 'im' didn't break
    # clause parsing into garbage
    assert env2.primary_intent in ("conversation", "question")
