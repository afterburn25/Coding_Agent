"""Persona speech soak — empirical audit of the Persona Speech Genome.

Not a unittest — a measurement harness. Exercises the real production
render path (PersonaRenderer.render_semantic + builtin_semantic lanes)
across strongly different personas, speech acts, repeated asks, and
multi-turn sessions, then reports the Part-26 metrics:

  exact/near-duplicate rate, opening/closing reuse, address rate,
  micro-reaction rate, serious-context humor leak, fact drift,
  exact-span corruption, persona distinctiveness.

Run:  python tests/soak_persona.py [--turns N]
"""
from __future__ import annotations

import random
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")

from localcodeagent.context.realize import (
    PersonaRenderer, RenderContext, SemanticResponse, fingerprint,
    opening_of, closing_of, similarity, classify_speech_act)
from localcodeagent.personality.genome import derive_genome
from localcodeagent.personality import presets as P


# ---------------------------------------------------------------- presets
ALL = {p["id"]: p for grp in dir(P)
       if grp.isupper() and isinstance(getattr(P, grp), list)
       for p in getattr(P, grp)}

PERSONAS = {
    "formal": ALL.get("sophisticated") or ALL.get("professional") or ALL["elegant"],
    "playful": ALL.get("energetic") or ALL["enthusiastic"],
    "nerdy": ALL.get("nerdy") or ALL.get("analytical") or ALL["curious"],
    "terse": ALL.get("minimalist") or ALL["concise"],
}
print("personas:", {k: v["id"] for k, v in PERSONAS.items()})

GENOMES = {k: derive_genome(v) for k, v in PERSONAS.items()}

# ---------------------------------------------------------------- fixtures
# Part 8 lane set — the asks the user explicitly listed.
LANES = [
    ("capability", "answer", "I can browse repositories, write and edit code, run tests, manage git, generate images, research topics, and keep track of long-running missions.", []),
    ("identity", "answer", "I am Nexus Core, the local assistant on this machine.", []),
    ("identity:creator", "answer", "I was created by the person who built this Nexus Core installation.", []),
    ("status:github", "answer", "GitHub is connected. The remote is configured and reachable.", []),
    ("status:model", "answer", "I am currently using the default local model route.", []),
    ("status:tests", "report_success", "The test suite passed — 37 of 37 tests green.", ["37"]),
    ("status:changes", "answer", "The last change touched the splash renderer and the delivery plan.", []),
    ("status:work", "answer", "I am working on the persona speech milestone closeout.", []),
    ("capability:image", "answer", "Yes — image generation runs through InvokeAI or ComfyUI on this machine.", []),
]

# A technical answer carrying exact spans that must survive verbatim.
TECH_SEM = SemanticResponse(
    semantic_id="tech:vram-fix",
    facts=["The CUDA out-of-memory error came from the 24GB pool cap in invokeai.yaml."],
    conclusions=["Raising pool to 18GB fixed it."],
    speech_act="answer",
    confidence="verified",
    exact_spans=["invokeai.yaml", "24GB", "18GB"])

SERIOUS_SEM = SemanticResponse(
    semantic_id="serious:disk-loss",
    facts=["The database file was deleted and there is no backup snapshot."],
    speech_act="alert",
    confidence="verified",
    exact_spans=["no backup snapshot"])

UNCERTAIN_SEM = SemanticResponse(
    semantic_id="guess:cause",
    facts=["The slowdown probably comes from the indexer."],
    speech_act="answer",
    confidence="inferred")


def _mkctx(**kw):
    base = dict(mood="relaxed", register="casual",
                relationship_stage="friend", familiarity=0.7)
    base.update(kw)
    return RenderContext(**base)


