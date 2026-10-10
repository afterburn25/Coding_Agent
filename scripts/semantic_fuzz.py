#!/usr/bin/env python3
"""Semantic mutation harness — meaning-preserving rewrites of the same
utterance must resolve to the same intent signature through
understand_turn(). Meaning-changing transforms (negation, quotation,
conditionals, modals) are contrast controls and MUST diverge.

Usage:
    python scripts/semantic_fuzz.py [--seed 7] [--json out.json]

Report: for each base utterance, the mutations are grouped by
(primary_intent, speech_act, semantic_intent). A base with >1 group has
a consistency defect — the divergence table lists which transform
caused the split.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from localcodeagent.context.intent import understand_turn

# ---------------------------------------------------------------------
# Meaning-preserving transforms. Each takes (text, rng) -> text.
# ---------------------------------------------------------------------

_FILLERS = ["um, ", "so like, ", "ok so, ", "hey, ", "hmm — ",
            "wait, ", "tbh, ", "real quick — "]
_POLITES = ["please ", "kindly ", "pls "]
_SLANG = [(r"\byou\b", "u"), (r"\bare\b", "r"), (r"\byour\b", "ur"),
          (r"\bwhat's\b", "whats"), (r"\bwhat is\b", "whats"),
          (r"\bplease\b", "pls"), (r"\bthanks\b", "thx"),
          (r"\breally\b", "rly"), (r"\bbecause\b", "cuz")]


def m_caseflip(t, rng):
    return "".join(c.upper() if rng.random() < 0.4 else c for c in t)


def m_decap(t, rng):
    return t.lower()


def m_strip_punct(t, rng):
    return re.sub(r"[?!.,;]+$", "", t)


def m_slang(t, rng):
    out = t
    for pat, rep in rng.sample(_SLANG, k=min(2, len(_SLANG))):
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return out


def m_typo(t, rng):
    """Adjacent-letter transposition inside a content word — humans
    still read it; the pipeline's typo normalization should too."""
    words = [w for w in re.finditer(r"[a-zA-Z]{4,}", t)]
    if not words:
        return t
    w = rng.choice(words)
    s = w.group(0)
    i = rng.randrange(1, len(s) - 1)
    sw = s[:i] + s[i + 1] + s[i] + s[i + 2:]
    return t[:w.start()] + sw + t[w.end():]


def m_filler(t, rng):
    return rng.choice(_FILLERS) + t


def m_polite(t, rng):
    return rng.choice(_POLITES) + t


def m_trailing_softener(t, rng):
    return re.sub(r"[?!.]*$", "", t) + rng.choice(
        [" if that's ok", ", yeah?", ", thanks", " — whenever"])


def m_doubled_word_drop(t, rng):
    """'the the tests' -> 'the tests' — a stutter artifact."""
    return re.sub(r"\b(\w+) \1\b", r"\1", t, count=1)


PRESERVING = [
    m_caseflip, m_decap, m_strip_punct, m_slang, m_typo, m_filler,
    m_polite, m_trailing_softener, m_doubled_word_drop,
]

# ---------------------------------------------------------------------
# Contrast transforms — these CHANGE the speech act and must diverge.
# ---------------------------------------------------------------------


def c_negate(t, rng):
    return f"don't {t.lstrip('please ')}"


def c_quote(t, rng):
    # Bare quotation — quoted content is discussed text, never the act.
    return f'someone wrote "{t}" in the notes'


def c_hypothetical(t, rng):
    return f"hypothetically speaking, suppose {t.lstrip('please ')}"


def c_modal(t, rng):
    return f"if you had to, you would {t.lstrip('please ')}"


CONTRAST = [c_negate, c_quote, c_hypothetical, c_modal]

# ---------------------------------------------------------------------
# Bases — representative utterances across the lane surface.
# ---------------------------------------------------------------------

BASES = [
    "what time is it",
    "run the tests please",
    "what's my favorite color",
    "tell me about the borrow checker",
    "push it to github",
    "summarize what we talked about",
    "what port did we decide on",
    "is the voice on",
    "how do i change the theme",
    "what's the weather like",
]


def signature(text: str) -> tuple:
    env = understand_turn(text)
    frame = getattr(env, "semantic", None)
    return (env.primary_intent,
            getattr(frame, "speech_act", "") or "",
            env.semantic_intent or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default="")
    ap.add_argument("--mutations", type=int, default=6,
                    help="preserving mutations sampled per base")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    divergences = []
    contrast_failures = []
    total = 0
    for base in BASES:
        base_sig = signature(base)
        groups = {base_sig: ["<base>"]}
        fns = rng.sample(PRESERVING, k=min(args.mutations,
                                         len(PRESERVING)))
        for fn in fns:
            mut = fn(base, rng)
            if mut == base:
                continue
            total += 1
            groups.setdefault(signature(mut), []).append(
                f"{fn.__name__}: {mut!r}")
        if len(groups) > 1:
            divergences.append((base, base_sig, groups))
        for fn in CONTRAST:
            mut = fn(base, rng)
            sig = signature(mut)
            if sig == base_sig:
                contrast_failures.append((base, fn.__name__, mut))

    print(f"{total} preserving mutations over {len(BASES)} bases")
    print(f"consistent bases: {len(BASES) - len(divergences)}/{len(BASES)}")
    if divergences:
        print(f"\n{len(divergences)} base(s) diverge:")
        for base, sig, groups in divergences:
            print(f"\n  base {base!r} -> {sig}")
            for s, ms in groups.items():
                if s != sig:
                    print(f"    DIVERGENT {s}:")
                    for m in ms:
                        print(f"      {m}")
    if contrast_failures:
        print(f"\n{len(contrast_failures)} contrast control(s) FAILED "
              f"(a meaning change kept the signature):")
        for base, fn, mut in contrast_failures:
            print(f"  {fn}({base!r}) = {mut!r} -> same signature")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "divergences": [
                {"base": b, "base_sig": list(s),
                 "groups": {str(k): v for k, v in g.items()}}
                for b, s, g in divergences],
            "contrast_failures": contrast_failures,
        }, indent=2))
    return 1 if (divergences or contrast_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
