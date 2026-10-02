# Hardware Model Tiers

The tier system decides which local text models a machine should run and
how each executes (GPU-native, hybrid GPU+CPU, or CPU-only). The catalog
is the single source of truth for installers, the runtime planner, and
the role ladder.

## Catalog

`localcodeagent/models/model_tiers.json` — one entry per tier:

| Tier | Model | Ladder role | Native VRAM | Hybrid VRAM | RAM (hybrid) |
|------|-------|-------------|------------|-------------|--------------|
| 1 | Qwen3 4B Instruct Q4_K_M | utility | 3.5 GB | — | — |
| 2 | Qwen3 8B Q4_K_M | lightweight_reasoner | 6.0 GB | — | — |
| 3 | Qwen3 14B Q4_K_M | primary_coder | 10.5 GB | 4 GB | 24 GB usable |
| 4 | Qwen3-Coder 30B-A3B Q4_K_M | deep_reasoner | 20.0 GB | 10 GB | 48 GB usable |

Exact thresholds, URLs, SHA-256 digests, and file sizes live in the JSON
— never copy them into other files by hand.

## Rules

- **Dedicated VRAM only.** Shared Windows GPU memory and pagefile are
  reported but never count toward GPU-native qualification.
- **Usable RAM** = `total − max(10 GB, 15% of total)`. A model that needs
  swap is not supported.
- **Everything that qualifies installs.** The largest supported tier and
  every tier below it are selected — the escalation ladder needs them all
  resident.
- **One-step hybrid ceiling.** Hybrid offload may reach at most
  `max_tier_distance` (1) tier above the highest GPU-native tier. Example:
  a 12 GB card runs tiers 1–3 natively and may qualify tier 4 as hybrid;
  a 4 GB card (tier 1 native) cannot hybrid into tier 3 or 4 regardless
  of system RAM.
- **Disk gate.** `free < download + 5 GB` deselects everything.
- **Multi-GPU is not pooled.** Only the largest single device counts;
  model splitting is not assumed.

## Planner

`plan_model_stack(hardware_profile(disk_path))` returns a `ModelPlan`
with a `TierDecision` per catalog entry: execution mode, a human reason
string, `near_native` flag (native but under recommended VRAM), and
supported/selected state. `GET /api/model-plan` exposes the live plan.

Example on a 12 GB RTX 3080 Ti + 64 GB RAM: tiers 1–3 `gpu_native`,
tier 4 `hybrid_gpu_cpu` — exactly the intended "12 GB + 64 GB → 30B
hybrid" case.

## Role ladder

`canonical_role()` maps legacy/extra role names onto the four ladder
roles (`utility`, `lightweight_reasoner`, `primary_coder`,
`deep_reasoner`); `tier_for_role()` gives the rung number. The model
router uses canonical-role matching, so a model configured as
`light_coder` still serves `lightweight_reasoner` requests, and unknown
roles fall back to the primary-coder rung.

## Installer sync

`installer/ChatNexus.iss` ships two catalog entries as compile-time
`#define` blocks (Inno cannot read JSON). Keep them in sync:

    python scripts/sync_model_catalog.py          # rewrite drift
    python scripts/sync_model_catalog.py --check  # CI check

`tests/test_model_tiers.py` runs the `--check` automatically.