def run_soak(turns: int = 600):
    rng = random.Random(20261005)
    r = PersonaRenderer(rng=rng)

    metrics = {
        "rendered": 0, "exact_dups": 0, "near_dups": 0,
        "openings": Counter(), "closings": Counter(),
        "address_used": 0, "micro": 0, "humor_in_serious": 0,
        "fact_drift": 0, "span_corrupt": 0, "empty": 0,
        "by_persona": defaultdict(list),
    }
    seen_texts: dict[str, set] = defaultdict(set)

    def check(sem: SemanticResponse, out, lane_key: str):
        text = out.text
        metrics["rendered"] += 1
        if not text.strip():
            metrics["empty"] += 1
            return
        fp = fingerprint(text)
        if fp in seen_texts[lane_key]:
            metrics["exact_dups"] += 1
        else:
            for prior_fp_text in seen_texts[lane_key]:
                pass
        seen_texts[lane_key].add(fp)
        # near-dup via similarity against kept samples
        metrics["by_persona"][lane_key].append(text)
        metrics["openings"][opening_of(text)] += 1
        metrics["closings"][closing_of(text)] += 1
        if out.used_address:
            metrics["address_used"] += 1
        if out.micro_reaction:
            metrics["micro"] += 1
        # fact drift: canonical facts / conclusions must appear verbatim
        for fact in sem.facts:
            if fact and fact not in text:
                metrics["fact_drift"] += 1
        for span in sem.exact_spans:
            if span and span not in text:
                metrics["span_corrupt"] += 1

    # ---- Part 26 main soak: personas × lanes × repeats -------------------
    reps = turns // (len(PERSONAS) * len(LANES)) + 1
    for rep in range(reps):
        for pk, genome in GENOMES.items():
            for sid, act, canon, spans in LANES:
                sem = SemanticResponse(
                    semantic_id=sid, speech_act=act,
                    facts=[canon], exact_spans=spans)
                ctx = _mkctx(register=rng.choice(
                    ["casual", "technical", "casual"]))
                out = r.render_semantic(sem, genome, ctx,
                                        intent=sid.split(":")[0],
                                        canonical=canon)
                check(sem, out, f"{pk}:{sid}")

    # ---- serious contexts must not joke -----------------------------------
    for pk, genome in GENOMES.items():
        for i in range(30):
            ctx = _mkctx(seriousness=3, register="system_alert")
            out = r.render_semantic(SERIOUS_SEM, genome, ctx,
                                    intent="alert", canonical="")
            if out.micro_reaction and out.micro_reaction in (
                    "laugh", "chuckle", "giggle"):
                metrics["humor_in_serious"] += 1
            for span in SERIOUS_SEM.exact_spans:
                if span not in out.text:
                    metrics["span_corrupt"] += 1

    # ---- uncertain vs verified confidence phrasing -----------------------
    conf_seen = {"verified": Counter(), "inferred": Counter()}
    for pk, genome in GENOMES.items():
        for i in range(20):
            for sem in (TECH_SEM, UNCERTAIN_SEM):
                out = r.render_semantic(sem, genome, _mkctx(),
                                        intent="answer")
                conf_seen[sem.confidence][opening_of(out.text)] += 1

    # ---------------------------------------------------------------------
    n = metrics["rendered"]
    print("\n=== SOAK RESULTS ===")
    print(f"rendered:              {n}")
    print(f"exact duplicates:      {metrics['exact_dups']}  "
          f"({100*metrics['exact_dups']/max(1,n):.2f}%)")
    print(f"empty renders:         {metrics['empty']}")
    print(f"address used:          {metrics['address_used']}  "
          f"({100*metrics['address_used']/max(1,n):.1f}%)")
    print(f"micro-reactions:       {metrics['micro']}  "
          f"({100*metrics['micro']/max(1,n):.1f}%)")
    print(f"fact drift:            {metrics['fact_drift']}")
    print(f"exact-span corruption: {metrics['span_corrupt']}")
    print(f"serious humor leaks:   {metrics['humor_in_serious']}")
    top_open = metrics["openings"].most_common(5)
    top_close = metrics["closings"].most_common(5)
    print(f"top openings:          {top_open}")
    print(f"top closings:          {top_close}")

    # near-dup rate within each lane
    ndup = 0
    total_pairs = 0
    for lane, texts in metrics["by_persona"].items():
        uniq = set(texts)
        for i, a in enumerate(texts[:80]):
            for b in list(uniq)[:80]:
                if a != b and similarity(a, b) > 0.92:
                    ndup += 1
                total_pairs += 1
    print(f"near-dup pairs:        {ndup} (of {total_pairs} compared)")

    # distinctiveness: persona -> avg pairwise similarity for same lane
    print("\n=== DISTINCTIVENESS (lower = more distinct) ===")
    for sid, act, canon, spans in LANES[:4]:
        per = {pk: metrics["by_persona"][f"{pk}:{sid}"][:20]
               for pk in PERSONAS}
        keys = list(per)
        sims = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for a in per[keys[i]][:8]:
                    for b in per[keys[j]][:8]:
                        sims.append(similarity(a, b))
        print(f"  {sid:22s} avg cross-persona similarity "
              f"{sum(sims)/max(1,len(sims)):.3f}")

    # repeat evolution — same ask 6×, must progress not loop
    print("\n=== REPEAT EVOLUTION (terse persona, same ask ×6) ===")
    r2 = PersonaRenderer(rng=random.Random(3))
    for i in range(6):
        sem = SemanticResponse(semantic_id="capability",
                               speech_act="answer",
                               facts=[LANES[0][2]])
        out = r2.render_semantic(sem, GENOMES["terse"], _mkctx(),
                                 intent="capability",
                                 canonical=LANES[0][2])
        print(f"  ask{i+1} [rep={out.repeat_index} "
              f"open={out.opening_family}] {out.text[:80]}")

    return metrics


