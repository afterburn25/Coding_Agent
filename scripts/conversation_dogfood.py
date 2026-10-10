#!/usr/bin/env python3
"""Conversation dogfood driver — runs hunt packs against a live backend.

Usage:
    python scripts/conversation_dogfood.py --url http://127.0.0.1:58325
    python scripts/conversation_dogfood.py --url ... --packs hijack,drift
    python scripts/conversation_dogfood.py --url ... --long 150 300

Outputs:
    .dogfood/conversation_ledger.jsonl   — FailureCorpus defect ledger
    .dogfood/conversation_report.md      — metrics + per-turn failures
    .dogfood/conversation_transcript.txt — full transcript for review
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from localcodeagent.qa.corpus import FailureCorpus
from localcodeagent.qa.live import LiveRunner, LiveSession, render_report
from localcodeagent.qa.packs import ALL_PACKS, all_scenarios, long_session, minimal_pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:58325")
    ap.add_argument("--packs", default="all",
                    help="comma list of pack names, 'all', or 'pairs'")
    ap.add_argument("--long", type=int, nargs="*", default=[],
                    help="generate seeded long sessions of N turns")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / ".dogfood"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus = FailureCorpus(out_dir / "conversation_ledger.jsonl")
    session = LiveSession(args.url)
    runner = LiveRunner(session, corpus=corpus)

    scenarios = []
    if args.packs == "all":
        scenarios = all_scenarios()
    elif args.packs == "pairs":
        scenarios = minimal_pairs()
    else:
        for name in args.packs.split(","):
            name = name.strip()
            fn = ALL_PACKS.get(name)
            if fn is None:
                print(f"unknown pack: {name} (have: {sorted(ALL_PACKS)})")
                return 2
            scenarios.append(fn())
    for n in args.long:
        scenarios.append(long_session(n, seed=args.seed))

    runs = []
    transcript_lines = []
    for sc in scenarios:
        print(f"[{sc.scenario_id}] {len(sc.turns)} turns...", flush=True)
        run = runner.run(sc)
        runs.append(run)
        nfail = sum(len(t.failures) for t in run.turns)
        print(f"  -> {'OK' if run.ok else str(nfail) + ' FAILURES'}")
        transcript_lines.append(f"===== {sc.scenario_id} =====")
        for t in run.turns:
            transcript_lines.append(f"U[{t.index}]: {t.text}")
            transcript_lines.append(f"A[{t.index}]: {t.response}")
            if t.failures:
                transcript_lines.append(f"  FAIL: {'; '.join(t.failures)}")
        transcript_lines.append("")

    (out_dir / "conversation_report.md").write_text(
        render_report(runs), encoding="utf-8")
    (out_dir / "conversation_transcript.txt").write_text(
        "\n".join(transcript_lines), encoding="utf-8")

    total = sum(len(t.failures) for r in runs for t in r.turns)
    turns = sum(len(r.turns) for r in runs)
    print(f"\n{turns} turns across {len(runs)} scenarios — "
          f"{total} assertion failures")
    print(f"ledger:    {out_dir / 'conversation_ledger.jsonl'}")
    print(f"report:    {out_dir / 'conversation_report.md'}")
    print(f"transcript:{out_dir / 'conversation_transcript.txt'}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
