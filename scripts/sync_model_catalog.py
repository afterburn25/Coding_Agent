"""Keep installer model #defines in sync with the tier catalog.

The Inno Setup script hardcodes the two setup-time downloads
(Qwen14/Qwen30 define blocks) because Inno `#define`s must exist at
compile time — it cannot read JSON. The canonical values live in
localcodeagent/models/model_tiers.json; this script rewrites the define
blocks (or, with --check, exits non-zero on drift).

    python scripts/sync_model_catalog.py [--check]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "localcodeagent" / "models" / "model_tiers.json"
ISS = ROOT / "installer" / "ChatNexus.iss"

# catalog id → Inno define prefix (installer-shipped subset only)
ISS_PREFIX = {
    "qwen3-14b-q4-k-m": "Qwen14",
    "qwen3-coder-30b-a3b-q4-k-m": "Qwen30",
}


def _block(prefix: str, tier: dict) -> str:
    return (
        f'#define {prefix}CatalogId "{tier["id"]}"\n'
        f'#define {prefix}FileName "{tier["filename"]}"\n'
        f'#define {prefix}Url "{tier["url"]}"\n'
        f'#define {prefix}Sha256 "{tier["sha256"]}"\n'
        f'#define {prefix}Size {tier["file_size"]}\n'
        f'#define {prefix}SourceRepo "{tier["source_repo"]}"')


def main() -> int:
    check = "--check" in sys.argv
    tiers = {t["id"]: t for t in
             json.loads(CATALOG.read_text(encoding="utf-8"))["tiers"]}
    text = ISS.read_text(encoding="utf-8")
    new = text
    for tier_id, prefix in ISS_PREFIX.items():
        tier = tiers.get(tier_id)
        if tier is None:
            print(f"error: catalog is missing installer tier {tier_id}")
            return 2
        pattern = re.compile(
            rf'#define {prefix}CatalogId "[^"]*"\n'
            rf'#define {prefix}FileName "[^"]*"\n'
            rf'#define {prefix}Url "[^"]*"\n'
            rf'#define {prefix}Sha256 "[^"]*"\n'
            rf'#define {prefix}Size \d+\n'
            rf'#define {prefix}SourceRepo "[^"]*"')
        if not pattern.search(new):
            print(f"error: no {prefix} define block found in {ISS.name}")
            return 2
        new = pattern.sub(lambda m: _block(prefix, tier), new, count=1)
    if check:
        if new != text:
            print(f"error: {ISS.name} is out of sync with {CATALOG.name} "
                  f"— run scripts/sync_model_catalog.py")
            return 1
        print("model catalog in sync")
        return 0
    if new != text:
        ISS.write_text(new, encoding="utf-8")
        print(f"updated {ISS.name}")
    else:
        print("model catalog already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