def run_session(turns: int = 120):
    """Multi-turn session — one persona, realistic turn mix."""
    rng = random.Random(77)
    r = PersonaRenderer(rng=rng)
    genome = GENOMES["nerdy"]
    script = []
    # realistic beat mix
    beats = (
        ["casual"] * 4 + ["tech"] * 8 + ["success"] * 3
        + ["failure"] * 4 + ["correction"] * 3 + ["casual"] * 3
        + ["tech"] * 6 + ["uncertain"] * 4 + ["success"] * 2
        + ["alert"] * 2 + ["casual"] * 5)
    while len(script) < turns:
        script.extend(beats)
    seen = set()
    dups = 0
    micros = 0
    addresses = 0
    for i, beat in enumerate(script[:turns]):
        if beat == "casual":
            sem = SemanticResponse(
                semantic_id=f"chat:{i % 7}", speech_act="answer",
                facts=[f"That tracks — thread {i % 7} is still open."])
            ctx = _mkctx(mood=rng.choice(["relaxed", "focused"]))
        elif beat == "tech":
            sem = SemanticResponse(
                semantic_id=f"tech:{i % 9}", speech_act="answer",
                facts=[f"The bug is in module {i % 9}: the retry loop "
                       "never released the lock."],
                exact_spans=[f"module {i % 9}"])
            ctx = _mkctx(register="technical")
        elif beat == "success":
            sem = SemanticResponse(
                semantic_id=f"ok:{i % 4}", speech_act="report_success",
                facts=["The fix landed and the suite is green."])
            ctx = _mkctx(social_cue="celebrating")
        elif beat == "failure":
            sem = SemanticResponse(
                semantic_id=f"fail:{i % 4}", speech_act="report_failure",
                facts=["The migration failed: constraint violation."])
            ctx = _mkctx(social_cue="frustrated", seriousness=1)
        elif beat == "correction":
            sem = SemanticResponse(
                semantic_id=f"fix:{i % 3}", speech_act="acknowledge_mistake"
                if "acknowledge_mistake" in
                __import__("localcodeagent.personality.genome",
                           fromlist=["SPEECH_ACTS"]).SPEECH_ACTS
                else "answer",
                facts=["You're right — I had the wrong branch."])
            ctx = _mkctx(social_cue="frustrated")
        elif beat == "uncertain":
            sem = SemanticResponse(
                semantic_id=f"unc:{i % 5}", speech_act="admit_uncertainty",
                facts=["I can't tell which build produced this binary."],
                confidence="uncertain")
            ctx = _mkctx()
        else:  # alert
            sem = SERIOUS_SEM
            ctx = _mkctx(seriousness=3)
        out = r.render_semantic(sem, genome, ctx, intent=beat)
        t = out.text
        if t in seen:
            dups += 1
        seen.add(t)
        if out.micro_reaction:
            micros += 1
        if out.used_address:
            addresses += 1
    print("\n=== MULTI-TURN SESSION ===")
    print(f"turns: {turns}  exact-dup: {dups}  micros: {micros} "
          f"({100*micros/turns:.0f}%)  address: {addresses} "
          f"({100*addresses/turns:.0f}%)")


if __name__ == "__main__":
    turns = 600
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])
    run_soak(turns)
    run_session(120)
