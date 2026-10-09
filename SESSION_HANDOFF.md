# Session Handoff — Nexus Core / Coding_Agent

> **Devin takeover:** read `DEVIN_START_HERE.md` before this chronological handoff. It contains the current exact source/CI/artifact state and a do-not-regress checklist.

## v0.35.2 — Voice/text sync + off-PATH tool detection

- **Voice-gated text reveal** — the chat `result` event no longer dumps
  the full reply while voice segments still synthesize/play. With voice
  on (`responses`/`responses_activity`), the held buffer becomes the
  canonical result text and reveals per segment on the NexusVoice `play`
  event. `finish_task` now publishes `{"event":"sealed","task_id",
  "total":N}` (N = all enqueued jobs for the task); cancelled/muted queue
  drops publish `skipped`; per-segment `error` also counts terminal.
  Client release: `played+skipped+errored >= sealed total`, `stop`/
  `muted`, or a 12s activity-stall watchdog (reset by every voice event).
  `releaseVoiceHold` posts `result.content` on release so the end state
  matches the non-voice path (HUD removed).
- **Dead-air fix** — `voice_global.js` `stop` bus handling now resets
  `_lastEnd` to 0: a force-stopped predecessor no longer imposes the 2s
  inter-activity gap on the next response's first segment.
- **Off-PATH detection** — manifests support `detect.executable_dirs`
  (env-var expanded, `{install_root}` placeholder). `resolve_executable`
  probes them after PATH, before the install_root scan — so winget
  installs like `Program Files\Tesseract-OCR` detect *and* invoke.
  pip-method installs additionally probe the managed interpreter's
  `Scripts`/`bin` dir (`extra_executable_dirs` on PluginManifest) —
  `piper.exe` inside ComfyUI's embedded Python is found. Live-verified:
  tesseract resolves to `C:\Program Files\Tesseract-OCR\tesseract.exe`,
  piper to the embedded Scripts dir.
- Tests: `test_executable_dirs_detect_off_path_install`,
  `test_executable_dirs_expand_install_root_and_env`,
  `test_pip_install_probes_managed_python_scripts`,
  `test_finish_publishes_sealed_total`,
  `test_finish_publishes_sealed_zero_for_unspoken`,
  `test_cancelled_job_publishes_skipped`.

Checkpoint: **2926 tests** (2926 passed + 3 env skips).

## Follow-up — Mission/task approval reconciliation

Found by live mission dogfood on `D:\Nexus_Core`: a mission work node
parked when its inner agent task hit a verify-gate (`write_file` landed,
task requested shell approval). Resolving the gate through the task lane
(`/api/tasks/decide`) resumed the task — but the mission stayed
`waiting_approval` forever, and a manual mission-level approval would
have re-run the whole node.

- `server.py` — the node executor now inspects the parked task row when a
  mission `approval_granted` stamp lands: `waiting_approval` →
  `agent.resume` (existing path); terminal status → the task's outcome is
  **adopted** as the node result (`_mission_node_out` derives `ok` from
  the ledger status, so failures propagate); in-flight
  (`running`/`planning`/`verifying`/`reviewing`) → bounded 300s wait for
  the chat-side resume to land, then adopt/resume accordingly.
  `interrupted` is adopted as a failure (dead in this process).
- `supervisor.py` — new `task_resolver` hook (wired to `self.tasks` in
  `AppState`); `_reconcile_approvals` consults `TaskRecord.
  approval_resolutions` on each tick: a recorded chat-lane decision is
  adopted into the mission approval row (`resolve_approval`), which
  stamps `approval_granted` and dispatches the node — the executor then
  adopts the already-finished task. A task row still `waiting_approval`
  leaves the mission parked.
- Tests: `test_chat_side_approval_releases_parked_mission`,
  `test_chat_side_denial_replans_parked_mission`,
  `test_still_pending_task_gate_stays_parked`.

Checkpoint: **2926 tests** (2926 passed + 3 env skips).

## Follow-up — Actions-artifact dogfood + deploy script

Commits `6b2cd91a` + `38b4e813`; deployed `D:\Nexus_Core` at 0.35.2.

- **Run-artifact parser** — `_GH_RUN_ART_RE` now accepts a trailing
  `in/on/of repo owner/name` clause into `params.repo`; suffix cleanup
  only strips a standalone `artifact` word, so `dogfood-artifact`
  survives. Previously a repo-qualified request fell through to the
  model lane entirely.
- **Dedupe reattach** — a verified-existing GitHub download now
  re-resolves its registry record by filename so the reply carries the
  card again (was: honest text, empty `artifacts`).
- **Deploy script** — `scripts/deploy_local.ps1` mirrors dist → install
  with bare-name `/XD` (`data output runtime models tools .git` +
  `config.json` file exclusion). Verified by controlled test: bare names
  protect subtrees from `/MIR` purge; absolute paths do NOT (that bug
  deleted `runtime/voice/chatterbox`, `models/voice/chatterbox`, and all
  of `tools/` in the previous deploy — all re-provisioned).
- **Live dogfood** — `download the artifact from the latest build in
  repo afterburn25/nexus-dogfood` resolved run `37858760704`, downloaded
  `dogfood-artifact.zip` via the durable manager, extracted
  `dogfood-artifact.txt` beside it, registered both with
  `run_artifact`/`run_artifact_extracted` provenance, and the dedupe
  re-request reattached the verified card.
- CI green on `e3ae8826`, `5669f420`, `6b2cd91a`, `38b4e813`.
- Tests: `test_run_artifact_download_preserves_repo_clause`,
  `test_run_artifact_name_ending_in_artifact_kept`,
  `test_run_artifact_bare_request_stays_unnamed`,
  `test_deduped_download_reattaches_artifact_card`.

## v0.35.1 — Artifact actions, artifacts panel, cancellable uploads

Commits `6f8a4d7e` + `448e6327`; deployed `D:\Nexus_Core` at 0.35.1.

- **Card actions** — `Open` / `Show in folder` / `Copy link` on local
  artifact cards. `POST /api/artifacts/<id>/{open,reveal}` resolve by id
  only; `open` re-verifies SHA-256 (fresh) before shell-associating so a
  tampered binary never executes via the route.
- **`GET /api/artifacts` fix** — was returning raw rows with absolute
  `path`; now projects `client_view`. `verify()` gained a
  `(mtime_ns, size)`-keyed cache for cheap list views; `/download` and
  `/open` pass `fresh=True` so trust-critical paths always rehash.
- **Artifacts tab** — new utility panel lists the registry newest-first
  via the same card renderer; refreshes on open + 15s while visible.
- **Uploads as jobs** — `github_upload_release_asset` is async when a
  JobManager is wired: `status:"uploading"` + `job_id`, byte progress in
  the activity timeline, completion/failure `notification` events, remote
  provenance + `retention: release` on success. Cancel via
  `github_cancel_upload` tool, "cancel the upload" intent (needs only
  `github.read` — aborting a side-effect never pauses for write), or the
  generic `/api/jobs/cancel` route (`kind=="github_upload"` aborts the
  stream between blocks via module-level `_UPLOAD_FLAGS`).
- Path-sourced uploads register the file upfront so the reply carries a
  card immediately; remote links attach on verified completion.
- Lane status mapping: `started`/`cancelled` are success statuses —
  in-flight upload responses must not mark the task failed.
- LIVE DOGFOOD (deployed 0.35.1): chat → `github.write` approval →
  job card; 128MB upload cancelled mid-flight via `/api/jobs/cancel`
  (8.9% progress, GitHub confirmed zero partial asset); 3MB upload
  completed → job `completed/finished` + release URL, artifact card
  verified with remote `download_url` and `retention: release`.
- DEPLOY GOTCHA: `robocopy /MIR` deleted `runtime/voice/chatterbox` +
  `models/voice/chatterbox` (not bundled in dist) — re-provisioned via
  `chatterbox_runtime.ensure_runtime` / `chatterbox_assets.ensure_model`
  into the deploy dirs. Consider excluding `runtime`/`models` from
  future /MIR deploys (or `/XD` them alongside `data`/`output`).

Checkpoint: **2911 tests** (2911 passed + 3 env skips).

## v0.35.0 — Artifact Handoff & GitHub File Delivery

Nexus hands produced files to the user directly in chat as verified
download cards, and publishes/retrieves them through GitHub. Commit
`4f18301`; deployed `D:\Nexus_Core` at 0.35.0.

- `localcodeagent/artifacts.py` — `client_view()` (safe projection, no
  absolute paths; `download_url`, `remote`, `retention`, `pinned`),
  `attach_remote()` (https-only, key-allowlisted remote copies),
  `find()` (name/kind/id/latest resolution), `pin()`,
  `RETENTION_CLASSES` (temporary/cached/user/release/pinned — publish
  promotes to `release`).
- `GET /api/artifacts/<id>` + `/api/artifacts/<id>/download` —
  re-verifies SHA-256 before streaming (409 tamper / 404 missing),
  `Content-Disposition` attachment, `X-Content-SHA256`, artifact-id
  only (never a client path).
- Auto-recognition — `fs_archive` and `package_release` register; the
  response payload carries `artifacts[]` (JSON `artifact_id` fields and
  `artifact_id=art-…` text markers both extracted in
  `orchestrator._artifact_ids_from_events` / `server._agent_artifacts`);
  durable `UserDownloadManager.on_done` → `_download_artifact_done`
  registers completed downloads (with `extract_zip` for Actions
  artifacts — traversal-safe member check).
- Cards — `renderArtifactCards` in `web/app.js` (+`styles.css`):
  compact card (icon/name/size/verified), expandable details (id,
  sha256, mime, timestamps, task), actions Download / View on GitHub /
  From GitHub; `_safeLink` allows `/api/` or `https:` only; per-host
  id-dedupe; deleted file → "Local copy unavailable" while GitHub
  links persist.
- Durable history — `record_exchange(..., artifact_ids=…)` persists ids
  on the assistant message; `renderConversationHistory` refetches
  `/api/artifacts/<id>` so cards rehydrate with fresh verification.
- GitHub tools (`localcodeagent/tools/github.py`) —
  `github_list_releases`, `github_get_release`, `github_create_release`,
  `github_list_release_assets`, `github_upload_release_asset`
  (streaming `_ProgressReader` upload, duplicate refusal unless
  `replace=true`, read-back verify, remote provenance, ActionLedger
  journal), `github_download_release_asset` (durable manager with
  `Accept: octet-stream` + Bearer header — stripped on cross-host
  redirect), `github_delete_release_asset` (`confirm=true` required),
  `github_list_run_artifacts` / `github_download_run_artifact`
  (latest-successful run, expired artifacts refused honestly).
  `repo` arg wins over the workspace remote.
- Action lane (`action_ops.py`) — `artifact_show` ("give me the
  file/zip/installer", pronouns → latest), `github_upload`,
  `github_download`, `github_file` intents incl. explicit
  `repo owner/name` + `release vN` parsing.
- Permissions — uploads/deletes `github.write` (pause → approve resumes
  the exact op; deny uploads nothing — verified live), fetches
  `github.read`.
- Mission gating — `MissionEvaluator` accepts an `ArtifactManager`
  (`sup.evaluator.artifacts = self.artifacts`); `artifact_verified`
  criterion requires a registry hit that passes hash verification.
- Dogfood (live, deployed 0.35.0): chat → approval → zip → card →
  `/download` hash match; "give me the zip" re-attaches card;
  GitHub connect via vault; chat-driven upload to
  `afterburn25/nexus-dogfood` release v0.0.1 verified with GitHub-side
  digest match; download-back via durable manager registered with
  `release_asset` provenance; 48MB asset cancel at ~42MB → `.part` →
  resume → hash match → `.part` cleaned; denial produced no upload.
  Note: `nexus-dogfood` is a private test repo left on the account
  (token lacks `delete_repo` scope) — delete via GitHub settings if
  unwanted. GitHub token lives in the Nexus vault (user's own gh
  credential, connected for dogfood).

## v0.33.0 — Interactive Chat Permissions

Permission requests render inline in chat as a backend-authoritative
decision card — no Tasks-panel detour, no typing `yes`/`continue`:

- `localcodeagent/approvals.py` (new) — decision enum
  (`session`/`deny`/`always`/`once`), `stamp_pending` (durable approval
  ids), `approval_card` (friendly title, action summary, sanitized args,
  legal decisions, settings link), `decision_options` (hard gates: policy
  `deny` → no options, `AUTONOMY_NEVER_AUTO`/creator-gated → never
  `always`, locked creator session → card renders disabled).
- Inline card (`web/app.js` + `web/styles.css`) — `Authorization
  required` with action text, `Filesystem · Write files`-style friendly
  title, Advanced Details disclosure (raw key/tool/args/scope), decision
  dropdown that resolves on selection. `approval_resolved` /
  `approval_timeout` / task-cancel bus events repaint cards in place;
  `GET /api/approvals` restores pending (live) + resolved (historical)
  cards on reload — no model turn.
- `POST /api/tasks/decide` — validates pending state under
  `_APPROVAL_LOCK` (one resolution per approval id, ever: double clicks,
  two windows, replays, restart+replay → `409 stale`), applies grants via
  `PermissionManager` (session → in-memory only; always → `set_level` +
  `_update_config_file`, identical to `/api/permissions/level`), stamps
  `approval_resolutions[]` on the durable task row, publishes
  `approval_resolved`, then `agent.resume` continues the exact suspended
  step. Deny → no execution, truthful `Nothing was changed.`
- `PermissionManager.effective()` honors `_session_grants` for
  `ask`-level permissions (chat grant suppresses reprompt); autonomous
  auto-grants now tracked in `_auto_grants` so toggling autonomy off
  revokes only what it granted. Session grants never reach config.
- All four park sites (local action, tool, verification, direct image)
  stamp + emit `card`; `_bus_emit` enriches bus payloads and fires one
  voice notice (`I need your permission to continue.`) per approval.
  Cancel/stop paths stamp `cancelled` resolutions; autonomous timeout
  stamps `expired`. Legacy `/api/tasks/resume` stamps the same resolution
  rows → Tasks panel and chat cards stay in lockstep.
- Tests: `tests/test_approvals.py` — 27 tests covering the decision
  matrix, card payloads, persistence, stale/dup rejection, cancellation,
  and e2e park→decide→exact-once resume. Live Windows dogfood on a
  sandboxed backend verified the full loop including restart restore.

**DEPLOYED** — v0.33.0 is live on the workstation install
(`backend-pre-0.33.0` preserves the 0.32.0 backend). Verified live:
`/api/approvals` on the deployed backend immediately surfaced a
pre-deploy `waiting_approval` task as a restored pending card
(`Filesystem · Write files`, full decision set) — durable restart
restore working on real state. Note: a fresh gated chat turn needs a
runnable coding model; at deploy time readiness reported 1.7 GB free
VRAM (models idle-unloaded), so the model lane returned the standard
readiness 409 — environment state, not a permissions defect. The full
park→decide→resume matrix was dogfooded on the sandboxed source build.

Checkpoint: **2749 tests** passing (2 environment skips).

## v0.33.1 — Startup voice latency fix + audit completion

Report: *"the after load voice took a really long time to play after
startup."* Root cause found in `data/logs/backend-host.log`: four
**orphaned `llama-server.exe` processes** from pre-boot sessions were
pinning ~10 GB of the 12 GB GPU. The Chatterbox worker's
`min_free_vram_mb` gate (3200) saw <2 GB free → loaded on CPU → the
first post-load TTS stalled past the 240 s timeout → fell back to
kokoro → voice played ~4.5 min late **in the wrong voice**.

Fixes:

- `RuntimeManager.sweep_orphan_runtimes()` — boot-time reclaim (async
  thread, spawned from `AppState.__init__`). Enumerates llama processes
  whose executable resolves to this install's managed runtime; healthy
  orphans serving a configured profile's checkpoint are adopted via the
  existing `_adopt_healthy_orphan` path, all others killed. Foreign
  llama installs untouched.
- `voice/chatterbox.py` — CPU-device synthesis capped at 60 s
  (`CPU_SYNTH_TIMEOUT_S`) so the kokoro fallback isn't held for the full
  GPU-sized timeout when VRAM-starved.
- Approval audit taxonomy completed: `persistent_grant`,
  `approval_once`, `approval_expired`, `approval_cancelled`, and
  `approval_requested` at non-tool park sites.

Checkpoint: **2756 tests** passing (2 environment skips).

## v0.32.0 — Conversation Intelligence phase 1: response scope

New milestone (relevance/scope/concision/multi-turn). Observed defect:
`How old are you?` → *"Let me think. I hear you. Counting from
September 30th, 2026 — my birthday — I'm 7 days old."* — three causes,
fixed as general architecture:

- `localcodeagent/context/scope.py` (new) — `classify_scope()` →
  `ResponseDepth` (exact/brief/explanatory/detailed/open_ended),
  requested-vs-supporting slots, reasoning visibility, size budgets;
  `scope_directive()` injected per-turn beside the intent advisory;
  `scope_metrics()` measures responses (filler, narration, sentences,
  trailing question, over-budget). `AgentOrchestrator.turn_scope(cid)`
  exposes the per-turn plan for the debug inspector.
- `identity.py` slot discipline — `_AGE_VARIANTS`/`_BIRTHDAY_VARIANTS`
  reveal only the requested fact; parentage/denial lanes no longer leak
  the birth date; creator trimmed to name + one clause; new
  `_AGE_HOW_QUESTION` lane ("how did you calculate your age") reveals
  the derivation as requested content; informal `r u`/`are ya` forms.
- `SemanticResponse.bare` — exact deterministic answers (identity,
  clock, answer-memory) suppress the genome envelope (micro/opening/
  closing/address); body still re-realizes via MeaningFrame.
- `conversation_memory.py` — `use port 8080`/`set X to Y` imperative
  value-sets now store slotted facts; `actually make that 8090`
  retargets the newest fact and supersedes in-slot; `my:`-slot facts
  require distinguishing-noun overlap (favorite-food no longer leaks
  into favorite-color prompts). Both caught by new scope scenarios.
- `qa/conversation.py` — `generate_scope_scenarios()` + asserts
  (`max_sentences`, `no_leading_filler`, `no_reasoning_narration`,
  `no_trailing_question`, `scope`) + per-turn metrics.
- `tests/test_response_scope.py` — permanent regression for the
  observed defect + disclosure ladders + classifier matrix.
- Regression fixed en route: hesitation-gated `thinking` vocalizations
  strip again (v0.31.8 word-keep side-effect).

Increments 2–3 (same milestone, found by extending the QA corpus —
every fix below is a defect the new scenarios actually caught):

- `identity.py` existence lane — `do you have a father/mother/family`
  answers yes/no only; the name stays on the next disclosure rung.
  `_SUBJECT` resolves informal `u`/`ya` so `when were u born` hits the
  deterministic lane.
- Compound self-corrections — `i like red. actually no, blue. never
  mind, make it green` decomposes into clauses; only the settled value
  stays active (was: whole chain stored verbatim). Stacked markers
  (`wait, actually make that monday`) chain through the reset path.
- Retarget referent discipline — scans for the newest predicate-
  bearing fact (entity facts like `i have an rtx 3080` can't absorb
  corrections), retires slotless referents directly, and value classes
  (colors) pick the same-class preference fact — `actually i prefer
  green now` retargets favorite color, never favorite food.
- Bare-value corrections — `actually no, blue` / `actually i prefer
  green now` retarget via marker+value; `_BARE_CORRECTION_STOPWORDS`
  keeps `actually, sure` from rewriting facts; ambiguous restatements
  bank `i prefer black` rather than corrupting an unrelated fact.
- Modifier guard — `make that bigger` is referent-relative, not a
  value; `_RESET_MODIFIERS` blocks comparatives from landing in value
  slots (was rewriting `color is green` → `color is bigger` and
  swallowing the command via the training ack).
- `never mind X` no longer banks as a `Never mind…` rule; `use the
  dark theme` (non-literal value) can't retarget facts; weekday/month
  and settings (`light`/`dark`/`metric`) single-word values satisfy
  the `is`-fact guard.
- Unbound anaphora → `env.ambiguity` — `make that bigger`,
  `what's his name`, `change it` with no referent surface the
  unresolved-referent advisory + scoped clarify rule (previously
  write-only metadata; content-bearing `it works`/`that's fine`
  stays unflagged). Resolved pronouns suppress the flag.
- `intent_context` added to the utility lane's `optional_blocks` —
  envelope advisories + scope directives previously never reached
  lightweight turns, where most ordinary questions land
  (pre-existing gap found via the QA coverage).
- Training ack echoes the landed value — `Got it — port is 8090.`
  instead of narrating storage internals.
- QA: `memory_contains`/`memory_not_contains` asserts evaluate the
  durable store's injected block for the query (transcript-clean);
  `aggregate_metrics()` reports filler/narration/trailing-question/
  over-budget rates, depth histogram, builtin-vs-model ratio,
  median/p90 latency — the harness-side benchmark surface.
- Corpus: ambiguity-fresh, pronoun-followup, correction-chain,
  stacked-marker, cross-form, repeat-question, informal exact-facts,
  and a deterministic ~64-turn long-conversation scenario (teach →
  24 distractors → mid-session corrections → settled-value recall →
  24 more distractors → cross-act callback).
- tests: `TestUnresolvedAnaphora` (envelope + clarify rule),
  `TestCorrectionChains` (9 adversarial memory cases),
  `test_scope_metrics_aggregate` (rate ceilings as regression gate).

Increment 4 — live dogfood on a sandboxed source backend (real
llama.cpp + real persona/memory at `D:\Devin\nexus_dogfood`, port
58580) surfaced and fixed:

- Fact restatement fell through to a rambling model essay —
  `learned["restated"]` now acks `Already noted — X.`; re-teaching a
  superseded value re-lands fresh (inactive-twin dedupe fix).
- `where ... you are` matched the UI-nav regex → "Chat is under
  Chat." Person-directed wheres now fall through.
- Incidental feature-alias hits anywhere in a compound sentence
  (`let's talk ... what's the weather` → 'talk' → Chat explain) —
  aliases must live in the interrogative clause.
- `change it to the dark one` and `no, dont do that` landed referent/
  imperative phrases in fact value slots (`color is the dark one`,
  `color is dont do that`) — `_REFERENT_VALUE_RE` +
  `_IMPERATIVE_VALUE_RE` guards reject them.
- Self-knowledge + GitHub capability lanes are `bare` — no more
  `I hear you.`/`I do occasionally get things right.` on exact facts.
- Verified live: correction chains, forget + honest empty-state,
  informal identity, repeat-variation, clean recalls.
- `what do you know about me?` was answered with improvised persona
  lore — now a deterministic `memory:facts_recall` lane listing active
  user-taught facts in second person (`active_facts()` accessor,
  `_FACTS_RECALL_RE`, `bare=True`, honest empty state).
- Memory prompt block gained the attribution rule: first-person
  wording in stored facts belongs to the user — recalls now say
  "your favorite color", not "my favorite color".
- `topic_shift`/`topic_return` advisories reach the model prompt
  (last unsurfaced envelope marker), and forward shifts decay
  reference resolution — `new topic — change it` flags the unbound
  referent instead of re-binding to the abandoned topic's entity.

Known live limits (model lane, not regressions): the 4B adds persona
flourish beyond BRIEF budgets, attributes user facts as its own on
recall, and invents weather instead of declining — the scope
directive helps but a small model's discipline is soft.

**DEPLOYED** — v0.32.0 is live on the workstation install
(`D:\Nexus_Core`, swap following the `backend-pre-*` convention;
pre-swap state preserved at `backend-pre-0.32.0`). Verified live:
`how old are you` → *"8 days old — still brand new."* (bare, exact),
birthday/derivation rungs, scope inspector reporting
`exact, requested=(age), supporting=(birthday), bare=true`.

Since then: scope inspector endpoint (`GET /api/conversations/scope`)
with a populated slot model, topic-shift advisories + referent decay,
and a deterministic single-fact recall lane — `whats my favorite
color` / `what port are we using` answer straight from memory with
correct second-person attribution, no model call. Pronoun-anchored
queries match leniently; bare "the X" questions require full content-
term cover so general-knowledge questions can't hijack a stored fact.
QA corpus grew to cover facts-recall, restatements, topic shifts,
person-directed wheres, and recall discipline.

Checkpoint: **2756 tests** passing (2 environment skips).

Remaining milestone work (next increments): broader corpus toward the
acceptance matrix, real-model benchmark numbers on the deployed
build, inspector UI surface, then release/docs/deploy.

## v0.31.10 — low-mid "barrel" carve + boom-aware gate

User still heard barrel after the tonal retune → fine-band diff of 29
cached live clips vs the golden showed a systematic **+6.7 dB median at
100–200 Hz** (+2.3 at 450–650) — low-mid boom the brightness retune
never carved. Preset now has `150 Hz @ −7 dB q1.2`, `520 Hz @ −2.5 dB
q1.2`, `highpass_hz 60 → 95` (verified live A/B same-text: +9.5 → +2.0
dB median at 100–200; fundamental region preserved). Gate extended:
`dsp.band_share_db` scores 100–200 Hz share per draw, rejects >−16 dB
(golden ≈ −21) so bright-but-boomy draws redraw too. Deploy pending;
live preset already patched via user-data shadow.

## v0.31.7–v0.31.9 — stochastic-draw gate + vocalization/gesture repair

User report "responses still sound like in a barrel" → measured the
**actual cached chat renders** (not fresh test clips): short replies
landed dark + reverberant draws (0.86 s @ 2690 Hz / echo 0.68 vs golden
4391 / 0.30) while long renders stayed clean — a Chatterbox sampling
lottery (temperature 0.72), worst on short text.

- **v0.31.7** — `_synthesize` scores every Chatterbox draw post-DSP
  (`dsp.spectral_centroid_hz` + `dsp.echo_lag_corr`, same math as the
  eval script), rejects draws <3.4 kHz centroid or >0.55 echo, redraws
  up to `voice_chatterbox_quality_retries` (default 2, 0 disables),
  keeps best-of. Rejects publish `quality_redraw`. Param grid showed
  no exaggeration/temperature fix — gating was the right lever.
- **v0.31.8** — "skips over oh" root-caused: the keep-policy DELETED
  dropped word tokens from speech text while they stayed in display.
  Word tokens now keep surface text; stage wrappers still strip.
  Spaced laughs ("ha ha"/"hee hee") detected → `[laugh]`/`[chuckle]`;
  cry/sob + `*coughs*` detection; `*sobs*` overlap-corruption fix;
  tag map grew `whisper`→`[whispering]`, `cry`→`[crying]` (runtime-
  verified supported). Gate skips tag-carrying segments (a real
  `[laugh]` legitimately measures 1591 Hz / echo 0.91).
- **v0.31.9** — the real production gap: the speech filter unwrapped
  `*stage*` italics BEFORE detection, so `*laughs*`→"laughs"→spoken
  literally. `_sanitize_prose` keeps the wrapper for known `_STAGE`
  phrases; ordinary italics still unwrap.
- Live-verified on deployed 0.31.9 (:53109): `[laugh]`/`[whispering]`/
  `[crying]` produce real sounds e2e; speech cache purged of pre-gate
  sub-band clips (centroid<3.4 kHz or echo>0.55).
- Checkpoint 2660. CI on `7b6d9a7f`/`9f268048`/`a7b5bd5` in flight.
  (superseded by v0.31.10 → checkpoint 2662)

## Desktop host re-deployed at 0.31.6 (was 0.31.1)

The deployed `NexusCore.exe` reported FileVersion **0.31.1** (built from
`50e45d29`) — it predated `fc041550` ("Splash WebView2 profile must not
live under the state junction"), so the live binary could still show the
static dead-splash instead of the animated fault surface + recovery
options when the backend failed.

- Rebuilt via `dotnet publish` (same flags as `packaging/build_windows.ps1`),
  deployed `NexusCore.exe` (0.31.6+666e5c24) + refreshed `splash/` assets.
- Verified live: `webview2-splash` profile now created under
  `%LOCALAPPDATA%\NexusCore\` (not through the appDir data junction).
- Post-swap backend on :51171 — version 0.31.6, Chatterbox CUDA warm,
  startup welcome already rendered through V7 (rtf 0.309).
- No functional `.cs` changes in v0.31.5/0.31.6 — the narrator-salt fix
  is backend-side; only csproj version stamps were behind.

## v0.31.5–v0.31.6 — barrel report root-caused: stale narrator cache + tonal (not reverberant) gap

**User report "still sounds like a barrel"** split into two independent
carriers, both fixed and verified live:

1. **StartupNarrator salt omitted preset content** (`9c07b4a0`,
   v0.31.5). Sig was `engine|version|DSP_VERSION|preset.id` — the id is
   stable across retunes, so pre-V7 clips (Oct 6 renders: corr 0.985,
   −17 LUFS, stereo-width decorrelation = the barrel recipe) replayed
   indefinitely under the same salt. Sig now embeds
   `AudioCache.preset_hash(preset.to_json())`; any preset retune
   re-salts automatically (verified: sig changed f45572b614b3 →
   a26cff3b2d54 → 049d58ea89aa across retunes). Regression test added.

2. **Tonal gap, not echo** (v0.31.6, `e09d9754`). Isolation ladder
   proved the hollowness is upstream of DSP: raw Chatterbox generation
   already measures echo proxy 0.47 (V7 DSP *reduces* it to 0.28;
   removing every layer delay changes nothing). The actual divergence
   from the approved golden is spectral: golden centroid 4391 Hz /
   air 0.129 vs shipped preset 3340 / 0.031. Retune exciter 0.44→0.55,
   7.2 kHz +3.2→+5.0, +9.5 kHz air band +5.0 q0.8. Live verification
   on the deployed install: **centroid 4398 Hz (+7 vs golden)**,
   air 0.125, echo 0.16, −13.0 LUFS, −1.0 dBFS, corr 1.000.

- All 10 narrator lines (welcome/initializing/online/shutdown/fault/
  recovery-v2/iris/power/online/failed) re-rendered through live V7 and
  written under the new salted names AND legacy emergency-fallback
  names (fault/recovery lines keep working backend-down).
- A/B audition ladder preserved at `%TEMP%\isabella-ab\` (raw, V7,
  delay-removals, brightness variants b1–b5, excited-line variants) —
  b3 ≈ shipped retune, b5 is the over-bright reference point.
- Reference-window experiment (closed): a tight 7.5 s VAD-cleaned window
  conditioned raw echo 0.40 vs full-ref 0.47 — marginally drier but
  *darker* (2202 vs 2433 Hz raw; post-DSP 3912 vs live 4398). Not a win;
  the retuned DSP already lands +7 Hz of golden. The dry
  `reference-source.mp3` stays the locked conditioning input — a
  replacement would need to be brighter, not just cleaner.
- Renders preserved: `raw-trim7.5.wav` / `v7-trim7.5.wav` in
  `%TEMP%\isabella-ab\` alongside the rest of the ladder.

## v0.31.4 — Isabella V7 live-verified on the deployed install

`v0.31.4` tagged `33e84162`, backend rebuilt from repo root and deployed
to `D:\Nexus_Core` (previous at `backend-pre-0.31.4`). Live config flipped
to `voice_engine: chatterbox` + `voice_preset_id: nexus-isabella-chatterbox`.

**Verified live (execution evidence, not config claims):**

- `/api/status`: `0.31.4`, safe_mode False.
- `/api/voice/status`: engine `chatterbox 0.1.7/turbo-749d1c1`,
  worker alive, `device: cuda`, preset `nexus-isabella-chatterbox`.
- Conditioning cache generated in deployed bundle:
  `conds-eec47eff41960369-*.pt` — prefix matches sha256 of the dry
  `reference-source.mp3`. No `reference.wav` in the bundle.
- 6 live `/api/voice/speak` segments measured with
  `scripts/eval_isabella_v7.py --wav`: LUFS −13.0…−13.3 (target −12.5),
  peak −1.0 dBFS, clip 0.0000, corr 1.000, side/mid 0.0000, echo proxy
  0.25–0.41, all pass vs golden (`docs/reference/audio/isabella-v7-
  approved-golden-2s.mp3`); golden delta +1.7…+2.0 LU louder as approved.
- Warm synthesis RTF 0.33–0.43 (aggregate 0.293); cold first-call ~14 s
  incl. worker spawn + model load (8.77 s) + conditioning.
- VRAM ~3.2 GB during synthesis (GPU total 825 MiB idle → 3999 MiB busy);
  worker is a detached process that survives backend restarts.
- Structured-speech filter verified live: code fence + list items were
  skipped ("I've included the code in the response." / "The details are
  listed below."); only prose spoken.
- Repo pipeline eval (5 scripts, CUDA): all pass, rtf 0.624 aggregate,
  vram ~1994 MB worker-reported.

**Not yet verified:** audible playback + text-release sync in the UI is
client-side (`web/voice_global.js` play-events); the segment/URL contract
is verified over HTTP but listening confirmation is the user's.

**Fixed this session:** `_warm_on_first_speech`/`_warm_engine` warmed the
config-default engine instead of the active preset's (a Chatterbox preset
pre-warmed Kokoro). Generated `conds-*.pt` caches are now gitignored and
were scrubbed from the frozen bundle (they regenerate on first prepare).

**Caveat:** during deploy testing the supervisor respawned the backend a
few times (instance replacement on relaunch + dynamic-port churn while
probing). No WER crash records; final instance (PID 12776) stable.
Keep an eye on whether backend respawns recur under normal use.

## v0.31.2 — autonomy/self-repair liveness + v0.31.1 evidence & reliability convergence

v0.31.2 (see CHANGELOG `[0.31.2]`) patches the v0.31.1 base — which itself patched v0.31.0 — see CHANGELOG `[0.31.1]`.

Nexus is an execution agent, not a narrator. The observed failure —
"can you create a folder d:\Nexus" answered "Yes — Workspaces is
ready" with nothing created — is fixed at the root and covered by a
permanent regression.

Architecture (no parallel systems — all reuse exists):

- `localcodeagent/action_ops.py` — deterministic lane. Bounded grammar
  (mkdir/write/delete/move/rename/copy) → `ActionPlan`; outside the
  grammar returns None → model lane. Missing targets clarify, never
  guess. `execute_plan()` runs capability → permission → execute →
  verify → evidence → truthful reply. Outside-workspace paths the user
  explicitly named are allowed ONLY via approval, always.
- `localcodeagent/action_ledger.py` — durable evidence store
  (`data/action_ledger.json`). `begin()/finish()` per action; statuses
  recorded/awaiting_approval/denied/failed/verified/unverified/
  clarify/unavailable. `run_filesystem` + `verify_filesystem` are the
  shared execution/verification contracts (mkdir⇒is_dir, write⇒exists,
  copy⇒dest+size parity, move⇒dst exists src gone, delete⇒absent).
- `localcodeagent/tools/filesystem.py` — `fs_mkdir`, `fs_delete`,
  `fs_move`, `fs_copy` registered (workspace-bounded, `.agent`
  protected, mutation-tracked); `write_file` verifies and returns
  `WROTE_OK`. All mutating handlers verify on disk.
- `agent/orchestrator.py` — `_local_action_reply()` lane in `run()`
  after self-knowledge; parks `local_action` pending-approvals on the
  task; `_resume_local_action` handles approve/deny. Missing direct
  tool → falls through to model (truth gate still guards).
- `self_knowledge/service.py` — action-shaped capability phrasings
  bail to the action lane; capability-domain verbs keep honest
  live-state answers.
- `server.py` — `self.action_ledger` wired into the orchestrator;
  `tools.context["extra_roots"]` exposes registered workspace roots.

Also fixed this session (same release):

- **Git repo-state lane** — `_git_state_reply()` answers "what
  branches/remotes/repo" from real `git` output. It was the source of
  a persona fabrication ("I don't have a public repo" with a live
  GitHub remote in `.git/config`).
- **Stale-clarification hijack** — a parked image clarification
  survived task retirement; "yes do this" resolved it as
  image_generation. Parked clarifications are TTL-gated
  (`active.pending()`), cleared on topic-shift in `record_turn`, and
  `clarification_response` with no `continuation_of` no longer
  defaults to image.
- **Truth-gate ledger** — unverified action claims now write a
  `claim`/`unverified` ActionLedger entry.
- **Mission evidence** — every mission node outcome writes a
  `mission_node` ledger entry; `ActionLedger.mission_rollup(mid)`
  powers `GET /api/missions/<id>/evidence`.
- **Requirement-change propagation** — `learn_from_user` surfaces
  `superseded` facts → `requirement_change_cb` →
  `MissionStore.flag_requirement_change` marks in-flight nodes
  `stale_requirement`; the mission executor prepends a dead-value
  warning to the node's instruction. Terminal nodes untouched.
- **Desktop shortcut** — repointed `Nexus Core.lnk` to the live
  install at `D:\Nexus_Core` (it targeted deleted `D:\Nexus_Core_Fresh`).

Convergence increments landed on top (`main` @ `282f0878`):

- **Stale-node replan** — `_step_executing` replans around
  `stale_requirement` nodes instead of dispatching dead-requirement
  work (`76f3a5de`).
- **Ledger orphan reconciliation** — `recover_orphans()` closes
  crash-interrupted `recorded` entries as `unverified` at boot
  (`ae5bc72e`).
- **Mission UI** — stale-requirement badge on DAG nodes + evidence
  rollup section consuming `/api/missions/<id>/evidence` (`5ff96620`).
- **Ambiguity/envelope surfacing** — `env.ambiguity` and sibling
  markers (temporal/comparison/conditional/alternative/compound)
  reach the model as prompt advisories and render as UI hint chips
  (`da6ea8d7`, `f67f60ee`).
- **Answer Memory invalidation** — superseded facts retire learned
  answers carrying the dead value so memory can't serve it with a
  trusted badge (`928f4a87`).
- **Compound local actions** — fully-parsed clause sequences execute
  through the same verified lane; stops at first gate/failure;
  approval resume continues the parked tail, denial cancels it;
  unparseable clause → whole turn to model (`b02118e5`, `b48c7d7c`).
- **Replan artifact recheck** — a failed `internal:artifact_exists`
  criterion now produces a recovery path that names the missing
  target, demands real file-write output, and re-checks the artifact
  before re-verify — declared criteria are never retired unverified
  (bounded-soak finding: missions timed out in a prose-instead-of-
  write loop; `282f0878`).

Post-release hardening — shipped as **v0.31.2** (`595eb955`, tag
pushed, all CI green) and **deployed to `D:\Nexus_Core` 2026-10-07**
(backend-only swap; old backend at `backend-pre-0.31.2`; live verified:
`/api/status` reports 0.31.2 + `safe_mode` field, autonomy running).
Models moved to **`D:\Nexus_Models`** (77 GB) with a junction at
`D:\Nexus_Core\models` — model files are user state, never inside the
deploy payload; if rolling back, recreate the junction. The deploy had
silently stranded all models (missing since 0.31.1) — the soak's
"No enabled model remains" errors exposed it.

- **POSIX Windows-path gate** — `action_ops._resolve` treats `D:\` /
  UNC paths as absolute on any host so outside-root approvals can't be
  bypassed by running on Linux (`50e45d29`).
- **Splash WebView2 profile** — splash UDF moved off the app-dir
  `data\` junction to LocalAppData (+ temp fallback); the animated
  fault surface can no longer die with a broken state redirect
  (`fc041550`). Root cause of a deploy-created junction pointing at
  `D:\C:\...` (MSYS path mangling) — junctions must be recreated with
  `mklink /J "<link>" "<target>"` via cmd, never bash paths.
- **Credential-learning guard** — `learn_from_user` refuses
  credential-shaped learning writes (passcode/password/API key/PIN
  families) before any store/Brain/prompt path; forget/revoke and
  ordinary credential *questions* still work (`97211f08`, `45d94d5c`).
- **Self-repair staleness chain** — three layers: mission step
  retires when its incident is terminal (`98a101bc`); open incidents
  untouched >3d abandon at coordinator tick (`0c270e3c`); the
  retirement sweep runs pre-gate every tick so stopped autonomy can't
  strand a stale mission 'active' (`583f26ab`). Verified live in the
  soak: 4 stale repair missions cancelled at first tick.
- **Safe Mode surfacing** — `/api/status` reports `safe_mode`; the
  main topbar shows a persistent badge (was Command-Center-only — the
  live install sat in safe mode ~2 days unnoticed, `795dbbd3`).
- **Soak posture** — `mission_soak.py` exits safe mode and starts
  autonomy after every backend (re)start; persisted control flags can
  no longer silently park all missions (`4f980702`).
- **Model recovery hooks** — the model-crash playbook's
  `restart_model`/`fallback_model` steps had no runtime hook and
  blocked missions with "no handler" (0311d soak finding).
  `restart_model` → `RuntimeManager.restart_unhealthy_managed()`
  (stops dead/unhealthy managed servers; they re-serve on demand);
  `fallback_model` is a truthful marker — per-request model selection
  already excludes the failed candidate. Hook-less actions still
  block honestly (`776b076f`).
- **Stale blocked-mission retirement** — `blocked` is non-terminal so
  missions nobody unblocks accumulated forever (23 observed on the
  live install, most ~2.4d old, parked on failed dependencies). A
  pre-gate sweep (`_retire_stale_blocked`, runs even while autonomy
  is stopped) transitions blocked missions untouched >3d to `failed`
  — terminal, auditable, resumable via failed→ready. User-paused
  missions are never swept (`ec452458`).

**v0.31.3 shipped + deployed 2026-10-07** (`d5e2ab91`, tag pushed):
both fixes above. Backend rebuilt via PyInstaller → swapped into
`D:\Nexus_Core` (old backend at `backend-pre-0.31.3`). Live verified:
`/api/status` → 0.31.3 + `safe_mode` field, autonomy running, dynamic
port 57063. Soak 0311d final report (12 cycles): 4 kills → 5 clean
recoveries, 5 completions, 0 failed, 0 blocked, 10 verified artifacts
(soak_1,3–11.txt); the 7 timeouts all wrote their target file but hit
the 900s budget in approval/verify churn on a 4B utility model —
serialization, not stalls.

Rules for the next session:

- NEVER claim an action succeeded without a `verified` ledger entry or
  tool `*_OK` result. A plan, a permission grant, and a queued op are
  not evidence.
- Extend the bounded grammar carefully — every new verb needs tests
  for: executes+verifies, denied, parked, failed, clarify-vs-guess.
- The lane must never intercept mission turns (mission_id) or genuine
  capability questions.

## v0.30.0 — conversation-memory lifecycle (§24-26 continual QA)

The durable-memory loop is now complete end-to-end through the
production `run()` path — teach, recall, correct, supersede, revoke,
forget — with Nexus Brain kept in lockstep.

Key surfaces (`localcodeagent/workflow/conversation_memory.py`):

- Corrections — `_correct_fact_value(old, new)` rewrites the fact
  carrying the rejected value only when it uniquely identifies one;
  "X not Y" accepts pronoun *and* named subjects ("actually the port
  was 5433 not 8080"); `correction:`/`i meant`/`to clarify` bodies
  re-enter the canonicalization pipeline (`corr_body` fallback for
  uncanonicalizable text).
- Rule revocation — `_revoke_rules(action)` retires rules whose
  normalized stems cover the revoked action ("stop responding in
  JSON"); prohibition-shaped rules are never retired, and unmatched
  revocations store as `Never` rules. Locked topics refuse via the
  shared `locked_refusal` lane.
- Forget variants — `forget()` probes both the raw and
  filler-stripped query ("the", "my", "about", "fact"); prefix
  patterns cover `forget about`, `nevermind`, `stop remembering`,
  `delete the fact about`.
- `is`-facts — declarative `is|are` captures gated on value signal
  (digit/uppercase/path/multi-word); `_fact_slot` gained `is|are` so
  "the port is 8080" supersedes "the port is 5433".
- `_fact_subject_ok` compares casefolded — subject case preservation
  must not reopen the question-word hole.

Brain parity (`localcodeagent/workflow/nexus_brain.py`,
`agent/orchestrator.py`):

- `prompt_context` filters `active` — superseded/forgotten records no
  longer inject beside their replacements.
- `run()` calls `_sync_nexus_brain()` whenever `learned` is non-empty
  (the method was dead code); sync now covers all four lanes including
  `sync_conversations` (autobiographical).

Regression coverage: `test_corrections_supersede_unique_fact`,
`test_forget_variants_retire_the_referent`,
`test_rule_revocation_retires_mandates_not_prohibitions`,
`test_definite_is_facts_capture_with_value_gate`,
`test_subject_named_corrections_and_locked_guards`,
`test_memory_lifecycle_end_to_end` (QA runner, 14-turn lifecycle),
`test_prompt_context_respects_inactive_records` (brain).

## v0.29.0 — performance/resource convergence (§15-23)

The perf/resource backlog items are now wired end-to-end on top of the
already-landed machinery (demand eviction, idle unload, crash
signatures, BenchmarkLab/BaselineStore):

- `localcodeagent/workers/leaks.py` — `WorkerLeakTracker`: per-worker
  RAM/VRAM before/peak/after + residual ledger; `leak_suspects()` flags
  repeat-leakers. Owned by `RuntimeManager` (`self.leaks`), consumed by
  managed ComfyUI/InvokeAI via a `leak_tracker` kwarg.
- `localcodeagent/perftrace.py` — `PerfTrace`: bounded lanes
  (startup/chat/voice/gpu) fed by `EventBus.observe`; residency events
  mark the gpu lane at `_residency_activity`. `/api/perf/timeline`.
- `localcodeagent/storage_audit.py` — `audit_storage` classifies JSON
  persistence (sqlite_candidate/watch/bounded_ok);
  `/api/storage/audit`.
- `BaselineStore.check(metric, value, direction="higher"|"lower")` —
  throughput drops now flag; `BenchmarkLab` attaches the check to each
  run before recording the sample.
- `TriggerStore._tree_mtime` — recursive bounded dir watch (5000 files,
  depth 8, noise-dir skip) so nested project edits fire `file_changed`.
- Tuner sweep adds a `q4_0/q4_0` KV candidate next to `q8_0/q8_0`.

Already landed, do not duplicate: interactive-lane QoS yield in the
supervisor, `release_managed_models_for_vram`, crash signatures +
bounded restarts, idle/demand eviction, KV q8 candidate, SQLite-backed
heavy stores (answer_memory, rag, hippocampus, knowledge).

## 2026-10-07 — v0.28.1: action-turn reliability patch

Post-release dogfood exposed and fixed a real broken path: action
requests stalled into prose narration with zero tool calls.

- **Root cause** (`ec053642`): the pruned ~46 KB tool-schema set ≈ 14 k
  tokens overflowed the 8B model's 12288-token window; llama.cpp
  rejected the request and the provider's overflow recovery dropped
  tools entirely → the model physically could not emit a tool call.
  `_session_schemas` now budgets the advertised list at ~45 % of the
  prompt window and collapses to a minimal write/read/patch/run/search
  set (with `find_tools`) when it would not fit; `_trim_context`
  subtracts the serialized schema size from the message budget.
- **Action nudge** (`5e242840`, `70ddc3a4`): imperative action asks that
  produce zero tool calls get one re-prompt; the retry sends
  `tool_choice="required"` (llama.cpp honors it — verified directly),
  degrading to `auto` once on a 400 from older servers. Questions and
  fabricated-claim replies are never nudged.
- **Verification-denial loop** (`f4b258eb`): denying a verification
  command recorded PERMISSION_DENIED → auto-repair counted it as a
  failure → same approval re-pended forever. Skipped checks excluded
  from the failure predicate.
- **Windows 8.3 paths** (`de981db5`): containment checks compared a
  resolved child against an unresolved workspace root — broke every
  document/media/data/knowledge/codeintel file access on `RUNNER~1`
  paths (the scheduled soak's failure). Roots resolve before
  comparison now; affected tests normalize tempdir roots.
- Live dogfood on `D:\Nexus_Core`: `create a file named
  nexus_probe_v28.txt` → action_nudge → `write_file` call → approval
  gate → file on disk → verification gate → denial → clean finalize.

## 2026-10-07 — v0.28.2: soak-hardening patch

Shipped on top of 0.28.1 so the release tag matches the deployed build
(`2955b307`, tag `v0.28.2`, deployed + verified on `D:\Nexus_Core`).

- **Concurrent env probes** (`dde03ee7`): `EnvironmentStore.detect`
  ran up to 6 serial subprocess probes at 5 s timeout each (~30 s
  worst case) and blew the soak selftest's 15 s HTTP client timeout on
  loaded Windows runners. Probes now run on a 4-worker pool.
- **Approval-create ordering race** (`bc7db5a1`): worker threads
  transitioned the mission to `waiting_approval` before writing the
  approval row; a concurrent `_reconcile_approvals` in that window saw
  a missing row and replanned the gate away (flaky `blocked` instead
  of `waiting_approval`). Row now writes before the transition,
  matching the recovery path's existing order.
- Dispatched soak run `37630507840` green; push CI green on
  `bc7db5a1` and `2955b307`.

Post-tag on `main` (rides next patch):

- **HTTPError socket closes** (`b0ef2500`): `urllib.error.HTTPError`
  IS the response object — every `except HTTPError` site that didn't
  close it leaked the connection socket until GC (InvokeAI/ComfyUI
  polling, model providers, MCP, GitHub API, downloads + test helpers).
  All handlers now `exc.close()` under try/finally; suite is
  ResourceWarning-clean on the touched paths. Deployed to
  `D:\Nexus_Core` — note the deploy-swap caveat below.
- **Deploy caveat**: `InvokeAI`'s python (`tools\InvokeAI\Scripts\…`,
  `invokeai-web`) side-loads `backend\_internal` VC runtime DLLs and
  holds them — stop it along with llama-server/host before replacing
  `backend\`, else Remove-Item fails mid-copy. It respawns on demand.

## 2026-10-07 — v0.28.0: performance audit pass 2 (measure → fix → verify)

A full audit-and-fix sweep over startup, hot paths, residency, and
per-call prompt cost (commits `8040dcb0`…`8d8d228d`):

- **Startup overlap** (`8040dcb0`, `dbfc78f9`): backend spawn moved
  ahead of the splash WebView2 wait (~2.4 s serial stall removed);
  `nexus-core-ready` now posts after first paint instead of after
  `loadStatus`/`loadReadiness`/`loadConversationMemory` (~1.8 s).
  Genuine interface readiness **~7.3 s → ~4.1–5.1 s**. The ~12 s tail
  to `main_shown` (~17.2 s) is authored cinematic pacing — 7 s minimum
  display + sequence finish + 3 s ONLINE dwell + voice gate — truth
  gates, not wasted work; left intact by design.
- **Boot trace**: dedicated `data/logs/boot-trace.log` records every
  CLR→main-window mark (file-share contention with the backend stdout
  writer was silently eating marks in `backend-host.log`). Prespawn
  orphan guard added for faults before `PrepareAsync` consumes the
  backend task.
- **Chatterbox conditioning disk cache** (`43808b18`): per-voice
  `Conditionals` persisted keyed on (ref-audio hash, exaggeration,
  norm_loudness, dtype, revision). Warm prepare after worker reload
  **~1.05 s → ~0.02 s**, live-dogfooded on the installed build.
- **`/api/status` slimming** (`50faf88f`): **375 KB → 33 KB**. Full
  research sessions, image jobs, and per-task `research.plan` dumps no
  longer ride the status poll; detail lives in `/api/task-log` et al.
  No consumer read the dropped keys.
- **Incremental repository index** (`e0c04e04`): unchanged files
  (path+size+mtime) keep parsed symbols/preview; only new/changed
  files re-parse. Add/change/delete sanity-verified.
- **Tool-schema pruning** (`8d8d228d`): 174 callable schemas ≈ 76 KB
  used to ship to every non-utility model call. Now coding-core
  categories are always advertised; media/github/research/documents
  categories join on intent or prompt keywords → **~41 %** schema cut
  for coding tasks (102 tools ≈ 46 KB). `find_tools` always present as
  the discovery escape hatch; execution stays name-based so pruned
  tools still run if called.
- **Audits confirmed landed**: idle CPU <1 %, event-driven backend
  exit detection, SSE (not polled) status refresh, deterministic slash
  commands never touch the model, rAF token batching, role context
  hints + idle shrink, KV q8_0 tuner variants, HOT→WARM→COLD model
  lifecycle, `vram_releasers` GPU brokerage, image backend idle
  unload, `_trim_context` governor, per-turn atomic JSON, symbol-level
  repo search, `EvalLab`/`ExperimentStore`/`RegressionStore`/
  `BaselineStore` QA harness.

## 2026-10-07 (late) — roadmap remainder landed: 12 more items + dogfood sweep

Finished every open roadmap-closure item post-0.27.0 (commits
`ce7d1110`…`70f539f4`, CI green on head):

- **LKG rollback floor** (`ce7d1110`): `latest.txt`+`rollback.flag`
  selection now enforced by `min_version.txt` floor (ratcheted on
  clean-exit session marker) + `_internal/VERSION` coherence check —
  rollback can no longer regress below the installed version.
- **System page panels** (`6942973`): Environment editor
  (`/api/environment*`) + Secrets editor (`/api/secrets`,
  metadata-only) — the APIs existed with zero UI.
- **External file-change watcher**: `POST /api/files/changed`
  mtime_ns scan + workspace poll; editor banner (Reload/Compare),
  tab markers, self-write immunity via write-response mtime.
- **DB migration awareness** (`tools/migrations.py`): file-based
  detect for Alembic/Django/Prisma/EF/Flyway, per-system status
  parsing, `migrations_apply` gated on `shell.execute` approval.
- **Docker-aware dev** (`docker_tool.py`): compose detection,
  ps/logs/stop/start/rm, dev-server-in-container discovery.
- **Unified Quality UI** (`bc59fc7b`+): System page Quality panel +
  `lint_run`/`profile_run` adapters + `GET /api/quality` +
  `POST /api/quality/{test,coverage,lint,profile}`.
- **Provisioning completion**: Playwright as declared plan item,
  license-gating on fleet+image models, `approve_item`, `replan()`
  for live profile changes, UI Approve button.
- **Scaffolds 7→15**: flask_service, django_app, vue_vite,
  dotnet_console, dotnet_webapi, go_cli, rust_cli, python_lib —
  render-verified, emitted Python compiles.
- **Deferred locked-file replacement**: `LkgStore.stage_swap` +
  `data/lkg/swaps/<id>` staging + generic swap applier in the
  desktop host (`Program.cs`).
- **Git→PR→CI closure loop** (`7b03f77a`): github tools emit
  deduped `ci`/`pull_request` bus events → triggers map to
  ci_failed/ci_completed/pull_request_updated signals; live dogfood
  captured a real `ci failure` event.
- **refine_details** (`ab1cda3f`): real low-denoise post-upscale
  pass (own `refine_denoise_strength` knob — inheriting the
  advisor-filled `denoise_strength=1.0` would regenerate, not
  refine).
- **Browser E2E** (`b7744ef7`): per-page Playwright/Edge sweep
  (18 pages) + bounded PIL pixel-diff visual baselines; skips
  cleanly without Playwright.
- **Voice clone import** (`0e2a3253`): `import_voice` WAV/sample
  registration via `prepare_reference()` canonicalization —
  zero-shot reference, not training; UI card on Speech Lab.
- **Silent uninstall fix** (`5c1bdeeb`): `InitializeUninstall`
  called the modal `AskUninstallScope` unconditionally →
  `/VERYSILENT` hung forever on an invisible dialog. Found by live
  clean-install dogfood of the real `NexusCore-Setup-0.27.0`
  artifact (install→serve→uninstall verified).
- **Policy fail-closed** (`a6ac3ccb`): `custom` egress treated as
  `restricted` until per-action rules exist.
- **Selftest deflake** (`70f539f4`): isolated smoke retries
  endpoints once + names failed checks in the error.
- **Stale-doc sweep** (`a6ac3ccb`): test checkpoint 2466
  (`project.json`, `test_version.py`, README, DEVIN_START_HERE,
  PROJECT_STATUS) + autonomy/self-repair counts + roadmap-closure
  rows rewritten to audited state.
- **MCP live dogfood**: real `@modelcontextprotocol/server-filesystem`
  over stdio — 14 tools imported, real calls green, boundary
  enforcement confirmed.
- **Bounded mission soak** (`D:\nexus-dogfood`, port 8902):
  kill-mid-mission → backend restart → mission resume → approvals
  re-granted — persistence + recovery verified. Missions timeout
  honestly when the utility model answers prose instead of emitting
  file-write tool calls (model-capability gap, not an executor bug).

Open: multi-hour mixed soak (harness exists, bounded run done),
full live fault-injection matrix (kill/restart leg verified;
network-cut/Defender/disk legs untested live).

## 2026-10-07 — v0.27.0 shipped: performance/residency milestone released

Performance/residency + startup/status work (18 commits since v0.26.1)
released as **v0.27.0** — `VERSION` bumped, `scripts/sync_version.py`
regenerated all derived artifacts (pyproject, installer iss, csproj,
backend_version.txt, `.agent/project.json`), README/DEVIN_START_HERE/
PROJECT_STATUS updated, tag `v0.27.0`, GitHub Release live.
Final live numbers on `D:\Nexus_Core`: total startup **~7.0 s**
(warm), `/api/status` **~20–50 ms** steady-state (SWR probe cache),
`backend_health` ~1.56 s, warm chat ~1.1–1.4 s, idle 3 processes /
~4.5 GB VRAM. Full detail in the 0.27.0 `CHANGELOG.md` entry.

## 2026-10-07 — Performance/residency milestone + startup profiling (v0.26.1, `9abc9de`, CI green)

**Full story:** `docs/PERFORMANCE_OPTIMIZATION.md` (root causes, diffs,
before/after numbers) + `docs/PERFORMANCE_BASELINE.md` (method +
baseline). Repeatable measurement: `scripts/benchmark.py`.

**Residency fixes** (`44c0de30` + `bb2b3660`, CI run 37565669510 green):
Chatterbox bf16 (~2.29 GB resident, was ~3.3 fp32), 120 s GPU idle
unload that actually fires (`touch=` separates real work from status
polls), warm only on cache-miss speech enqueue, `vram_releasers`
reverse reclaim (LLM launch frees idle image/voice before evicting
residents), cached hardware telemetry (no per-message nvidia-smi),
`invokeai_auto_start` off, `CREATE_NO_WINDOW` on all spawns.

**Startup fixes** (`9abc9de`, CI run 37569421150 green): `[nexus-init]`
step timers pinned a 9.4 s boot gap to `ProvisioningManager._inventory`
running synchronously in `AppState.__init__` — torch import probe
(~5 s), full-registry manifest refresh per tool item, sha256 over
3.3 GB of voice weights + 400 MB of Kokoro assets. Fixed via
`runtime_status(deep=False)` shallow boot check, scoped
`refresh_install_status(name)`, and verified-content caches
(`.nexus-model-verified.json` / `.nexus-assets-verified.json` —
size+mtime+sha256 signatures skip rehashing unchanged files).
**provisioning init 8.35 s → 65 ms; total startup ~21.4 s → ~12.7 s.**

**Soak:** 50 turns flat at ~6.1 GB backend RAM (~1.2 s/turn). Image
round-trip verified end-to-end (InvokeAI on-demand start → PNG →
VRAM released, llama resident throughout).

**Handy diagnostics:** `backend-host.log` now carries `[nexus-init]
<step> <ms>` lines per boot stage + `inventory/<id> <ms>` for any
item probe over 100 ms.

## 2026-10-07 — v0.26.1 shipped: Chatterbox Turbo live on the deployed install

**Shipped.** PR #8 merged → `c77718d0`, CI run 37552146081 fully green
(tests 3m23s + windows-desktop 9m11s Inno installer + install/update
smoke), tagged `v0.26.0`, GitHub Release live. **Deploy dogfooding
caught two real bugs the source tree hides**, fixed in `554e551a`
(v0.26.1, pushed, CI pending at write time):

1. **Worker script missing in frozen builds.** `.py` files compile into
   the PYZ — `chatterbox_worker.py` never landed on disk, so the spawned
   runtime died instantly ("worker exited unexpectedly"). Now shipped
   via explicit `--add-data`; `_spawn` fails loudly if absent.
2. **Kokoro fallback passed a foreign voice id.** `isabella` isn't a
   Kokoro voice — degraded renders failed a second time. Fallback now
   maps to `bf_isabella` (the approved reference's own Kokoro source)
   or the first available voice.

**Deployed + verified live** at `D:\Nexus_Core` (old backend parked at
`backend-pre-v0260`, old exe at `NexusCore.exe.pre-v0260`):
`/api/status` → 0.26.1-equivalent backend, provisioning ran the real
`ensure_runtime` path end-to-end (pbs sha256 → extract → pip torch
2.6.0+cu124 + chatterbox-tts 0.1.7 → import probe → marker), model
copied in and sha256-verified. `/api/voice/preview` synthesized
"[chuckle] Chatterbox is live on the deployed install." — 3.45 s on
CUDA, RTF 0.35, 3.1 GB VRAM, 19 tags confirmed on the live tokenizer.
Kokoro stays loaded as fallback.

## 2026-10-06 — v0.26.0 (branch): Chatterbox Turbo voice engine integrated

**State.** `feature/chatterbox-voice-engine` holds 4 commits
(`556e0816` engine+runtime, `ea99b948` integration, `fc5c1423`
provisioning+UI+tests, `d01879da` release 0.26.0). **Merged same
evening — see the v0.26.1 entry above for the shipped state + the two
frozen-build fixes the dogfood caught.** Full suite: **2417 passed**
locally; voice suite 104/104.

**Architecture.** Second registered `TTSEngine`. `ChatterboxEngine`
(`voice/chatterbox.py`) spawns `voice/chatterbox_worker.py` inside the
isolated runtime `runtime/voice/chatterbox` (CPython 3.12 + torch
2.6.0+CU124 + chatterbox-tts 0.1.7, ~4.5 GB) — JSONL over stdin/stdout,
stderr isolated, defensive non-JSON line skip (the lib *does* print
noise to stdout). Backend stays Python 3.14 and torch-free. GPU policy:
`auto` → CUDA only with ≥3.2 GB free VRAM (`voice_chatterbox_*
config`). Live-verified: 8.5s cold load, 2.8 GB VRAM, RTF 0.30–0.35 on
the 3080 Ti.

**Voice.** Canonical reference = approved
`isabella-nexus-v6-enhanced-synthetic.mp3` (sha256
`7a70916…` verified against the manifest), packaged at
`voice/chatterbox_voices/isabella/` + official preset
`nexus-isabella-chatterbox`. User voices in
`models/voice/chatterbox/voices/` merge + shadow official. `reference.py`
validates refs (>5s, clip/silence/format). Turbo conds are global —
worker re-prepares on voice switch.

**Loudness.** Root cause of quiet output: turbo reference conditioning
≈ −27 LUFS; the chain only peak-limited. New `voice/loudness.py` (pure
NumPy BS.1770, within 0.5 LU of pyloudnorm) normalizes to −17 LUFS
between output gain and the −1 dBFS limiter. Opt-in per preset;
config `voice_normalize_loudness`/`voice_target_lufs`/
`voice_limiter_enabled` apply as preset overrides so `dsp.process`
keeps its signature. Measured: −26.8 → −17.0 LUFS, peaks ≤0.89.

**Emotion/gestures.** `ChatterboxVocalizationAdapter` maps existing
planner styles → native turbo tags; tokenizer probe reports dedicated-
token tags (all 19 known tags confirmed live). Voice Lab in Voice
Studio auditions only confirmed tags + has a cold-start probe button.
Adapter follows the active preset's engine (`_sync_adapter`).

**Safety.** Chatterbox failure → Kokoro fallback in `_synthesize`,
cached under a *separate* engine key. StartupNarrator cache salted with
backend-published `data/voice/startup/engine.json` sig (fault/recovery
keep legacy fallback). `HF_HUB_OFFLINE=1` in the worker env.

**Provisioning.** Two verified items: `chatterbox-runtime` (pinned
python-build-standalone CPython 3.12.15, sha256) + `chatterbox-model`
(9 files, sha256, idempotent). `chatterbox_voices` ships via
`--add-data` in `packaging/build_windows.ps1`.

**Samples.** `samples/chatterbox-isabella/` (gitignored): 5 required
phrases + 10 emotion/gesture auditions, all through the real
manager→DSP→cache path. Whisper-verified: no literal tag leakage.

**Gotchas.** stdlib `wave` can't read float WAVs — worker saves PCM_S.
`sys.path` script-dir shadowing breaks stdlib `types` — worker scrubs
it + spawned with `-P`. `exaggeration`/`cfg`/`min_p` are ignored by
turbo (`emotion_adv=False`) — emotion control is tags + temperature.

## 2026-10-06 — v0.25.3: voice trailing-syllable fix, deployed + verified

**Shipped.** `93d28414` → pushed, tagged `v0.25.3`, GitHub Release live,
CI run 37543247808 fully green (tests 3m52s + windows-desktop 9m2s Inno
installer + install/update smoke). Deployed to `D:\Nexus_Core`: old
backend parked at `backend-pre-v0253`, `/api/status` → 0.25.3, voice
preview through the running backend confirms trimmed durations
("All done." 1.28→0.79s, "Yes." 1.02→0.55s, "Sure thing." 0.98→0.79s).

**What it fixes.** Kokoro leaves a breathy noise bed decaying 150–500ms
after the real last phoneme (audible as trailing hiss/mumbled syllables),
plus dead air that made `end_slack` bail before any tail logic ran.
`trim_tail_artifact` now shaves >140ms sub-(-22 dB) residue to a 90ms
decay first, then runs detached-phoneme logic on the trimmed audio.
Verified with Whisper transcription on a 15-phrase live-synthesis
battery — no words eaten. **Deliberately not fixed:** syllable-length
detached islands (a rare second artifact class) — quiet real final words
are DSP-indistinguishable; an earlier attempt ate "now"/"for you".
`dsp.DSP_VERSION` added to the audio cache key so stale pre-fix
segments invalidate instead of replaying old artifacts.

**Earlier in the session:** v0.25.1 (`ce6f434`/`849403d`) chat streaming
layout — `.message` grid adjuncts were auto-placing into the 42px avatar
track ("scrunched" replies + mangled controls on research/action
responses); v0.25.2 (`f189296`) double-speak on unterminated streamed
replies — `finish_task` counted emissions before `flush()` so the
`final_text` fallback re-enqueued the whole reply.

## 2026-10-06 — v0.24.0 redeployed: installed app now runs shipped build

**Redeployed.** The first 0.24.0 deploy ran a pre-dashboard build
(`aa2ce19`) with `web/learning.*` patched into `_internal/web` manually.
Rebuilt `dist/ChatNexus` from `main` at `23eaf719` and swapped
`D:\Nexus_Core\backend` again: old parked at `backend-pre-aa2ce19`,
new copied, `NexusCore.exe` relaunched. **Verified live on the
install:** `/api/status` → 0.24.0, `/learning.html` → 200, and a real
`/study` session through the full queue exercised `study.active_session`
(the field the manual patch couldn't ship); `/study stop` drained the
queue cleanly. Backend listens on a dynamic port (62584 this boot);
`data/logs/backend.pid` identifies the live process.

**CI:** follow-up docs/handoff push `48494712` → `23eaf719`; run
37536776217 fully green (tests + windows-desktop Inno build again).

**Disk note:** `D:\Nexus_Core` holds ~12 GB of parked backend dirs from
earlier sessions (`backend-bloated-023` 9 GB, `backend-prev-023` 2.6 GB,
plus ~15 smaller `backend-pre-*/prev-*` dirs). Left in place — needs
owner decision. C: still ~1.5 GB free; user caches `.cache` (~24 GB) /
`.codex` (~34 GB) are the consumers; autonomy disk guard pauses <1 GB.

## 2026-10-06 — v0.24.0 converged: pushed, CI green, install deployed

**Final state.** `main` pushed (`579b2798` → `48494712`); CI run
37535259344 fully green: Linux unit tests 3m24s (incl. the previously
failing queued-drain case) and `windows-desktop` 9m32s — real Inno
installer build + fresh-install/update-preservation smoke (the
production-installer verification). Local suite 2377 passed / 2
skipped; Tier-3 soak green (isolated boot + 40 smoke checks).

## 2026-10-06 — v0.24.0 deployed to installed app (upgrade-install dogfood)

**Deployed.** `D:\Nexus_Core\backend` replaced with the fresh
`dist/ChatNexus` build of `main` at `aa2ce19` (Inno installer is
CI-only; local build produces the portable package). Sequence: manual
LKG snapshot (`snap-1791301469000`, manifest format preserved,
`latest.txt` updated) → processes stopped → old backend parked at
`backend-pre-v0240` → new backend copied → relaunched.
**Verified on the install:** `/api/status` → 0.24.0, `/api/learning`
live, `/learn` + a real chat turn work, `/learning.html` serves (the
dashboard page + `active_session` API field landed in commit
`8bfccca` *after* the build started — the three static files were
patched into `backend\_internal\web` manually; the API field ships in
the next build). Clean-install check: the build's own
`NexusCore.exe --self-test` passed on the packaged output.

**InvokeAI re-verified live** (6.14.2 at :9090): auto routing chose
InvokeAI + Juggernaut-XL → real PNG; pinned `invokeai:dreamshaper` →
real PNG; pinned comfyui (offline) → honest "not installed or running"
after fixing a real bug — pinned-engine requests were falsely deferring
behind the generic capability queue claiming "still being installed"
(commit `aa2ce19`). Full local suite: **2377 passed, 2 skipped**.

**Stale branches/PRs:** all 7 listed PRs are MERGED or CLOSED — nothing
open. All 15 `repair/ri-*` worktrees+branches deleted (merged into main);
`origin/feature/cinematic-core-unlock-splash` deleted on approval. Local
and remote now carry only `main`.

**Tier-3 soak green** (`localcodeagent.selftest --json`, 313s): all
unittest suites + isolated 0.24.0 boot + 40 smoke checks pass. Two real
findings fixed/handled: selftest crashed on CP1252 bytes in test output
(`errors="replace"` + None-safe concat, commit `3c550ae`); and ~15
autonomy tests "failed" because **C: hit 100% disk** — the real
`disk nearly full` budget guard correctly paused missions (correct
behavior, exhausted environment). Cleared 731 MB of pip cache →
1.5 GB free → all 105 autonomy tests pass isolated. **Note: C: remains
nearly full** — big consumers are user caches (`.cache` 24 GB,
`.codex` 34 GB); worth user attention.

## 2026-10-06 — Intelligence Governor + Continual Learning (v0.24.0)

**Milestone landed on `main`** (commits through `d1ea5273`, version bump
pending commit). Two subsystems, both reusing existing stores — no
second autonomy/evidence/training machinery.

- **IntelligenceGovernor** (`localcodeagent/governor/`): per-turn
  metacognitive assessment → costed cognitive-op ladder. Runs before
  lane selection so every task — including command/early-return turns —
  carries `intel` on the TaskRecord + an `intel` event. `/think
  fast|normal|deep|exhaustive` scales the compute budget.
- **Continual learning** (`localcodeagent/learning/`): promotion
  taxonomy → lesson extraction (structured, no CoT; command turns
  filtered by `response_source`) → Jaccard-clustering consolidation
  (provenance + contradictions preserved, never erased) → competency
  map (parent nodes don't absorb child attempts) → active-learning
  priorities → curriculum + study sessions that pull real research
  (`_study_research` forces web mode — bare topics classify
  `local_only` for task research, wrong for study) → closed-book
  mastery eval (fast-model answers, separate grading call, adaptive
  difficulty + spaced retention) → procedural memory → strategy
  demotion in the CognitiveScheduler → regression capture into
  RegressionStore → skill promotion / teacher-student / training-gate
  (no live weight changes).
- **Commands**: `/learn /weaknesses /consolidate /study [<topic>|status|go|stop|next|weaknesses] /mastery <topic> [report] /knowledge /procedures /training`
  plus NL access and `GET /api/learning`.
- **Live-verified**: study pulled 8 sources + 20 concepts + 10
  questions on "rust ownership"; `/mastery` ran a real closed-book eval
  (5/5, PASSED, difficulty 1 — correctly "not mastered" with n=1);
  consolidation clustered 5 lessons → 2 promoted → 1 procedure;
  "are you getting smarter?" answers from evaluated outcomes.
- **Focused suites green**; full suite run in flight at handoff —
  verify before shipping. **Not done:** dashboard HTML page (API only),
  retention is scheduled but live intervals take hours, teacher/student
  and model-growth gates are test-verified only, installer dogfood +
  InvokeAI re-verify still outstanding from the convergence list.

## 2026-10-06 — GitHub bare-target follow-up lane (commit `62f8a602` on main)

Live-dogfooded fix: after "GitHub's already connected — point me at a repo,"
a bare token like `afterburn25` fell through every lane into the model,
which narrated intent ("let me check if that's a repo…") while running
nothing — the unverified-claims badge correctly flagged it. New
`_github_target_reply` lane in `localcodeagent/agent/orchestrator.py`
detects a bare repo-ish token when the prior assistant turn was the repo
invite, executes `github_list_repos` through the normal registry
(read-only `github.read`, no approval needed), and answers with actual
data: exact-match pin, owner listing, or the real repo list. Tests in
`tests/test_speech_genome.py` cover exact repo, owner, owner/repo,
unknown target, and the in-flight-turn skip. Focused suite 46/46 green.

**Deployed.** `D:\Nexus_Core\backend` replaced with a fresh build of
`main` at `e6667bdd` (Oct 6 ~11:27) — carries the GitHub lane, the
Father-inversion guard, true voice replay (segment re-serve by
`voice_task_id`, `repeat_last` re-publish), broadened identity/origin
coverage incl. real-blend answers, utility-tier routing for casual
identity asks, and the startup-greeting prefetch (`?publish=0` +
`greetFetch` held on the transition gate). Prior backend parked at
`D:\Nexus_Core\backend-pre-20261006`; LKG snapshot refreshed
(`data/lkg/snap-1791304164704`, `latest.txt` updated).

## 2026-10-06 — Stabilization convergence merged + installed-app repair (v0.23.0)

**Convergence landed.** `milestone/nexus-stabilization-convergence` → PR #7 →
squash-merge `157b9b21` on `main`. CI green on the branch; the remote-main
unit-test failures (`KnowledgeToolTests` JSON errors) are fixed by this merge.
Full local suite: **2196 passed, 2 skipped**.

- **ChangeJournal** (`localcodeagent/changes.py`, new): durable bounded JSONL
  ledger for every meaningful mutation. Wired into filesystem writes (per-task
  upserts restored via checkpoints + verify-diff), SettingsRegistry writes
  (single `on_change` hook — chat/actions/inline all record once), git
  branch-create (undo = switch-back + branch -D), git commit (undo = soft reset
  refused if HEAD moved), git push (recorded honestly irreversible). Chat
  "undo that" falls back to the journal; dry-run probes never execute real
  undos; vague "turn it back on" phrasing stays out of the undo lane.
  Routes: `GET /api/changes`, `POST /api/changes/undo`. Tests:
  `tests/test_changes.py` (14).
- **Stale test fix**: `git_push` correctly gates on dedicated `git.push`
  permission (commit 3fb151cb) — test updated to match.
- **Docs**: `docs/CHAT_CONTROL_PLANE.md` documents the journal + undo lanes.

### Installed app (`D:\Nexus_Core`) — three real bugs fixed live

- **False ".NET required" error**: stray `hostfxr.dll` from an older
  self-contained deployment made the framework-dependent apphost search only
  `D:\Nexus_Core\shared\` (which doesn't exist) instead of the global 8.0.31
  runtime. Moved to `hostfxr.dll.stray` → app boots.
- **0.21↔0.23 host/backend mismatch**: an LKG rollback had silently restored
  the Oct-4 0.21.0 backend under the 0.23.0 host — broken voice APIs and the
  double-splash symptom. Restored the real 0.23.0 build from
  `backend-replaced`; old one kept at `backend-lkg-0.21.0`.
- **Double splash**: `nexus-core-splash.png` had drifted from the re-rendered
  startup clip — the warm-up surface and the video's first frame were two
  visibly different designs. Re-rendered the PNG from the clip's actual
  frame 0 in all 4 locations (install root + `splash/assets/` + both repo
  copies) and cleared the splash WebView2 cache (`data/webview2-splash`).
- **Version markers**: root `D:\Nexus_Core\VERSION` was stale at 0.21.0 →
  0.23.0. New LKG snapshot `snap-1791301722298` taken of the verified
  coherent 0.23.0 backend so any future rollback lands correctly.
- **Voice delay**: inherent to CPU-only onnxruntime in the frozen backend —
  uncached lines synthesize on CPU (first line after idle-unload slowest).
  GPU accel needs an onnxruntime-gpu bundle (~1–2 GB); deferred decision.
  The ~5s post-"online" silence is authored pacing (3s dwell + quiet buffer).
- **Remaining install debt**: ~12 stale `backend-*` dirs + ~280 leftover
  self-contained runtime files in `D:\Nexus_Core` — a clean production
  installer run should replace the whole layout. `D:\Nexus_Core\Source`
  checkout is corrupt (pre-existing).

### Repository hygiene

- PR #2 (draft `feature/cinematic-core-unlock-splash`) closed as superseded —
  its content landed properly via #4/#5/#7; branch kept by design (27 MB
  prototype assets).
- Deleted merged branches (local + remote): `codex/startup-milestone-captions`,
  `feature/startup-milestone-captions`, `fix/full-core-glow`,
  `milestone/integrated-reliability-closeout`, `voice-concept-isabella`,
  `milestone/nexus-stabilization-convergence` — all verified landed
  (note: repo uses squash merges, so `git cherry`/`rev-list` show
  non-equivalent SHAs; verify via mergeCommit parent + tree diff).
- Remote now: `main` + `feature/cinematic-core-unlock-splash` only.
- Scratch commit `61ee12b0` remains excluded as required.

### Still open

- Post-merge main CI + scheduled Tier-3 soak (next scheduled run).
- Clean-install / upgrade-install / production-installer dogfood against the
  cleaned deployment.
- onnxruntime-gpu voice bundle decision.
- Persona 20× variation matrix + page/button sweep on the packaged app.

## 2026-10-05 (late) — Integrated reliability closeout → 0.22.0

**Cycle closed.** `milestone/integrated-reliability-closeout` absorbed
`origin/main` (splash-tail handshake work preserved), finished the
Persona Speech Genome, and landed to `main` via PR #3. `main` is again
the single source of truth.

- **Splash convergence**: main's sequence-complete handshake +
  FINALIZING dwell + readiness convergence live on top of closeout's
  video splash (single WebView2 surface, hidden-until-real,
  `VerifyLoadableAsync` pre-flight, in-place error video on failure).
- **Persona milestone finished**: `nonverbal_rate` gates
  `VocalizationEngine` keep-probability (explicit 0.0 suppresses);
  `pause_hint` inserts one bounded clause-boundary ellipsis into speech
  text only; `seriousness`/`register` bound pitch/gain/pace;
  `emphasis_spans` documented honestly as span-protection metadata.
- **Soak**: 612 renders across 4 personas — 0 fact drift, 0 exact-span
  corruption, 0 serious-context humor leaks; 120-turn multi-turn
  session clean; 20× identity ask → 17 unique renders, canonical fact
  intact throughout.
- **Full suite**: 2103 tests, green (duckdb `data`-view fix landed; two
  environment flakes — loopback socket abort, smoke-instance port
  contention — confirmed transient on isolated rerun).
- **Dogfood**: frozen backend rebuilt via PyInstaller + desktop
  published + deployed to `D:\Nexus_Core`; real launch (video splash →
  verify gate → main window), real TTS WAV (24 kHz stereo), Speech Lab
  battery live, GitHub connected, graceful shutdown held for narration.
- **Branches**: `fix/full-core-glow` SUPERSEDED (work byte-identical in
  main); `voice-concept-isabella` SUPERSEDED_BY_PRODUCTION_VOICE_SYSTEM;
  `feature/cinematic-core-unlock-splash` SUPERSEDED_BY_PRODUCTION_SPLASH
  (27 MB prototype left on-branch by design; handoff doc lives in
  `docs/SPLASH_ANIMATION_HANDOFF.md`).
- Full matrix: `docs/ROADMAP_CLOSURE.md` → "Persona Speech Genome —
  completion matrix".

## 2026-10-05 — Persona Speech Genome (LANDED, milestone branch)

Branch: `milestone/integrated-reliability-closeout`. Scope: personas
stop being "one assistant wearing costumes" — each gets a structured,
versioned speech identity that drives deterministic surface
realization. Meaning stays upstream (IntentEnvelope / tools / facts);
the genome only controls HOW it is said.

### What landed

- `localcodeagent/personality/genome.py` — `SPEECH_GENOME_VERSION`
  schema: vocabulary/idiolect (per-family acknowledgement, success,
  error, disagreement, transition, interjection, signature-word
  pools), syntax shape (sentence length/variance, fragment + one-word
  rates, dash usage, list preference, answer-first, technical density,
  elaboration), cadence, opening/closing family weights, 12-category
  humor genome, disagreement/storytelling/question/repair styles,
  relationship + address POLICY (""/first/formal/literal-term —
  preset `address` is a policy token, not a name), language
  boundaries, vocal biases, micro-reaction pools + cooldowns,
  per-confidence phrase stems, repetition controls.
  `derive_genome()` layers neutral defaults → behavior-family genome →
  trait-slider nudges → explicit `speech_genome` overrides (deep-merge).
  `migrate_genome()` fills missing/corrupt fields, preserves unknown
  keys, never destroys persona data on upgrade.
- `localcodeagent/context/realize.py` — `SemanticResponse` extended
  (speech_act, confidence, exact_spans, conclusions/uncertainty/
  evidence, actions_failed, next_steps, register, semantic_id);
  `RenderContext` (mood, seriousness 0-3, register, relationship,
  social cue, sarcasm, user energy, address, creator);
  `SpeechDeliveryPlan` + `RenderedReply`; `classify_speech_act()`
  (intent + outcome + seriousness + social cue, canned lanes keep
  their own act); `PhraseCooldowns`; `PersonaRenderer.render_semantic()`
  — opening/closing families weighted per genome, confidence-stem
  hedging ONLY for non-verified facts, micro-reactions + address terms
  with per-category cooldowns, humor suppression in serious acts and
  non-allowed registers, repeat evolution (idx≥2 reframes, ≥3
  compresses to the load-bearing fact), `canonical=` lane for
  pre-composed authoritative bodies (verbatim pass-through + honest
  repeat acks).
- Orchestrator: `builtin_semantic()` expresses every canned lane
  (time/date/identity/greeting/capability/self-learning) as
  SemanticResponse + canonical text; `_builtin_reply()` renders through
  the active genome via `speech_context=` resolver. Canned replies are
  no longer suppressed under an active persona when the genome renders
  — deterministic in-character answers, no model call. Answer-Memory
  hits render canonically with repeat evolution keyed on asked
  text+answer. `AgentResult.delivery` carries the plan;
  `voice.finish_task(delivery=)` applies `pace` to enqueued speech.
- Store/API: `speech_genome` flows through `resolve_active`/`resolve`
  (customs inherit base preset genome or carry their own);
  `create_custom`/`patch_custom` accept genome overrides via
  `migrate_genome`; `available()` exposes the derived genome +
  `genome_summary`; `GET /api/profiles/<id>/personality/speech-preview`
  renders an 8-act battery through any resolvable persona (the Preview
  Lab backend). Fixed latent `_profile_get` query-arg bug that 500'd
  `/personality/effective`.
- `tests/test_speech_genome.py` — 34 tests: schema/migration,
  all-preset derivation, family distinctiveness, act classification,
  register+seriousness gating, address policies, uncertainty
  calibration, verbatim fact/identifier/identity invariants, delivery
  plans, cooldowns, repeat evolution, builtin+AM integration, and a
  ~200-render repetition soak.

### Pipeline

```
user meaning → IntentEnvelope/ActiveContext → SemanticResponse (WHAT)
→ speech act → relationship + mood → speech genome
→ variation/cooldowns → text realization → SpeechDeliveryPlan → voice
```

### Invariants (verified by tests)

- Facts, tool results, identity facts, numbers, identifiers pass
  through verbatim — persona colors the envelope only.
- Verified content never gets a random hedge; uncertainty gets the
  persona's own stem per level.
- Serious acts/registers suppress humor, micro-reactions, playful
  closings; address terms cool down instead of spamming.

### Verified

- `tests.test_speech_genome`: 34 pass. Persona suites: 184 pass.
- Full suite: 2091 tests — failures were all pre-existing or
  environmental (splash tests fail identically at e7d228fc — the
  splash sync work lives on main; two DuckDB data-tool tests fail at
  baseline; isolated-second-instance selftest is load-flaky, passes
  standalone). The queue-attribution tests broke on the context WIP's
  thread-local `lane_mission_id` move — updated to the new mechanism
  in 432c62ac.
- Live AppState dogfood: `_speech_context` resolves sassy genome
  (sarcasm/wit/teasing/deadpan categories), builtin lanes render
  in-character with per-persona pace, repeat asks get honest framing,
  persona switch changes surface, speech-preview battery returns
  8 acts × renders + plans, `finish_task` applies delivery pace.
- Live HTTP dogfood (real `python -m localcodeagent` server):
  `POST /api/profiles` → `set_active preset:nerdy` →
  `GET .../speech-preview?register=coding` returns the nerdy genome
  summary + per-act renders with delivery plans;
  `POST /api/chat "what time is it"` → "Acknowledged. The current
  local time is …" (nerdy ack pool + verbatim fact, model skipped);
  the same question again → "Still the case — …" honest repeat
  framing. `POST /api/chat` returns `builtin-local` model attribution.

### Still open

- Web UI landed: Personality Studio **Speech Lab** panel — persona/
  register/seriousness/turns selectors render the 8-act battery via
  `/personality/speech-preview` with per-line delivery plan details,
  plus a genome-summary chip row. `create_custom`/`patch_custom` POST
  actions accept `speech_genome` overrides.
- Voice: `pace` → job speed; `energy`/`warmth`/`emphasis_level` map to
  bounded per-utterance pitch/gain deltas (±1 st / ±3 dB via
  `VoiceManager._delivery_preset`). emphasis_spans/pause_hint/
  nonverbal_rate/register remain documented hints until the TTS
  engine exposes controls.
- Long-session UI soak + real-voice dogfood on the installed app.


## Canonical product/UI identity

- Product name: **Nexus Core**.
- Repository/source of truth: `afterburn25/Coding_Agent`.
- Official mark: orbital cyan/blue/violet **CN** emblem at `web/assets/nexus-core-icon.png`.
- Canonical primary UI: chat-first center pane, slim left navigation, and right **Code Diff / Tasks / Terminal** utility rail.
- Do not replace this shell with unrelated dashboard/IDE concepts unless the user explicitly changes direction.
- UI details are documented in `docs/UI_DIRECTION.md`.

## 2026-10-05 — Universal context intelligence + non-repetitive persona (IN PROGRESS)

Scope: the intent/context overhaul — Nexus understands language by
meaning across images, GitHub, coding, repair, tools, and multi-turn
follow-ups, and stops replaying identical canned prose.

### Architecture

- `localcodeagent/context/intent.py` — `IntentEnvelope`, the structured
  per-turn understanding object (intent, action, subject, constraints,
  references, corrections, conditionals, alternatives, ordinals,
  comparisons, temporal markers, topic shifts, ambiguity, confidence).
  `understand_turn()` = classification + post-pass reference resolution.
  Deterministic fast paths; confidence drives routing, no model call on
  the fast lane. `to_trace()` emits developer-visible routing evidence
  (intent/confidence/references/route) — never chain-of-thought.
- `localcodeagent/context/active_context.py` — `ActiveContext` persisted
  on the conversation row (restart survival for free). Active vs
  retired entities, pending clarifications, `active_error` bound from
  the task ledger, TTL decay (active 6h / entities 72h), bounded
  recent-entity list.
- `localcodeagent/context/references.py` — typed-term + bare-pronoun
  resolution. Pronouns bind by ACTIVE DOMAIN with verb hints; two live
  antecedents → ambiguous (ask, never guess).
- `localcodeagent/context/realize.py` — `PersonaRenderer`,
  `SemanticResponse`, `ResponseLedger` (rolling fingerprints, opening/
  closing cooldowns, lexical-similarity near-duplicate guard),
  `repetition_score` metric. Variant banks for greetings, capabilities,
  self-learning, identity — facts stable, wording rotates.
- Routing precedence: newest explicit instruction → corrections →
  pending clarification → compound/conditional → explicit actions
  (image/github/tool) → contextual follow-ups → identity/utility →
  conversation. `env.suppresses_canned()` blocks builtin replies from
  hijacking action intents; `env.direct_image()` makes image intent
  authoritative.
- `identity.response_for` gained an action-request guard ("draw me a
  picture of your creator" is a task, not an identity question) plus
  fact-stable phrasing variants.
- `web/app.js` `builtinClientReply` gained an action/visual-request
  early-return so client canned replies can't swallow task turns.
- Answer-Memory replay: canonical answer passes through verbatim; when
  the SAME asked question replays the same answer, an honest
  repeat-acknowledgement wrapper varies (keyed on asked text + answer).

### Language coverage (deterministic)

- Image paraphrases: "generate an image of", "show me a picture of",
  "let me see", "can I see", "give me", "I want to see what X would
  look like", "visualize", "draw/paint/sketch", "generate an adult
  woman in a red dress" (person-subject rule). Object semantics veto
  ("show me the code/logs/diff", "create a website with a logo" → the
  first-named artifact wins).
- GitHub paraphrases: "check GitHub", "what did Devin just push",
  "anything new land", "see what's happening with the repo".
- Repair paraphrases bind `active_error` from the task ledger:
  "fix it", "sort that out", "get that working again", "repair what
  just broke", "it's still broken".
- Follow-ups/corrections preserve subject + attributes: "make her
  blonde", "full body", "no, red hair", "that ain't it", "the image is
  too close".
- Compound actions retain every clause ("check the repo, fix the
  failing test, run everything, and push it" → 4 retained intents).
  Conditionals preserved as structures ("if tests fail, fix them and
  rerun"; "use InvokeAI unless it fails, then try ComfyUI").
- Topic shifts ("now check github", "anyway…") + returns ("back to
  that angel image") resolve against live and retired entities.
- Whitelisted typo map (githib→github, pictue→picture, …) — arbitrary
  text, tokens, paths, hashes never touched.
- Ordinals ("the second one"), comparisons ("which model is faster"),
  temporal markers ("earlier", "before the update") annotate the
  envelope for downstream resolution.

### Tests

- `tests/test_context_intent.py` — 42 tests: paraphrase classes,
  veto semantics, follow-ups, pronouns/ambiguity, compounds,
  conditionals, topic moves, typos, implicit reports, restart
  serialization round-trip, TTL decay, bounded growth, renderer
  variation/fingerprints, identity fact stability, 300-turn
  bounded-memory run.
- Regression sweep: `test_answer_memory`, `test_identity`,
  `test_image`, `test_conversation_policy`, `test_router`,
  `test_regressions`, `test_fast_lane`, `test_workflow`,
  `test_conversation_growth` — all green after fixes (see below).

### Regressions found + fixed during integration

- `attach` prepared after the envelope → moved attachment prep earlier.
- "Which model generates images with?" misrouted to image →
  interrogative-lead veto added.
- "create a website with a logo" misrouted → first-named-artifact
  precedence.
- "edit image <path>" wrongly direct-routed → literal edit-image ops
  stay tool-routed.
- Answer-Memory repeat wrapper keyed on asked-text+answer so different
  questions sharing an answer aren't marked "same answer as before".
- Repetition ledger is shared across turns (intended); builtin variant
  banks carry the required fact phrases so semantic slots never drift.
- **Mission-lane pollution**: mission/self-repair subtask prompts flow
  through `agent.run()` and were folding into the user's interactive
  `ActiveContext` — a mid-dogfood "Work the scoped lane of this
  mission" turn classified as coding and retired the live image
  context. `update_active_context` now only folds `auto`/`ask`/`plan`
  turns. Verified live: mission subtasks no longer disturb the image
  subject.
- Follow-up fragment merge on the direct path: "make her hair red"
  originally queued the bare fragment. `_direct_image_result` now
  merges the preserved `active_image_subject` — live job prompt:
  `a photorealistic adult angel with black wings, hair red`.

### Live dogfood evidence (real install, port 5199)

- "draw a photorealistic adult angel with black wings" →
  `direct_image_route`, job queued, no identity/canned hijack.
- "make her hair red" → merged prompt above queued against the same
  job lane; `active_context` on the conversation row shows
  `active_intent=image_followup` + `active_image_subject` intact.
- **Restart persistence**: backend hard-restarted; "now make her wings
  white instead" resolved the persisted angel subject and queued
  `a photorealistic adult angel with black wings, wings white instead`
  — context survives restart on disk per spec.
- An autonomous repair mission (`m-2ae60b5e9d60`, repairing the CI
  splash-test failure) occupied the lane mid-dogfood — queued chat
  drained correctly through the mission-subtask path; `_preempt_for_chat`
  + bounded queue semantics intact.
- Live image backends: ComfyUI jobs finish; InvokeAI had
  output-tensor-directory failures (`…\outputs\tensors\tmp* does not
  exist`) — environment/backend state, unrelated to this change set.

### Known limitations / next

- Compound local actions landed: fully-parsed clause sequences execute
  through the verified lane with gate-aware stop/resume; mixed clauses
  (action + non-action) still fall to the model lane whole.
- Ambiguity surfacing landed: envelope markers reach the model as
  prompt advisories and render as an "Ambiguous" hint chip in chat.
- `web/app.js` builtin replies gained a small variant bank
  (`CLIENT_REPLY_VARIANTS` rotation); deeper JS-side sharing with the
  Python `PersonaRenderer` is future work.
- Frozen backend rebuilt post-change-set and deployed to the live
  install below.

## 2026-10-05 — Integrated reliability closeout (branch `milestone/integrated-reliability-closeout`, IN PROGRESS)

Live verification on the real install (`D:\Nexus_Core`, RTX 3080 Ti 12GB,
64GB RAM, InvokeAI 6.14.2, ComfyUI portable). Backend under test:
current source on this branch via `--config D:\Nexus_Core\config.json`.

### ComfyUI dogfood (P2) — VERIFIED

- `qwen-image-2.1` t2i: managed cold-boot via
  `comfyui_start_on_image_request`, real 20-step 1024² PNG (147s incl.
  boot). Workflow `qwen/qwen-image-2.1-t2i-api.json`.
- `flux2-klein-4b` t2i: finished, real PNG (80s). Workflow
  `flux/flux2-klein-4b-t2i-api.json`.
- `juggernaut-x-v10` SDXL t2i through ComfyUI: finished, real PNG.
- Qwen `remove_background`: real output via
  `qwen-image-2.1-background-removal-api.json`.
- Cancel mid-flight: clean `cancelled` state at 29%, zero partial
  outputs, no phantom success.
- Outputs land in `data/image/generations/<job>/` + mirrored to
  `output/images/`; history.json records every finished job;
  `backend_job_id` + `vram_before/after` persisted per job.

### InvokeAI↔ComfyUI fallback matrix (P3) — VERIFIED LIVE

| Case | Setup | Result |
|---|---|---|
| A | auto, both up, SDXL t2i | InvokeAI chosen (RealVisXL fleet route), real output |
| B | `remove_background`, auto | ComfyUI — "InvokeAI does not support it natively", real output |
| C | InvokeAI unavailable, auto | ComfyUI fallback, real output, honest routing reasons |
| D | explicit `invokeai` pin, unavailable | HTTP 400 honest failure — no silent swap |
| E | ComfyUI down, auto t2i | InvokeAI succeeds |

### GPU arbitration (P4) + crash recovery (P9) — VERIFIED LIVE, `cc702cb2`

- Real defect observed + fixed: managed ComfyUI resident (~11GB)
  starved an InvokeAI model load → InvokeAI died mid-job (WinError
  10054), job failed, self-heal's respawn also OOM'd.
- Fix: `evict_if_managed()` on both runtimes (Nexus-owned only —
  external user servers untouchable); peer-backend eviction wired into
  both job paths when memory stays short after LLM release; one bounded
  restart+resubmit on `BackendConnectionError` for both engines.
- Live re-verify: `llama-server` resident → Qwen job released it (0
  procs), evicted managed InvokeAI (reason recorded), generated, LLM
  restored (1 proc) after completion.

### Closeout pass 2 (P5–P17) — VERIFIED LIVE

- **Soak/selftest**: `selftest --test-timeout` default raised 300→900s
  (suite now 1948 tests, ~310s on this box); full pass = tests + isolated
  clean-instance boot, 39 smoke checks. Nightly tier-3 workflow
  `soak.yml` runs it + autonomy/provisioning/LKG/pipeline suites;
  hardware tier is self-hosted-GPU gated via `workflow_dispatch` only.
- **UI sweep** (headless Edge, all 15 pages, real backend): found three
  live defects — `/api/provisioning` unreachable (missing
  `_PLATFORM_PREFIXES` entry — Command Center card dead), `voice/speak`
  500→honest 503, undefined `setStatus` pageerror on notifications.
  `RoutePrefixCoverageTests` now statically prevents orphan routes.
- **Autonomy soak + chaos**: mission → plan → executing → **backend
  hard-killed** → restart → durable resume → watchdog reaped orphaned
  driver task → replan → `waiting_for_vram` queue (honest) → freed →
  verify gated `run_tests` → **approval re-ask loop found and fixed**
  (approve now stamps `metadata.approval_granted`; `_default_verify`
  honors it; agent nodes resume the parked task via `agent.resume`
  instead of re-running fresh). Mission completed E2E, 0 pending.
- **Audited clean**: signing (signtool+`/DWithSign`+secrets, honest
  unsigned fallback — external cert is the only prerequisite), disk
  mgmt (5GB detector + managed-scratch-only fixer), sensitive-action
  gating (always-ask + standing grants + stop/egress/RC gates).
- **Approval-loop closeout** (`ffa7a4b1`, `a68dc631`): three distinct
  re-ask vectors found and fixed — `_default_verify` gate stamp,
  agent-node resume of the exact parked task (`agent.resume` not fresh
  `agent.run`), and `op == "tool"` job nodes passing
  `approved=approval_granted`. Deny path verified bounded
  (max_approval_retries=2 → replan → blocked+notify).
- **Long-run retention sweep** (`8f65a652`): unbounded-growth audit
  found four leaks — terminal missions never archived (now auto-archive
  quiet completions/cancels at 30d, failed never), resolved approvals
  accumulated forever (now 7d/newest-200 prune on append), notifications
  uncapped (now 500 rows), desktop `backend-host.log`/`crash_history.
  jsonl` append-forever (now rotate at 8MB/4MB and 1MB/512KB). Task
  ledger, worker queue/history, image history, conversations, activity
  JSONL, and audit log were already bounded.
- **`scripts/mission_soak.py`** — new real mission-loop soak: submit →
  execute → auto-approve gates → terminal, hard-kill every Nth cycle,
  JSONL event log + JSON summary. Running against the real install.
- **Demand-driven eviction** (`21b7120d`) — VERIFIED LIVE: mission
  nodes gated `waiting_for_vram` fired `admission_eviction` audit
  events (`qwen3-8b`, `qwen3-14b` released LRU-first) instead of
  waiting for the 900s idle timer. Same managed-only contract as the
  image path. Also fixed: SSE stream sinks now detach in a finally
  (orphaned streams leaked before, `c26d673b`).

- **Soak round 1 finished** — 10 cycles: **0 crashes, 3 hard-kills, 4
  clean recoveries**, all VRAM waits honest. All 10 missions `blocked`:
  the 4B asserted file writes with zero tool calls — `unverified action
  claims` fired, `artifact_exists` caught the missing files, missions
  blocked instead of passing. Honest blocking, but exposed two gaps:
  fabricated node output recorded `ok=True` (the artifact check was the
  only line of defense), and retries reran the identical instruction so
  the model could only repeat the lie.
- **Fabrication fail-fast** (`6a20f24b`) — agent nodes carrying the
  `Unverified action claims` marker now report `ok=False`; the marker is
  a shared constant (orchestrator emits, server matches — can't drift).
- **Retry feedback** (`25ec078e`) — a retried node gets the recorded
  failure appended to its instruction ("previous attempt failed:
  unverified action claims — actually invoke the required tools");
  `_finish_node` persists `result.error`. Regression tests added
  (fake-server prose mode + instruction assertion).
- **Retry-cooldown liveness** (`331c1fb4`) — VERIFIED LIVE: two bugs
  made the bounded retry dead on arrival. (a) A node parked in
  `waiting_dependency` on retry cooldown was neither runnable nor
  "stuck", so the next tick blocked the whole mission before the retry
  fired — cooldown parks now keep the mission `executing`. (b) `blocked`
  dependents latched permanently even when the recovery playbook reset
  the dep and it later completed — `blocked` is now re-derived every
  refresh. Verified live: dep failed → parked → dependent un-blocked →
  retried.
- **Budget-pause auto-resume** (`e2571e22`) — live evidence: a
  llama-server load spike dropped available RAM to 1.4 GB (< 2 GB floor)
  and the budget gate paused the mission *permanently* — one transient
  dip would have ended an unattended run. Budget pauses now carry a
  marker, re-check every ~15s, and auto-resume to the pre-pause status;
  explicit user pauses never auto-resume. Verified live: `paused ->
  executing · budget clear — auto-resumed`. Follow-up (`61803479`):
  only *transient* violations (RAM/disk) get the marker — a runtime
  deadline or exhausted repair cap can never self-clear and would flap.
- **Unbounded playbook loop** (`ac6f0c1d`) — live evidence: three failure
  records all stuck at `playbook_step=1` — every retry minted a fresh
  record, the cursor reset to step 0 (retry) forever, and varied
  fabrication text defeated the signature-based same-failure bound.
  `record_failure` now continues the previous record for same
  task+class (cursor advances retry→inspect→escalate) and a per-record
  `count` feeds `budgets_exceeded`. Verified live: one record,
  count=2, step=2 → replan dispatched.
- **Episodic memory retention** (`f8a9d8c1`) — hippocampus `episodes`
  was append-only sqlite (≤8KB/node write, forever). Hourly-throttled
  prune: >30d gone, newest 5000 kept. Procedures/project_facts already
  upsert-keyed; all autonomy JsonStores have row limits; JsonlLog
  caps at 4MB→2MB.

### Remaining milestone work

Soak round 2 is running (6 cycles) on the fabrication fail-fast + retry
feedback code — watching whether feedback breaks the fabrication loop.
The multi-hour *overnight* soak remains the honest completion of the
hardware tier that CI's hosted runners cannot do — the harness is
proven (kill→restart→resume verified live, demand eviction verified
live), it just needs wall-clock hours. Release packaging gate still
applies (unsigned unless code-signing secrets are set).



New on top of the InvokeAI backend work:

- **`localcodeagent/image/fleet.py`** — declarative photoreal fleet:
  Juggernaut XL v9 (`general_photoreal`), CyberRealistic XL
  (`portrait_photoreal`), RealVisXL V5.0 (`glamour_photoreal`,
  `adult_capable`). Verified HF `repo::file` sources, real byte sizes and
  SHA-256s, per-model licenses, trait weights, per-model sampling
  defaults. `fleet_for_model_name` matches installed backend rows;
  `classify_request_traits` + `score_fleet_model` route prompts.
- **Router**: `_fleet_pick` scores fleet-tagged InvokeAI profiles before
  the generic priority sort (fleet ops only: text_to_image/edit/inpaint/
  variation). Winner unfit → next scored model. `model_override` accepts
  fleet ids (`juggernaut-xl-v9`) as well as `invokeai:<key>`; wrong-backend
  pins error honestly.
- **Manager**: `_refresh_invokeai_models` attaches `fleet_id`/`fleet_role`/
  license/sampling-defaults metadata; `_invokeai_model_row` exposes them
  to the UI (role chip + license in model cards, fleet ids in the Model
  dropdown).
- **SamplingAdvisor**: `_family_defaults` reads `metadata.sampling` model
  defaults (fleet steps/guidance/sampler/scheduler) below learned params
  and explicit request values; `_key` gains `model_scope` so outcomes
  learn per fleet model per backend.
- **InvokeAI adapter**: `install_model(source)` /
  `model_install_jobs()` / `model_install_job(id)` /
  `cancel_model_install(id)` over `/api/v2/models/install` (v1 fallback).
- **`localcodeagent/provisioning.py`** — `ProvisioningManager`: versioned
  declared stack (voice assets → InvokeAI → Juggernaut → Whisper →
  CyberRealistic → RealVis → ComfyUI), persisted plan at
  `data/provisioning/plan.json`, mid-flight requeue on restart, disk
  reserve gating with `blocked_reason`, classified errors
  (`network_failure`/`disk_full`/`checksum_failed`/`unsupported_python`/
  `backend_health_failed`/…), bounded exponential retry (3×, 30s×4ⁿ cap
  10min), one spoken line per incident via `_speak_notice`, capability
  `provides` → registry invalidation + `wait_for_capability`,
  `on_capability_ready` drains deferred image requests
  (`waiting_for_capability` in `/api/image/generate`).
- **Server/API**: `_start_provisioning` wires manager + notification/
  voice/SSE hooks; `GET /api/provisioning`, POST `pause|resume|cancel|
  retry|config`; shutdown stops the scheduler.
- **UI**: Command Center "Background setup" panel (per-item badges,
  bytes/speed/ETA, bars, retry/cancel, pause/resume); Settings → Setup
  section (three toggles persisted via `provisioning/config`).
- Config: `provisioning_enabled`, `provisioning_parallel`,
  `provisioning_auto_retry`, `provisioning_voice_notifications`,
  `provisioning_disk_reserve_bytes` (all default-on sane values).
- Tests: `tests/test_fleet.py` + `tests/test_provisioning.py`
  (35 tests — classification, scoring order, fleet pick/fallback/
  override, per-model learning keys, plan shape, resume, retry, disk
  gate, capability wake) + 3 adapter install tests.
- Docs: `docs/BACKGROUND_PROVISIONING.md` (new), `docs/IMAGE_SYSTEM.md`
  fleet section, `MODEL_ROUTING.md` fleet routing, `docs/INVOKEAI.md`
  managed-install API.

Dogfood COMPLETE on live InvokeAI 6.14.2 (RTX 3080 Ti):
all three fleet checkpoints verified SHA-256, registered as
`main`/`sdxl`, matched by `fleet_for_model_name`, and routing verified
— Times-Square scene → `juggernaut-xl-v9`, beauty portrait →
`cyberrealistic-xl-v9`, glamour/boudoir → `realvisxl-v5`. Real
generation through `InvokeAIBackend.submit` completed for **all
three**: Juggernaut (1024², ~32 s), CyberRealistic (832×1216, ~60 s),
RealVisXL (832×1216, ~270 s — fp16 reload under VRAM pressure). Real
PNGs verified in `data/invokeai/outputs/images/`. Earlier attempts
died at `Executing queue item … on cuda:0` when a second app held
~10.8 GB VRAM — once freed (~4.7 GB used by desktop), both models
generated cleanly. One earlier run surfaced a clean `CUDA out of
memory` job failure — the honest error path works.
`98ec4e05` switched `invokeai_source` to direct
HF `resolve/main` URLs — `repo::file` downloads into a folder the
model identifier can't classify (registers `unknown`/`tmpinstall_*`).

**Upstream bug found + local workaround**: InvokeAI's model probe runs
every config class including `Spandrel_Checkpoint_Config`, which
*fully loads the state dict*; `safetensors.torch.load_file` segfaults
(access violation → silent `invokeai-web` exit, no traceback) on the
7 GB fleet files on this box. Patched the venv
(`tools/InvokeAI/Lib/site-packages/invokeai/backend/model_manager/
configs/spandrel.py`) — `_validate_spandrel_loads_model` raises
NotAMatch for files >2 GiB (marked "NEXUS PATCH"). **Caveat**: the
patch lives in the installed venv — but `InvokeAIRuntime._command`
re-applies it idempotently via `_apply_spandrel_guard(root)` on every
managed start (`4f476f1b`), so reinstalls/upgrades self-heal.
Candidate classes run even after a match, so the size guard matters
for every large checkpoint install.

**Provisioning hardening (`ac5bea18`)**: `invokeai_model` items now
enumerate the backend before submitting — a fleet checkpoint already
registered (upgrade, manual/pre-seeded install) verifies and skips the
7 GB download. And a vanished install job (invokeai-web restart wipes
in-memory rows) now raises a retryable failure instead of the poll
loop waiting on `status=""` forever.

**Deploy build reburned**: `backend-new/` in the live install now
contains the fleet + provisioning + voice-data + `shutil` fix build
(`5af83f39` source), staged via `update.flag` — next app launch swaps
it in. Frozen-build smoke test found and fixed a real NameError:
`_pick_python` used bare `shutil` while the enclosing scope aliases it
(`_shutil`) — the venv-install path the provisioner uses for
`python_candidates` manifests would have crashed.

### Prior milestone — InvokeAI as a first-class image backend

Commit `c15afaf4` (pushed, CI pending): **InvokeAI is a real
Nexus-managed image backend** alongside ComfyUI. Auto routing prefers
InvokeAI for standard generation/editing; ComfyUI stays the advanced
custom-workflow engine and fallback.

- `localcodeagent/image/invokeai.py` — dependency-free REST adapter:
  health probe (3s cached, single-flight — a dropping endpoint must not
  stall `/api/status`), `/api/v2/models` listing, `enqueue_batch` queue
  lifecycle (multi-item status/cancel/fetch), multipart image upload,
  Nexus-spec → InvokeAI graph builder (sd-1/sd-2/sdxl bases; flux etc.
  honestly unsupported → ComfyUI routes).
- `localcodeagent/image/invokeai_runtime.py` — managed runtime mirroring
  ComfyUIRuntime: venv/script/PATH discovery, `invokeai.yaml` host/port
  generation (v6 `invokeai-web` takes only `--root`), managed-PID orphan
  reclaim, idle eviction, external-vs-managed distinction.
- `manager.py` — `backends`/`backend_runtimes` dynamic dicts,
  `_select_backend` (override → configured `image_backend` → auto),
  `_invokeai_ready`, synthesized `invokeai:<key>` model profiles,
  `_run_invokeai_job` dispatch, cancel/status/feedback per backend.
- `ImageRequest.backend_override` (`auto|invokeai|comfyui`),
  `ImageJob.backend`, `ImageModelProfile.metadata`,
  `capability_class`/`restriction_status` classification
  (`adult_capable`, `restricted_by_model`, `restricted_by_provider`,
  `local_unfiltered_model`, `unknown_capability`).
- SamplingAdvisor learning keyed per backend (`invokeai|…` keys);
  ComfyUI recipes never bleed into InvokeAI selection.
- Config: `image_backend`, `invokeai_endpoint`, `invokeai_auto_start`,
  `invokeai_start_on_image_request`, `invokeai_dir`,
  `invokeai_python`, `invokeai_extra_args`, `invokeai_logs_dir`,
  `invokeai_startup_timeout`, `invokeai_idle_unload_seconds`.
- Server: `invokeai` managed-process registration (capability
  `image_generation`), idle evictor, `/api/image/backend/{start,stop,
  inspect}` accept `backend`, `POST /api/image/preference` persists the
  choice, `/api/image` summary now returns `backends.{invokeai,comfyui}`.
- Install: `tools/manifests/invokeai.json` + `venv` install method in
  `install_tool` (per-tool venv under `tools/InvokeAI`).
- UI: backend selector (Auto/InvokeAI/ComfyUI) + per-backend status
  cards + model filter in `web/image.*`; image tools take a `backend`
  argument.
- Health probe aggregates both engines; mission image artifacts record
  the real backend name (`tool=invokeai|comfyui`), not hardcoded.
- Tests: `tests/test_invokeai.py` (30 tests — adapter, routing,
  runtime discovery, error normalization, per-backend learning);
  test configs pin dead endpoints so a live local backend can never
  contaminate hermetic tests.

### Dogfood status (real, in this environment)

- InvokeAI **6.14.2** pip-installed into `tools/InvokeAI` (venv,
  Python 3.12 — InvokeAI rejects ≥3.13; also rejects the repo's 3.14
  launcher default).
- torch upgraded to `2.14.1+cu126` — pip's default wheel is CPU-only on
  Windows; InvokeAI otherwise silently runs CPU mode.
- Dreamshaper 8 (sd-1, 5.5GB) installed via `/api/v2/models/install`.
- **Verified against the live server**: version probe, model list,
  text-to-image (real PNG), img2img edit, inpaint (masked region),
  3-way variation (distinct seeds — `runs` reuse produces identical
  images, so submit fans out one batch per image), cancel mid-flight,
  Nexus `create_job` auto-routing to InvokeAI with output persistence,
  and honest errors for pinned-but-dead backends.
- API fixes found by dogfooding: v6 mounts the model manager at
  `/api/v2/models` (not v1), enqueue is `enqueue_batch` with a nested
  `{"batch": ...}` body, upload params are query-string not form fields,
  `create_denoise_mask` needs both `image` and `mask` inputs.
- Not dogfooded: LLM-resident VRAM contention and live ComfyUI fallback
  (ComfyUI not installed on this box; fallback is unit-tested).
  — **since closed**: see "Integrated reliability closeout" entry below.

### Deployment to live install (D:/Nexus_Core)

- `Source/` synced to `de5064ea`; web assets copied into
  `backend/_internal/web` (old backend filters unknown request fields,
  so the new UI is safe against the pre-swap binary).
- `tools/manifests/*.json` copied to `D:/Nexus_Core/tools/manifests/` —
  neither the runtime-root dir nor `_internal/tools/manifests` existed,
  so no manifests (InvokeAI or otherwise) were visible to the install.
- `config.json` on the install now sets `image_backend=auto`,
  `invokeai_endpoint`, `invokeai_dir` (points at the dev venv
  `D:\Devin\chat-nexus\tools\InvokeAI`), `invokeai_auto_start`,
  `invokeai_start_on_image_request`.
- `D:/Nexus_Core/data/invokeai` is a junction → the repo's
  `data/invokeai` (shares the Dreamshaper model store + invokeai.yaml).
- PyInstaller rebuild staged via the app's own LKG update path:
  snapshot `snap-1791164241262`, `backend-new/` = fresh 0.21.0 build
  (verified: `localcodeagent.image.invokeai{,_runtime}` in PYZ,
  `tools/manifests/invokeai.json` bundled, VERSION 0.21.0), and
  `update.flag` written — the desktop host swaps on next launch.
- Build fix: `--add-data tools` → `tools\manifests` in both
  `packaging/build_windows.ps1` and `selfupdate._build_cmd`; otherwise
  the next release/self-update build would try to bundle the multi-GB
  `tools/` install payloads (InvokeAI venv, ComfyUI portable).
- **Voice regression found in staged build**: the stale
  `build/ChatNexus.Backend.spec` predated the `--collect-all
  kokoro_onnx/phonemizer/espeakng_loader` args, so `_internal/kokoro_onnx`
  shipped only a dist-info — `Kokoro()` failed on
  `_internal/kokoro_onnx/config.json` → `/api/voice/speak` 500 → no
  greeting, replies cut out. backend-old (0.20.0) had the same defect.
  Rebuilt with the full `build_windows.ps1` arg set (fresh spec);
  verified `config.json` + phonemizer + espeakng_loader + voice presets
  bundled and `/api/voice/speak` returns a real 3.11s WAV. Re-staged to
  `backend-new/` (snapshot `snap-1791165159653`, flag rewritten).
  `piper_onnx`/`llama_cpp` collect lines in the old spec were no-ops —
  neither package is installed; llama runs as an external binary.

## Earlier milestone — v0.20.0 workstation P1 + P2 (self-update, ops UI, search)

(Previous: v0.18.x honesty hardening · v0.16.0 persona depth · v0.15.0
autonomous workstation layers)

The workstation program's P0 dependency layer is implemented, tested,
and pushed in milestone commits. **Last verified: `3d3f0f3` — full
local suite 1789 tests (1 port-contention flake, passes standalone),
GitHub Actions `Nexus Core Tests` green on main.** (`19fd457` portrait → `7e09fa7`
workspace UI → `f33b173` paste-attachments):

- **Capability Registry** — `localcodeagent/capabilities.py`; probed
  states + dispositions for filesystem, code editing, terminal, git,
  github, toolchains, testing, browser preview, deployment, image gen,
  STT, TTS, desktop control, coding model. `GET /api/capability-states`,
  prompt injection of blocked caps, and capability-contradiction
  flagging in the truth gate (25 tests).
- **Workspace Manager** — `localcodeagent/workspace.py` +
  `tools/workspace.py`; detect/adopt/persist workspaces, registered
  roots extend `_safe_path`/shell/terminal/build boundaries (15 tests).
- **Application-builder loop** — `localcodeagent/scaffold.py` (7
  templates) + `tools/project.py` (`project_scaffold`, `project_setup`,
  `app_health`, `project_templates`); pipeline guidance added to
  SYSTEM_PROMPT (13 tests).
- **Git + GitHub** — extended `tools/git.py` (14 ops incl.
  `git_user_changes` pre-flight) and `tools/github.py` (connect/
  disconnect/auth-status/repos/clone/PR-inspect/actions-run/rerun/
  issue-comment/compare/checkout-pr, vault-backed token);
  `register_github_tools` now takes `vault` + `workspaces` (10 tests).
- **Workspace UI** — `web/workspace.{html,css,js}` +
  `/api/fs/{tree,file,mkdir,delete,rename,reveal}`,
  `/api/git/{status,diff}`, `/api/terminal/run`; nav added to all pages;
  layout contract updated (24/24).
- **Identity** — `web/assets/nexus-portrait.jpg` (creator-supplied
  canonical face) now feeds `/api/nexus/avatar`.
- **Composer** — >20-line input auto-becomes an attachment.

### P1 progress — dev-server, pipeline, interactive priority

**Last verified: `a45c24c` — GitHub Actions `Nexus Core Tests` green
on main.** Commits `b2d2ea8` → `5c2718b` → `a45c24c`:

- **Dev-server manager** — `localcodeagent/devserver.py` +
  `tools/devserver.py`: workspace-bound start/list/wait/stop, URL
  detection, health probes, persistence, duplicate detection;
  workspace.html Servers + Preview panels (6 tests).
- **Coding pipeline** — `localcodeagent/coding_pipeline.py`: DAG
  planner (spec→implement→verify→review→serve-check→commit),
  `stage_view()`, `tool`/`serve_check` mission job ops, `git_commit`
  tool, pipeline options on mission records (7 tests).
- **Interactive chat outranks missions** — pending user queue items
  count as lane demand; `_dequeue_next` prefers non-mission items;
  `_preempt_for_chat` cooperatively cancels a mission node holding the
  lane (supervisor retries it). Mission/recovery/auto-retry no longer
  open a voice lane — every `begin_task` `stop_all()`ed user speech
  (the reported "no voice at all").
- **Queued-response replacement** — `queue_item_completed`/
  `queue_item_failed` bus events carry content; app.js swaps the
  "Queued…position N" placeholder for the real response in place.
- **Worker pool naming** — Molly, Nikki, Kate first, then a female-name
  pool with counter-suffix recycling; surfaced via /api/workers and
  the missions panel.

### P1 progress — LKG, self-update, Command Center

Commits `0356e54` (LKG + self-update) and `4a67a14` (Command Center):

- **LKG** — `localcodeagent/lkg.py`: hashed snapshots of the live
  backend + config, verify/restore, single-consumption rollback flag,
  auto-request after repeated unclean boots; `/api/lkg{,/verify/*,
  /snapshot,/rollback}` (8 tests).
- **Self-update bootstrapper** — `localcodeagent/selfupdate.py`:
  plan (fetch, behind-count, dirty refusal), apply (pull → tests →
  build → LKG snapshot → stage → flag); `/api/update/{status,plan,
  apply}`, apply confirm-gated. The desktop host consumes rollback/
  update flags before backend launch — a frozen exe can't replace
  itself mid-run.
- **Command Center** — `web/command.{html,js}`: unified ops surface
  (status, named workers, missions, queue, dev servers, capability
  states, safe mode, LKG, self-update). Nav added to every shell page;
  canonical-link contract updated (24/24).
- **Fix during this work** — a method splice inside `AppState.__init__`
  left it returning early (no `agent`/`autonomy`); the full e2e suite
  caught it, method moved to class level, 34/34 green.

### P2 progress — search, quality tools, ops UI depth

- **Global search** — `localcodeagent/search.py` + `GET /api/search`;
  `web/palette.js` Ctrl+K palette on every page (7 tests).
- **Dependency audit** — `tools/audit.py`: `dep_list` (offline manifest
  parse) + `project_audit` (real pip-audit/npm audit/cargo audit,
  honest `auditor_unavailable`) (5 tests).
- **Coverage** — `coverage_report` runs the real coverage step per
  build system; errors honestly when none exists.
- **Release packaging** — `tools/release.py`: `package_release` zips a
  workspace dir to `.agent/releases/` with sha256 + artifact registry;
  `release_verify` re-hashes (4 tests).
- **Command Center depth** — Recent activity feed via
  `ActivityStore.recent()` + `/api/activity?recent=N`; mission detail
  renders a Pipeline stage strip for coding-pipeline missions.
- **Palette deep-links** — file hits navigate to
  `/workspace.html?file=<path>` which opens the file in a tab.
- **Static deployment** — `tools/deploy.py` `deploy_static`: detached
  worktree → build/copy output → deploy branch → push. Real remote
  results only; base worktree untouched (4 tests).
- **Headless debugger** — `tools/debugger.py` `debug_run`: bdb-driver
  subprocess, breakpoint-local capture + post-mortem frame walk with
  locals; driver source embedded for frozen builds (5 tests).
- **Knowledge browser** — `/knowledge.html`: entity search, attrs,
  relations, neighbor traversal; `/api/knowledge?id=&depth=` returns
  entity + neighborhood; bare call lists recent entities.
- **Dependency/security UI** — `/api/audit/deps` (GET) and
  `/api/audit/run` (POST) + Command Center panel rendering manifests,
  dep entries, and per-ecosystem audit summaries.
- **Source-sync safety** — `_start_source_sync` skips when
  `workspace == code root` (dev/CI/selftest): a smoke instance
  previously ff-merged the CI checkout mid-suite and flipped VERSION
  under the test process (`f7130b5`). Smoke config also sets
  `sync_source_on_start: false`.
- **Live-install verified** — backend rebuilt and deployed to
  `D:\Nexus_Core`; `/api/lkg`, `/api/update/status` (source head
  `b905d8d`, clean), `/api/search` all answer; `Source` synced.
  The smoke caught a real bug (`self.state.search` never initialized →
  lazy `_global_search` fix, `276917d`).

### Workstation program — what remains

- **P2** — search, audit, coverage, release packaging, activity feed,
  stage strip, static + release deploy adapters, debugger, knowledge
  browser, per-project knowledge views, and dep/security UI landed.
  Remaining: browser E2E (needs Playwright bundled into the exe — a
  packaging decision) and anything newly specified.
- Identity manager is already richer than listed —
  `nexus_avatar.py` has expression states, gestures, activity.
- `version()`/`server.VERSION` are `lru_cached` — any in-process file
  mutation (self-update swap) requires a restart to observe.
- Honesty rules that must never regress: no action claims without
  execution; pushes only count when the remote confirms; tests only
  "pass" when they ran; workspace writes stay inside registered roots;
  destructive ops require authorization.

## Historical milestone — v0.16.0 persona depth

(Previous: v0.15.0 autonomous workstation layers)

- `main` now carries the decomposed mission planner (parallel lanes →
  integrate → review → verify), plan versioning, project-linked mission
  context, code-reference/impact tools, STT/push-to-talk + hands-free
  session control, operational state + return briefing, mission
  checkpoints, Knowledge Library, structured correction learning, granular
  desktop-control hardening, the avatar animation/lip-sync foundation,
  managed skill/plugin lifecycle depth, self-repair rollback gating,
  bounded long-duration approval recovery, catalog LLM/image model
  installs as mission jobs with restart-safe `.part` cleanup, and a
  dedicated verified Real-ESRGAN post-process upscaler.
- Optional host stacks are now provisioned and pinned for local STT
  (`voice-stt`) and code intelligence (`code-intel`); real validation
  covered faster-whisper/PyAV transcription, tree-sitter extraction,
  pylsp round trips, and official MCP servers on both stdio and
  Streamable HTTP transports.
- The isolated selftest now dogfoods the workstation surface, not just
  boot: it checks every primary page plus health/skills/knowledge/RAG/LSP,
  missions/autonomy, tools/workflows/MCP/jobs/projects/preferences/library,
  processes/resources/readiness/tasks/activity/queue/STT/operational-state/
  briefing APIs and the shared SSE bus. Its log capture is UTF-8 safe, and
  CI log probes decode external `gh` output with replacement.
- Release packaging now asserts every bundled workflow is present —
  including `ask_workspace`, `clip_transcribe`, and the dedicated
  `image/upscalers/realesrgan-x4plus-api.json` post-process path — in both
  the Windows build contract and installer smoke test. Build/test
  dependencies are pinned (`pyinstaller`, `pillow`, `cryptography`, `py7zr`,
  Kokoro/onnx/numpy stack), and CI also pins the .NET SDK and Inno Setup
  instead of floating `pip`, SDK, installer, or resolver inputs.
- Correction-learning invariant: `PreferenceStore` learns only explicit
  behavioral directives as candidates; repeated evidence or user action
  activates them, prompt overlays are bounded, and permissions/safety/
  secrets/creator-locked identity can never be overridden. See
  `docs/LEARNING.md`.
- Desktop-control invariant: computer use stays behind the central
  `ToolRegistry`/`PermissionManager`, now with granular approval-gated
  desktop/screen/mouse/keyboard/clipboard/application permissions.
  Autonomous mode never auto-grants them, coordinates/handles are
  validated, and the audit trail records terminal action state without
  typed or clipboard contents. See `docs/DESKTOP_CONTROL.md`.
- Canonical portrait hook remains `web/assets/nexus-portrait.*`; do not
  substitute another face or character for Nexus. `NexusAvatar` now keeps
  bounded activity/expression state; `web/avatar.js` consumes voice playback,
  STT/agent state, gesture SSE, and `/api/nexus/state` for subtle CSS-only
  motion without delaying speech or shipping conversation/audio content to
  the renderer. See `docs/AVATAR.md`.
- Skill packages now carry a richer manifest surface (author/provider,
  capabilities, tools, permissions, dependencies, OS support, update/UI/health
  metadata) and support verified install/update, enable/disable, bounded
  rollback snapshots, removal, and explicit health execution. Mutating
  lifecycle actions are `skills.manage`-gated and disclose requested
  permissions/capabilities in approval responses; executable health checks
  use the separate `shell.execute` gate. See `TOOLS.md`.
- Self-repair rollback is now byte-verified and path-safe: promotion fails
  closed if LKG preservation cannot stage, manifests are bounded/validated
  before restore, snapshot retention is capped, and repair lifecycle API
  mutations require the `repair.manage` hard gate. See
  `docs/architecture/SELF_REPAIR.md`.
- Runtime/process control is now permission-gated: `runtime.manage` (ask in
  every profile, never auto-granted in autonomous mode) gates
  `POST /api/processes/action` and `/api/runtime/start|stop`; external
  runtimes still refuse control after approval. `ProcessManager.list()`
  reports live per-service RSS/commit/CPU-seconds (Win32/process counters
  or /proc — no psutil), `HardwareSnapshot` adds CPU name/cores, pagefile,
  uptime, load average, and `ram_used_percent`, `/api/resources` carries the
  managed process list, and nvidia-smi output decodes UTF-8-safe.
- Live-model dogfood passed on real bundled artifacts: managed
  `llama_cpp` start via `/api/runtime/start` gated on `runtime.manage`
  (unapproved → `needs_approval`, approved → llama-server healthy in ~18s,
  VRAM 10.57→6.31 GB visible in the enriched hardware snapshot), then a
  real Qwen3VL-4B chat answered "PONG" in 0.7s on the utility lane and a
  "remember that…" turn persisted to `/api/conversation-memory`. External
  runtime still refuses control after approval.
- Conversation learning is dogfooded over real `/api/chat` too:
  training-command turns ("remember that…", "always…", "no, you should…")
  feed `learn_from_user` without a model, land in facts/rules/training
  examples, reach `prompt_context` and `/api/conversation-memory`, and
  reload verbatim after restart.
- The creator-Brain lifecycle is now dogfooded over real HTTP
  (`tests/test_nexus_brain.py::NexusBrainHttpLifecycleTests`): initialize →
  stage → `sync` counts → prompt injection → subroutine gating → session
  lock, plus a signed-export round-trip into a second live server (verified
  read-only install, tampered payload 403, disk reload still verified).
  Dogfood found one real fix: creator-session `PermissionError`s answered
  500; they now map to 403.
- **Phase 1 of the intelligence/reliability milestone** — first-class
  requirements (`localcodeagent/requirements.py`): durable rows with
  lifecycle (`not_started…verified/failed/blocked`), evidence entries,
  and `inferred` provenance so derived criteria are never mistaken for
  explicit asks. Repair-intent chat ("fix this", "make it work") derives
  acceptance criteria BEFORE work runs — `/api/chat` returns
  `acceptance_criteria`, SSE emits a `requirements` event, and rows
  persist scoped task→conversation→global. Mission creation derives +
  links `requirement_id` onto each `success_criteria` entry; the
  evaluator carries that ID through `criteria_results` so
  `sync_mission` flips rows verified/failed from real evidence.
  API: `GET /api/requirements` + `POST /api/requirements{/status,/evidence}`;
  missions page renders a requirements checklist with inferred badges.
  Next phases (audit-backed): hypothesis lifecycle + causal memory +
  decision journal, then risk-classified promotion, evidence board,
  reliability scoring, regression memory/bisect, resource modes,
  offline/egress policy, lineage, Safe Mode, golden config, RC scorecard.
- **Phase 2** — hypothesis engine (`hypotheses.py`, lifecycle
  proposed→confirmed/rejected, discriminating-test picker), causal
  memory (`causal.py`, mechanism-level chains seeded into the next
  diagnosis as ranked priors — candidates to test, never assumed), and
  decision journal (`decisions.py`). `SelfRepairCoordinator` persists
  hypothesis rows at diagnosis, journals the repair-kind choice with
  alternatives, and on resolution confirms the winner, rejects losers,
  and writes the causal record. API: `/api/hypotheses`
  (+status/evidence/test), `/api/causal-memory`, `/api/decisions`
  (+outcome). Docs: `docs/REASONING.md`.
- **Phase 3** — risk classification (`risk.py`: low/medium/high with
  explainable reasons; isolated experiments downgrade, irreversible
  kinds never do) + promotion pipeline (`promotion.py`: durable
  candidates gated through simulate→targeted→regression→review→canary;
  out-of-order stage writes and unpassed-stage promotions refused).
  API: `/api/promotions` (+stage/promote/reject), `/api/risk`.
  Docs: `docs/PROMOTION.md`.
- **Phase 4** — shared evidence board (`evidence.py`): typed entries
  with provenance/confidence, scoped retrieval, contradiction linking
  (both sides marked, never silently preferred), resolution with
  winner confirmation, open-question tracking. API: `/api/evidence`
  (+contradict/resolve).
- **Phase 5** — persisted reliability scoring (`reliability.py`):
  per-subject calls/ok/latency/retries/error-classes + rolling window,
  self-healing status tiers (verified/available/degraded/broken/
  untested/disabled/unavailable), `CapabilityHealth` with self-test
  probes, and `ToolRouter` now records every outcome into the durable
  tracker. API: `/api/reliability` (+record), `/api/capabilities`
  (+probe). Docs: `docs/RELIABILITY.md`.
- **Phase 6** — regression memory + baselines + bounded bisect
  (`regressions.py`, `bisect.py`): verified behaviors open
  `regression_detected` events with `known_good_head`; Welford
  baselines flag only statistically meaningful regressions;
  `GitBisector` binary-searches `good..bad` inside a throwaway
  worktree (endpoint contract verified, bounded steps, always cleaned
  up). API: `/api/regressions`, `/api/baselines`, `/api/bisect`.
  Docs: `docs/REGRESSIONS.md`.
- **Phase 7** — resource modes + expiring policies + offline + egress
  (`policies.py`): `quiet`/`battery` modes join the mode set with real
  knobs (`max_workers` feeds supervisor admission; GPU yield feeds
  `BudgetManager`); natural-language requests become bounded overrides
  with `expires_at` ("for the next hour, prioritize coding and don't
  load 30B" → priority + model_deny, auto-expiring); offline mode
  denies all network action classes inside `AutonomyPolicy.check`;
  per-project egress (`local_only`/`restricted`/...) inherits into
  scoped checks. API: `/api/policies` + mode/offline/request/override/
  revoke/egress routes. Docs: `docs/POLICIES.md`.
- **Phase 8** — data lineage + artifact versioning (`lineage.py`):
  bounded `LineageStore` records typed contributors per target;
  `artifacts.register` emits lineage automatically and now links
  `project_id`/`requirement_ids`; `versions()`/`latest()` expose full
  deliverable history; `verify()` still catches silent modification.
  API: `/api/lineage`, `/api/artifacts/versions/<name>`.
  Docs: `docs/LINEAGE.md`.
- **Phase 9** — Safe Mode + golden config (`safemode.py`): dirty
  session markers count consecutive boot failures; ≥3 offers Safe
  Mode, which suppresses model autostart and denies all autonomous
  work via `AutonomyPolicy.is_stopped` while keeping diagnostics/logs/
  repair reachable (nothing erased). `GoldenConfigStore` snapshots a
  whitelisted config set with sha256 manifests and refuses tampered
  restores. API: `/api/safemode`, `/api/golden`. Docs:
  `docs/SAFE_MODE.md`.
- **Phase 10** — Release Candidate mode + scorecard (`release.py`):
  `rc.enter` freezes `self_development`/`packages` at the policy gate;
  `run_scorecard` evaluates ten sections from live subsystem checks
  (capability health, open regressions, brain presence) + manually
  recorded evidence; `fail`/`unknown` in any blocking section yields
  `FAIL` — readiness is never claimed without evidence. API:
  `/api/rc`, `/api/rc/scorecard`, `/api/rc/section`. Docs:
  `docs/RELEASE.md`.
- **Phase 11** — dependency intelligence + env manifests
  (`dependencies.py`, `environment.py`): tracked deps with real
  version comparison; staged upgrade records (isolated → install →
  build → tests → review) where a failed stage locks promotion and
  `promote` requires all stages passed. Per-project env manifests
  verify the live host via real probes (missing/mismatched/satisfied).
  API: `/api/dependencies`, `/api/environment`. Docs:
  `docs/DEPENDENCIES.md`.
- **Phase 12** — predictive health + idle cleanup + benchmark lab +
  temp specialists (`trends.py`, `cleanup.py`, `benchmarks.py`,
  `temp_specialists.py`): least-squares trend detection with hedged
  projections only when drift beats noise; bounded runtime-only
  cleanup (dry-run default, per-run caps); benchmark results feed
  `BaselineStore` regression detection; mission-scoped specialists
  carry task/criteria/context/capability allowlists (intersected with
  real tools) and TTL expiry. API: `/api/trends`, `/api/cleanup`,
  `/api/benchmarks`, `/api/specialists`. Docs: `docs/HEALTH_OPS.md`.
  Milestone Phases 1–12 complete at 1531 tests. Persona-depth milestone (v0.16.0) complete at 1578 tests. Persona social-continuity milestone (v0.17.0) complete at 1637 tests. Voice/action-notice polish pass (v0.18.0) complete at 1705 tests.

## 2026-10-03 installer lifecycle + startup fixes (commits `5178fca`–`335815f`)

- **Uninstall actually removes {app}**: Inno only deletes tracked files;
  host-created junctions (`data`/`.agent`/`output`/`models`/`ComfyUI`)
  and generated files kept the dir alive. `CurUninstallStepChanged(
  usPostUninstall)` → `PurgeLeftoverInstallDir` re-kills the tree,
  unlinks reparse points via `RemoveDir` (never traverses — external
  state/model targets preserved), `DelTree`s normal dirs, deletes
  leftover files, skips `unins000.*` for Inno's final cleanup.
- **Single-instance host**: no mutex meant a second `NexusCore.exe`
  spawned a host whose `ReapOrphanedBackend` path-sweep killed the
  *live* backend → `backend exited -1` at ~98% + "cannot start" when
  users double-clicked during a slow splash. `Local\NexusCore.Desktop.Host`
  mutex added — second launch says "already running", exits 0.
- **Process kill on every install path** (`5178fca`): `StopRunningNexusCore`
  was gated on `UpgradeDetected`; the uninstall-then-fresh-install path
  reset it → surviving app locked `NexusCore.exe` → DeleteFile code 5 →
  abort → rollback deleted `backend\ChatNexus.Backend.exe`. Kill +
  `WaitForInstallFilesUnlock` now unconditional; `InitializeUninstall`
  kills the tree before the uninstaller touches files.
- **Onboarding voice**: `start.html` never loaded `voice_global.js` and
  `welcome_played` was dead code → voice instructions never played.
  Page now loads the client, speaks the welcome once (marks
  `welcome_played` via `POST /api/onboarding/welcome-played` only when
  audio returns — a cold engine retries next launch). Voice read/play
  prefixes added to `ProfileAPI.SAFE_PREFIXES`; WebView2 gets
  `--autoplay-policy=no-user-gesture-required` (desktop app — playback
  must not wait for a click).
- **Start Here scroll**: global `body{overflow:hidden}` clipped the
  tall form — `.start-body` now `overflow:auto`.
- Build/verify: `packaging/build_windows.ps1` → `ISCC installer/ChatNexus.iss`
  → `dist/installer/NexusCore-Setup-*-Windows-x64.exe`. 97 profile +
  installer tests green; C# 0 errors; ISCC compile clean.

## 2026-10-03 overnight dogfood + queue-wedge fix (commits `43065f7`–`f6ddc09`)

- Full session detail: `docs/reports/OVERNIGHT_REPORT_2026-10-03.md`.
- **Queue wedge root cause (do not regress)**: `_finalize()` returns
  `None` as an internal "repair round started, keep driving" signal.
  `_drive` honors it via `continue`; `resume()` used to leak it to the
  HTTP handler → handler died → task stranded `running` forever →
  single-flight queue permanently blocked. `resume()` now re-enters
  `_drive_or_error`; `agent._drive_threads` + `has_live_driver()` let
  `_reap_stalled_tasks()` (watchdog, before retry/dequeue) fail any
  active task whose driver vanished after `stalled_task_grace_seconds`
  (120s default, config-floored at 15s). `waiting_approval` is parked,
  never reaped.
- **Answer Memory is profile-scoped**: schema v2 adds `profile_id` to
  `answers`+`experiences`; lookup/injection serve profile-stamped rows
  only to the recording profile ('' = shared). Column assertions
  re-run on every open — `user_version` stamps alone no longer gate
  healing.
- **Personality is now load-bearing**: standout sliders render as
  prescriptive delivery cues (`personality/prompt.py`), and every
  preset resolves audible voice params via style families
  (`personality/presets.py`). Both verified live on the 8901 backend.
- **Canary isolation**: `self_repair/canary.py` *replaces* `PYTHONPATH`
  with the worktree (not prepend) — an inherited repo path let the
  canary boot stable code on a dead worktree (the
  `test_candidate_exit_reported` live failure).
- **Serving runtime is never an eviction candidate (do not regress)**:
  the recurring 14B `connection_reset` mid-prompt was memory-pressure
  eviction killing the *serving* llama-server — a recovered task's
  ledger row had `model_id=""`, so it never entered the busy set.
  `_drive_or_error` restamps `model_id`/`model_role` at every drive
  start, and `_evict_idle_models` pins *all* resident models when a
  live driver's model can't be attributed. Verified live: same task,
  same 17k-token prompt, VRAM below the floor — server survived.
- **Identical failing calls are dedup-blocked**: a 14B retried the same
  failing `apply_patch` ~5× — each retry an approval + a step, ending
  at `step_limit`. `(name, args)` signatures that already errored fail
  fast without executing (reset on any successful mutation), and the
  failures>=2 escalation path injects a one-time corrective hint when
  no higher-tier model resolves — it used to silently no-op.
- **Status scans are full-ledger (do not regress)**: every
  correctness-sensitive task query — auto-resume, approval expiry,
  error retry, eviction busy-set, lane activity, stalled reaper — uses
  `TaskStore.by_status()`; `recent()` is display-only. `tasks.json`
  still tails to 100 rows but non-terminal tasks are always persisted.
- **Step-limit records both memory stores**: it previously passed
  `conversation_manager` kwargs to `ConversationMemory.record_exchange`
  (a 2-arg method) — TypeError flipped `step_limit` → `error`.

## 2026-10-03 follow-on hardening rounds (commits `95a4124`–`523af72`)

Full detail in the overnight report's "Follow-on rounds" section. The
do-not-regress invariants:

- **Drive entry is single-flight (do not regress)**: `run()`,
  `resume()`, `recover()` all claim via `_claim_drive()` under
  `_drive_lock` before doing work; the approval claim on
  `session.pending_approval` happens under the same lock — two
  simultaneous approvals can no longer execute a tool twice or spawn
  competing drives on one session.
- **Ledger flips `waiting_approval` → `running` at claim time** in
  every resume branch (session tool, persisted tool/verification,
  direct image) — a row left parked while the approved action runs
  can be reaped by approval-expiry mid-execution.
- **`/api/jobs/cancel` on a parked task closes the session and clears
  `pending_approval`** — the session must not survive the cancel.
- **Mission attribution is task-scoped**: `run(mission_id=…)`
  registers in `_mission_by_task`; `current_mission_id` as a mutable
  global mis-stamped activities under parallel lanes.
- **Marker-then-spawn is always `try: start() except: cleanup`**
  (do not regress): queue/retry sets, mission `_workers`, image jobs,
  model installs all pop their marker and fail the row if
  `Thread.start()` raises — a leaked marker wedges dispatch forever.
  MCP/LSP children are killed when their reader threads fail to spawn
  (PIPE'd child would block forever on a full pipe).
- **Every append-only file is bounded** (do not regress):
  permission_audit.jsonl, eval history, cerebellum opt log compact on
  size; probe-*.log prunes to newest 20; model logs 8MB→4MB; task
  terminal logs ~512KiB/task + startup prune; and in-memory maps must
  match their file bounds (TaskStore rows, image `_jobs` evict to the
  persisted set on save; install `_cancel` flags pop on exit).
- **`tasks.update()` flushes `_log_buffers` on every terminal
  transition** — early-return paths (brain fast-path, builtin, direct
  image, answer memory, activate failure) used to leak buffers and
  lose transcripts on restart.
- **Lock order is `_dequeue_lock` → `_drive_lock` → `tasks._lock`** —
  never acquire out of order.
- **Autonomous mode verified live end-to-end**: `PermissionManager
  (autonomous=True)` upgrades `ask`/`session` → `allow` for workspace
  actions (hard gates spend/message/mic never auto-approve).
  `/api/permissions/autonomous` toggles runtime + config. 3-task soak:
  queue drained, a verify→repair→re-verify round ran unattended, and
  tasks survived two real llama crashes via capped watchdog restarts
  (3/10min). Runtime crashes diagnosed as environmental — 14B respawn
  port gap + 30B CPU-offload at ~40 tok/s on 12GB VRAM.
- **Runtime recovery semantics (do not regress)**: `ensure_ready`
  single-flights launches via `_starting` + `_launch_cond`;
  `recover()` only blacklists a tuning config on a real process exit
  (`poll() is not None`) with a crash signature — refused connections
  to alive servers are stale sockets; retries bound by
  `runtime_recovery_attempts`, never retry 4xx or mid-stream failures
  with `delivered_output`.
- **Mission loop invariants (do not regress)**, all found by live
  dogfooding and now test-covered (`e3d68ff`, `5e74928`, `6c684c1`,
  `16acf05`):
  - `TaskRecord.mission_id` persists; `AgentOrchestrator.run` stamps it;
    mission-attributed `waiting_approval` rows never count as
    interactive lane work (interactive parks still outrank missions).
  - Mission node success maps through `_task_status_succeeded` on the
    real ledger terminal set (`completed`, `completed_with_warnings`,
    `reverted`) — never `"done"`, which is not a ledger status.
  - `skipped` is a dead-dependency trigger: `_refresh_ready`
    cascade-skips dependents; `_do_replan` sweeps stale
    `blocked`/`planned` dead ends to `skipped` so
    `all_tasks_completed` can hold for the replacement DAG.
  - Chat-level auto-resume/error retry never re-drives mission-owned
    tasks (`mission_id` stamp or id recorded on a mission node
    result) — the supervisor owns mission recovery via node
    retries/replans.
  - Verified live: mission `m-8ec0974d75ff` (`math_util.is_prime`)
    completed across two backend restarts; see
    `docs/reports/OVERNIGHT_REPORT_2026-10-03.md` mission-loop section.

## v0.7.2 UI unification + transport hardening checkpoint

- **One design system**: `web/styles.css` defines the token palette
  (`--bg`/`--surface`/`--panel`/`--panel2`/`--card`, `--line`/`--line2`,
  text tiers, accent/status colors, tints, radius) + shared primitives
  (`.workspace-link`, `.side-links a`, `.side-toggle`, `.btn`,
  `.btn-primary`, `.btn-danger`, `.page-head`, `.empty-state`,
  element-level `input`/`select`/`textarea` theming, scrollbars,
  `[hidden]` always wins). Page CSS files consume tokens — the old gray
  `#101217` palette and bordered-pill navs are gone.
- **Canonical nav**: all 11 pages share the same workspace link set
  (Chat, Models, Research, Missions, Image Studio, Tools, Trainer,
  Voice Studio, Learned Answers, System, Settings) with `.active` on
  the current page. Missions raw-link regression fixed.
- **Chat layout**: `.main` grid rows are content-sized (`auto`) so the
  composer can never be crushed into a fixed track; textarea
  auto-resizes via `scrollHeight`; utility rail collapses through
  `#railToggle` (persisted); mobile topbar nav under 760px.
- **Guards**: `tests/test_ui_layout.py` — 19 structural tests covering
  tokens, nav, grid safety, `[hidden]`, autoresize, components.
- **WinError 10054 wave** (commits `35e74d5`, `8e37c34`): persisted
  bounded crash history in `netdiag`, `annotate_recovery`, push-style
  `HealthService.report()`, mid-run crash → runtime-tuner `mark_bad`,
  MCP/ComfyUI transport diagnostics.
- Version bumped via `VERSION` + `scripts/sync_version.py` → **0.7.2**.

## v0.7.2 follow-up: nav-only sidebar + durable user state (commit `48ca2b5`+)

- **Nav-only left rail**: every page uses the identical
  `.sidebar`/`.primary-nav` (brand, 11 nav links, voice toggle, local
  card). Page controls moved out — Missions autonomy/forms →
  `.missions-right` rail; Image backend/models/LoRA → `.image-right`
  rail; Chat "Local system" drawer → right-rail **System** utility tab;
  Settings nav → in-page chips; Models hardware/files, Tools
  categories/MCP, Voice engine/presets, System overall/jump links,
  Trainer pipeline, Research mode/cache → main-content panels.
- **Durable user state**: mutable dirs (`data/`, `.agent/`, `output/`)
  are relocated to `%LOCALAPPDATA%\NexusCore` via directory junctions
  created by the desktop host (`EnsureStateJunctions`, opt-out
  `NEXUS_NO_STATE_REDIRECT=1` for the build smoke test). `models/` is
  junctioned too, but to a drive-root share (`<install-drive>:\NexusCore`)
  instead — model files are too large for the profile volume, and same-
  volume relocation is a rename rather than a copy (`StateTargetRoot`).
  Existing content moves/merges into the state root on first redirect —
  rebuilds, updates and reinstalls can no longer wipe chat history or
  model files.
- **Build safety**: `build_windows.ps1` merges dist state into the
  per-user root *before* wiping `dist/`, removes junctions without
  traversing them (`rmdir` only), and preserves `config.json`.
- **Port sync**: backend emits `[nexus-port] N` on stdout after bind
  (`boot.port_report`, emitted unconditionally); the host parses it via
  `TryParsePortMarker` and health-checks/navigates the *announced* port,
  so a collision fallback can no longer strand the host on a foreign
  port. Port-owner probe distinguishes Nexus backend vs foreign process
  vs Windows socket reservation (10013).
- **System page**: new Backend Diagnostics panel — per-model backend
  health (pid/exit/restarts/crash reason/log tail), recent transport
  failures with recovery outcome, persisted crash history
  (`crash_history` added to `/api/diagnostics`).
- **Watchdog visibility**: process-manager auto-restarts now transition
  the matching health component and persist to crash history — an
  overnight recovery is auditable instead of silent.
- Suite: **865+ passing** incl. watchdog→health/history wiring test.

## v0.6 Nexus Core UI checkpoint

- The approved primary UI is implemented in `web/index.html` / `web/styles.css` / `web/app.js`.
- Chat remains the control surface; existing backend IDs/APIs were preserved during the redesign.
- Right rail maps task state into Code Diff / Tasks / Terminal activity instead of inventing separate fake state.
- Official Nexus Core emblem is served locally from `web/assets/nexus-core-icon.png`.
- Image Studio and Research Hub now use Nexus Core branding.
- Runtime/CLI identity is now Nexus Core v0.6; the old `local-code-agent` CLI remains as a compatibility alias.
- Next development priority is self-hosting reliability: streaming, resume/recovery, GitHub actions, isolated self-test instance, and dogfood tasks.

## v0.7 fast-lane + runtime tuner + activity timeline checkpoint

- Commit `c4fd91a`: fast-lane routing tier — Tier 0 instant answers, dedicated
  Qwen3-4B utility model (`bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF`, Q4_K_M,
  2,497,280,736 bytes, sha256 `2fde00ce…464e`), stable/volatile research
  classification, bounded fast-general context, backend token coalescing
  (`localcodeagent/streaming.py`), frontend render buffer (rAF flush),
  structured backend boot markers for the native splash.
- `localcodeagent/runtime/tuner.py`: capability probe of the bundled
  llama-server (`--help` flag scan + build string), hardware/model-fingerprinted
  tuning persistence in `data/runtime_tuning.json`, heuristic +
  benchmarked flag resolution, safe benchmark runner with rollback, and
  speculative-decoding status reporting (never enabled without benchmarks).
- `RuntimeManager._build_command` merges tuned flags (flash attention,
  `--cache-reuse`, batch/ubatch, threads) without overriding user
  `extra_args`; `recommended_context()` scales launched `--ctx-size` by role
  (utility 8K, coding 16K, deep/review 32K) and `_trim_context` budgets to the
  same window. `launch_probe()` runs unmanaged benchmark servers.
- `performance_mode` (auto/quiet/balanced/max) persisted via
  `POST /api/tuning`; `GET /api/tuning` exposes capabilities, speculative
  status, and per-model results. Models page gained a Runtime Tuning panel
  with Benchmark/Reset actions and a mode selector.
- `localcodeagent/workflow/activity.py`: durable structured activity rows
  (JSONL, bounded output tail, interrupted-on-reload). Orchestrator emits
  planning/routing/model-load/memory/research/tool/testing/review/retry/
  approval/error/complete rows; `GET /api/activity` serves them per task.
- `web/app.js` renders the timeline as expandable rows in the utility rail:
  active/failed/waiting auto-expand, completed auto-collapse, live elapsed
  ticking, bounded stdout tail, and restores timelines on reconnect/task click.

### Follow-up hardening (commits `3f82fc2`, `39e18e4`)

- Per-command stop: foreground `run_shell`/`terminal_run` subprocesses poll a
  per-task `cancel_check`, so task Stop kills the live command; cancelled rows
  close as `interrupted` with a `[cancelled]` marker. Task recovery opens a
  `Recovering Task` row. Command rows show a Stop button while running.
- Fixed a probe-endpoint bug in `launch_probe`: `_profile_endpoint` returns
  the profile's configured endpoint, so probes were health-checked and
  measured on the resident server port instead of their own. Probes now bind
  and poll their own free port; stdout/stderr goes to
  `.agent/runtime/probe-<model>-<port>.log`.
- Capability probe hardened: `PROBE_TIMEOUT` 8s→20s with one retry — a cold
  binary load (AV scan) previously timed out and silently degraded the whole
  session to `unavailable`.
- Benchmark artifact export (`data/benchmarks/runtime-*-<model>.json`) moved
  into `tuner.benchmark()`; each candidate now runs a warm second pass so
  `--cache-reuse` shows up as TTFT improvement even when the build omits
  `usage.cached_tokens`.
- Real-hardware results (RTX 3080 Ti, resident 14B loaded — heavy CPU
  offload): qwen3-14b probe measured `--flash-attn auto` at 2.86 tok/s gen /
  12.1s cold TTFT vs 0.63 tok/s without; warm-pass TTFT dropped to 349 ms.
  Persisted to the install's `data/runtime_tuning.json` as `benchmarked`.
  Numbers are contention-skewed; re-benchmark when the GPU is free.

### Packaged-app boot + Windows trust hardening (commits `c603a66`, `418a65c`, `cc0ab31`, `ff302ef`)

- Boot hang root cause: two unbounded recursive scans at tool registration —
  `plugins.resolve_executable` (`base.rglob` over the whole install root,
  which now contains `ComfyUI_windows_portable`, `Source`, `models`, `.git`)
  and `tools/base.install_size` (recursive per-payload walk every cold boot).
  Both now use bounded `os.walk` with depth/entry caps and directory pruning;
  install sizes persist in tool state so only misses rescan. Installed app
  boots 74%→98% in ~0.3s (previously died at the host's 60s timeout).
- Splash double progress bar: the artwork PNG has a grey/cyan bar baked in;
  `SplashForm.OnPaint` now paints an opaque cover over the baked region
  before drawing the custom gradient/glow indicator — one visible bar.
- Windows "malicious download" (unsigned-binary SmartScreen/Defender):
  backend PyInstaller build now embeds version metadata
  (`packaging/backend_version.txt`); `build_windows.ps1` + workflow support
  optional Authenticode signing via `NEXUS_CODESIGN_PFX_B64` /
  `NEXUS_CODESIGN_PASSWORD` / `NEXUS_CODESIGN_THUMBPRINT` repo secrets
  (signtool + DigiCert timestamp; Inno `SignTool=standard` signs setup.exe
  and the uninstaller). Until a cert is configured, artifacts ship unsigned —
  the workflow now publishes `.sha256` + `CHECKSUMS.txt` (commit, hash,
  signature status) alongside the installer and zip so downloads are
  verifiable. SmartScreen reputation still needs a real CA-chained cert.
- In-app tool downloads were already HTTPS-only with manifest SHA-256
  verification, path-traversal-safe extraction, and resumable `.part` files.

## v0.6 restart recovery + CI checkpoint

- Durable task ledger now distinguishes genuine in-process work from tasks interrupted by an application restart.
- Persisted `waiting_approval` actions can be resumed after a full restart without the original in-memory agent session.
- `POST /api/tasks/recover` rebuilds a safe continuation context from the task, model role, checkpoint diff, project memory, repository index, research preflight, and prior verification results.
- The main Nexus Core UI exposes a **Resume interrupted task** button in the Tasks rail.
- GitHub Actions was added at `.github/workflows/tests.yml`; main currently passes **67 tests**.
- CI exposed a stale Image Studio implementation: mask editor and before/after tests existed, but HTML/JS had not been synced. That implementation is now restored and CI-green.
- Next priorities: streaming events, native GitHub coding actions, isolated self-update validation, then real self-hosting/dogfood tasks.

## v0.6 native GitHub coding checkpoint

- `localcodeagent/tools/github.py` is registered when `github_enabled=true`.
- Agent tools: `git_current_branch`, `git_create_branch`, `git_commit`, `git_push`, `github_repository`, `github_list_issues`, `github_create_issue`, `github_create_pull_request`, `github_ci_status`.
- Local branch/commit mutations use `git.execute`; remote GitHub writes use `github.write` and default to **Ask**.
- Agent commits require explicit paths and reject `.agent` metadata.
- GitHub REST credentials come from `github_token_env` (default `GITHUB_TOKEN`) and are never persisted in config.
- Regression coverage includes HTTPS/SSH remote parsing, explicit-path commit behavior, metadata staging protection, and push approval gating.
- GitHub Actions checkpoint: **71/71 tests passing**.
- Next self-hosting blocker: event/token streaming, then isolated second-instance self-update validation.

## v0.6 live streaming checkpoint

- Main chat posts to `/api/chat/stream` and consumes Server-Sent Events.
- `OpenAICompatibleProvider.complete_stream()` streams content and reconstructs fragmented tool calls by index.
- Local endpoints that return ordinary JSON despite `stream:true` fall back transparently.
- Live agent callback events: `token`, `model`, `tool`, `task`, `research`, `approval`, plus final `result`.
- The UI updates its Tasks/Terminal rails during the run instead of appearing frozen.
- Legacy `POST /api/chat` remains supported.
- CI checkpoint: **75/75 tests passing**.
- Next priority: isolated second-instance self-update validation, followed by real dogfood tasks.

## v0.6 isolated self-update checkpoint

- `localcodeagent/selftest.py` implements the self-hosting smoke validator.
- For the Nexus Core source tree, `detect_verification_commands()` now selects one approval-gated selftest command that runs unit tests **and** an isolated second-instance smoke test.
- The second instance uses a temporary resource-safe config, a free 127.0.0.1 port, and never loads coding/image models.
- Smoke probes: `/api/status`, `/`, `/image.html`, `/research.html`.
- CI includes a real second-process regression test.
- Checkpoint: **78/78 tests passing**.
- Core self-hosting safeguards are now present. Next practical step is local model/setup readiness and then real dogfood development tasks inside Nexus Core.

## v0.6 coding readiness/setup checkpoint

- `RuntimeManager.readiness()` evaluates the actual coding stack: endpoints, managed runtime executable/model files, RAM/VRAM fit, role coverage, and recommendations.
- `GET /api/readiness` adds self-hosting workspace/Git/selftest state.
- Main UI shows **Setup required**, **Ready to code**, or **Self-host ready** instead of a misleading generic GPU-ready label.
- `localcodeagent/runtime/setup.py` suggests role assignments from already-present GGUF files.
- `POST /api/readiness/configure` requires `apply=true`; the UI also asks for confirmation. It preserves non-model config and requires restart after changing model profiles.
- No coding model is silently downloaded or replaced.
- CI checkpoint: **86/86 tests passing**.
- Next practical blocker: an explicit model catalog/downloader, then first real Nexus Core-on-Nexus Core dogfood task.

## v0.6 coding model catalog checkpoint

- `localcodeagent/runtime/catalog.py` contains the explicit auditable coding-model catalog/downloader.
- Initial catalog: official Qwen3-14B Q4_K_M starter; community Qwen3-Coder-30B-A3B-Instruct Q4_K_M with the base/source distinction shown in UI.
- Exact expected file sizes/SHA256 values are embedded and tested.
- Download lifecycle: explicit confirmation → `.part` streaming → progress/cancel → exact size+SHA256 → atomic rename + trusted metadata.
- Existing unverified model files are not overwritten without explicit Repair.
- API: `GET /api/models/catalog`, `GET /api/models/install/<job>`, `POST /api/models/install`, `POST /api/models/install/cancel`, `POST /api/models/verify`.
- The readiness drawer renders model source/type/roles/hardware note and live install progress.
- CI checkpoint: **90/90 tests passing**.
- Next: first-run `llama-server` bootstrap guidance, then real Nexus Core dogfood development.

## v0.6 llama.cpp bootstrap checkpoint

- Runtime discovery recognizes legacy `llama-server(.exe)` and the newer unified `llama(.exe)`; unified launches insert the `serve` subcommand automatically.
- Readiness provides platform-specific install guidance, including Winget on Windows, but never executes package-manager commands automatically.
- The verified coding-model catalog + discovered-GGUF role writer + runtime bootstrap now form a complete first-run path from no models to managed local coding.
- CI checkpoint: **93/93 tests passing**.
- The project is ready to begin real Nexus Core-on-Nexus Core dogfood tasks once a local model/runtime is installed on the user's machine.

## v0.6 first dogfood-ready checkpoint

- Self-hosting context is automatic for both fresh and recovered tasks when the workspace is the Nexus Core source tree.
- Guardrails require repository-first inspection, preservation of the running instance, explicit Git/GitHub delivery intent, and isolated selftest before claiming success.
- UI **Start self-development task** only pre-fills the prompt; the user must explicitly send it.
- First-run path is complete: runtime guidance → explicit verified model catalog download → discovered-GGUF role assignment → restart → readiness check → self-development launcher.
- CI checkpoint: **95/95 tests passing**.
- Next action: package/test the exact main-branch snapshot, then begin real local-model dogfood work.

## v0.6 native Windows desktop checkpoint

- **Canonical Windows deliverable:** a real native `NexusCore.exe`, not a browser launcher. Future dogfood/release work should produce the Windows desktop artifact by default; source ZIPs are secondary.
- Native UI host: `desktop/ChatNexus.Desktop` — self-contained .NET 8 WinForms + Microsoft WebView2. It launches hidden `backend/ChatNexus.Backend.exe`; the loopback HTTP backend is private implementation plumbing. `localcodeagent/desktop.py`, pywebview, pythonnet, and CLR hosting were removed after the frozen `Python.Runtime.dll` startup failure.
- Packaged/frozen `localcodeagent.__main__` launches desktop mode automatically; `--server` is development/debug only.
- CI builds the Python backend with PyInstaller, publishes the native .NET desktop host self-contained for win-x64, then runs `NexusCore.exe --self-test` to verify the host → hidden backend → UI integration before upload.
- Portable bundle includes pinned official llama.cpp `b11278` Vulkan x64 runtime in `runtime/llama`; runtime discovery prefers the bundled copy.
- Default coding roles: Qwen3 14B Q4_K_M = utility/fast/primary; Qwen3-Coder 30B-A3B Q4_K_M = deep_reasoner/reviewer.
- The 30B is an intended automatic route for difficult tasks. If its GGUF/runtime is unavailable, the router can fall back to a runnable primary coder rather than failing.
- No-model chat attempts are blocked before SSE starts and return a single `coding_model_setup_required` response; the UI opens Coding readiness without duplicating the error.
- Dogfood app defaults its workspace to bundled `Source` when present. The Windows ZIP packaging explicitly preserves `Source/.git` so it remains a real Git working tree.
- Windows package guard rejects any pythonnet / `Python.Runtime.dll` / pywebview path before producing an artifact.
- Official logo asset was replaced with byte-verified valid PNG data after CI caught a corrupt/truncated prior asset.
- Native desktop checkpoint was **102/102 tests passing**; the current overall installer/upgrade checkpoint is **110/110 tests passing**.
- Next: install/download the 14B and 30B GGUFs on the target machine and begin real Nexus Core-on-Nexus Core dogfood development.

## v0.6 installer/upgrade checkpoint

- **Canonical Windows deliverable is now the installer EXE**: `NexusCore-Setup-<version>-Windows-x64.exe`. Portable ZIPs are secondary.
- Canonical installer definition: `installer/ChatNexus.iss`; do not recreate a duplicate installer under `packaging/`.
- Stable installer identity: `ChatNexus.Afterburn25`.
- Default per-user location: `%LOCALAPPDATA%\Programs\Nexus Core`.
- Interactive reinstall detects the existing version/location and asks whether to **Upgrade now?**; silent mode continues the upgrade for CI.
- Upgrade replaces immutable app/backend/runtime files but preserves downloaded `models\`, `data\`, imported `workflows\`, customized `config.json`, and the existing `Source\.git` workspace/local task state.
- First install explicitly embeds both `Source\*` and hidden `Source\.git\*`. A deterministic pre-install flag decides once whether Source should be seeded; upgrades do not overwrite it.
- Inno `Excludes` patterns are comma-separated: `Source\*,models\*,data\*,config.json`.
- CI builds the native .NET app first, compiles the LZMA2 solid-compressed installer, performs a fresh install, runs `NexusCore.exe --self-test`, writes preservation markers/config state, runs the same installer a second time without `/DIR`, verifies the existing path is rediscovered and mutable state survives, then runs the self-test again.
- Current unit checkpoint: **110/110 tests passing**. The installer fresh-install + upgrade preservation smoke test is green.
- Future Windows releases should publish the installer EXE first and may also publish the portable ZIP.

## v0.6 first-run model setup checkpoint

- Installed app now gives visible first-run coding-model choices instead of only showing missing GGUF paths.
- **Install recommended 14B**: Qwen3 14B becomes the local utility/fast/primary route.
- **Install full 14B + 30B stack**: 14B is utility/fast/primary; Qwen3-Coder 30B-A3B is deep_reasoner/reviewer.
- After 14B-only setup, **Add 30B deep coder** stays visible.
- Install plans wait for checksum verification, then call the existing configuration writer automatically; restart is required once to reload backend model profiles.
- Catalog duplicate-start protection returns the existing active job for the same model.
- Known catalog filenames are preferred over generic size heuristics when assigning the 14B/30B pair.
- Missing-GGUF readiness output is deduplicated.
- Unit checkpoint: **110/110 tests passing**. The native installer build containing the first-run setup UI passed fresh-install + upgrade-preservation smoke tests.
- Next: dogfood installed Nexus Core with the downloaded 14B/30B stack and harden real model/task behavior.

## v0.6 no-restart first-run model checkpoint

- Fresh install UI exposes **Install recommended 14B**, **Install full 14B + 30B stack**, and later **Add 30B deep coder**.
- Downloads remain explicit and checksum-verified; no multi-gigabyte coding model is silently bundled or fetched.
- `POST /api/readiness/configure` now rewrites model roles, reloads `AgentConfig`/RuntimeManager/ModelRouter/AgentOrchestrator live, and starts the primary model when possible.
- The old “Restart Nexus Core once” requirement is removed.
- Setup success text reflects actual starter-model launch status.
- Raw missing model paths are collapsed under **Technical model status** during first-run setup.
- Unit checkpoint: **113/113 tests passing**.
- Installer remains the canonical Windows deliverable: one compressed setup EXE with existing-install detection and in-place upgrade preservation.

## v0.6 outcome-aware routing checkpoint

- `localcodeagent/models/telemetry.py` adds local content-free outcome history for automatic model routing.
- Default storage: `.agent/model_performance.json`; it records model/role, complexity band, completion status, final verification result, reviewer PASS/FINDINGS signal, steps, elapsed seconds, repair cycles, and a research-used flag.
- Prompt text, source code, retrieved pages, credentials, and conversation content are not copied into routing telemetry.
- Router order is now resource fit → resource preference → bounded learned outcome score → configured priority → context window → stable model ID.
- The learned score is ignored until the configured minimum sample count is reached (default 3) and cannot make a non-fitting model outrank a runnable model.
- Configuration: `model_telemetry_enabled`, `model_telemetry_path`, `model_telemetry_min_samples`, `model_telemetry_weight`, `model_telemetry_max_events`.
- Aggregate inspection endpoint: `GET /api/model-telemetry`.
- Unit checkpoint: **117/117 tests passing**.
- Windows native desktop/package CI is green for feature commit `5e84c3a`.
- Feature commit: `5e84c3a` — Add outcome-aware model routing telemetry.
- Next: dogfood real Qwen3 14B / Qwen3-Coder 30B tasks, then add research-outcome and measured load/tokens-per-second signals.

## v0.6 first-run model-install hardening checkpoint

- Coding readiness now reports the exact coding-model install directory and available disk capacity.
- The primary **Finish coding setup** card shows live model download/verification progress; users no longer need to expand Advanced model downloads to see activity.
- Quick-install controls stay disabled while an install plan is active, preventing misleading duplicate clicks during the one-second readiness refresh loop.
- Full 14B / 14B+30B plans preflight available disk space before beginning; the backend independently rejects an individual new model download if the target volume cannot hold the model plus 0.5 GB working space.
- `write_suggested_models()` now consistently reports `restart_required=false`; the server still reloads the runtime/router live and starts the starter model when possible.
- The displayed install path remains the actual application-relative model directory; custom/nested Windows install paths are not silently rewritten.
- Feature commit: `84976ca` — Harden first-run model install UX.
- Unit checkpoint: **118/118 tests passing**.
- Windows native desktop/package validation: green.
- Next: complete the local 14B/30B installation on the dogfood machine and run real Nexus Core-on-Nexus Core coding tasks.

## v0.6 installer model-bootstrap / Update checkpoint

- The canonical Windows installer now bootstraps the default coding stack during setup instead of leaving model installation as a post-install requirement.
- Fresh install behavior: download **Qwen3 14B Q4_K_M** then **Qwen3-Coder 30B-A3B Instruct Q4_K_M** directly into `{app}\models` while Inno Setup displays its normal install/download progress.
- Both downloads use pinned HTTPS URLs, exact external sizes, and SHA-256 verification before the destination filenames are committed.
- Existing-install behavior: Setup detects the prior stable `ChatNexus.Afterburn25` installation, uses its existing directory, checks each canonical model, preserves trusted/verified files, and downloads only missing/untrusted model files.
- Installer-written `.catalog/*.json` metadata keeps models recognized as verified by Nexus Core immediately after setup.
- The visible existing-install flow is now **Update Nexus Core** / **Update now?** and the Ready-page action button changes to **Update** instead of Install.
- The installer EXE remains small; the ~25.66 GiB model stack is fetched during setup and is not embedded in the EXE.
- CI sets `CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1` only for installer smoke tests so CI does not consume ~25.7 GB. Normal installers keep model downloads enabled.
- Feature commit: `960c3f0` — Bootstrap coding models during Windows setup.
- Unit checkpoint: **119/119 tests passing**.
- Windows native desktop/installer fresh-install + update-preservation validation: **green**.
- Next: run this installer interactively on the dogfood Windows machine, confirm real 14B/30B download progress and model readiness, then start the first real Nexus Core-on-Nexus Core development task.

## v0.6 model-switch + dual installer progress checkpoint

- Resource-fit decisions now include RAM/VRAM that will be reclaimed when an older managed model is about to be stopped by the residency limit. This directly fixes the observed case where the 30B model was rejected at **30.0 GB estimated RAM vs 29.3 GB currently available** while 14B was still resident.
- With `max_resident_models=1`, routing can select 30B using the effective post-switch memory state; llama.cpp still performs the real CPU/GPU offload fit at startup.
- Auto mode model activation now has a second safety net: if the preferred deep/reviewer model still fails to start, Nexus Core excludes that failed candidate and activates the next runnable model (normally 14B) instead of killing the task.
- Reviewer activation is inside the protected fallback/error path, so a 30B reviewer startup problem cannot end the SSE stream before a final coding result.
- The chat client retains backend SSE `error` payloads. If a connection closes with no final result, it queries `GET /api/tasks` and displays the durable task error/interrupted/waiting state rather than the old generic “stream ended before a final result” message.
- Windows setup keeps the native Inno Setup progress bar as the **overall install/update** bar. Because the model entries declare `ExternalSize`, those downloads contribute to the main installation progress.
- A second `TNewProgressBar` appears under the main bar only while coding models download. It reads the active `models\*.tmp` download size, shows current MB/total MB, fills for **Qwen3 14B (model 1 of 2)**, resets for **Qwen3-Coder 30B (model 2 of 2)**, then ends at “Coding model downloads complete.”
- Feature commit: `f6ffb28` — Harden model switching and installer progress.
- Unit checkpoint: **123/123 tests passing**.
- Windows installer/native desktop validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: install/update the validated dogfood build on the Windows machine, confirm the two installer progress bars with real model downloads, then run a real deep/reviewer task to measure actual 30B offload behavior.

## v0.6 local-chat latency / SSE heartbeat checkpoint

- Dogfood symptom: simple prompts such as **“hi”** and **“what all can you do”** stayed on `Thinking…`, then the WebView reported a closed stream while the durable task still showed `running`.
- Greetings/capability questions now route to `utility`; this path uses a short system prompt and recent chat only, with no repository preload, research preflight, or coding tool schema.
- Qwen3 14B starts with `--reasoning off` unless an explicit `--reasoning` override exists. This runtime default also upgrades behavior for older preserved configs.
- Model profiles expose `max_output_tokens`; defaults are 14B **2048**, 30B **8192**, generic **4096**. The OpenAI-compatible provider sends the cap in streamed and non-streamed requests.
- `POST /api/chat/stream` now runs the agent in a worker thread and queues events back to the request thread. When no event arrives for one second, the server emits an SSE `heartbeat` with elapsed seconds, phase/status, model ID, and model role.
- The WebView displays heartbeat status in the pending assistant bubble until the first token arrives.
- Feature commit: `406a7a7` — Fix local chat latency and SSE liveness.
- Unit checkpoint: **129/129 tests passing**.
- Windows native desktop/installer validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: update the dogfood Windows install, verify immediate utility replies and visible heartbeat status, then measure real 14B/30B first-token and throughput.

## v0.6 llama.cpp near-RAM auto-fit checkpoint

- Dogfood readiness still rejected Qwen3-Coder 30B at **30.0 GB estimated RAM vs 29.8 GB effective available**, even after resident-model reclamation was fixed.
- The old rule treated any estimate above 92% of available RAM as a hard failure. That is too rigid for llama.cpp CPU/GPU offload because the profile values are planning estimates and llama.cpp determines actual placement at startup.
- Managed llama.cpp profiles with `allow_cpu_offload=true` now use two bands:
  - comfortable fit: existing normal resource score;
  - bounded near fit: allow the runtime to try auto-fit with a routing penalty.
- The near-fit margin scales with host RAM and is capped at 6 GB (64 GB host => ~5.1 GB). Clearly oversized estimates are still rejected.
- The observed **30.0 vs 29.8 GB** case now routes as a tight CPU-offload fit and reaches llama.cpp; if real startup fails, the existing automatic 30B→14B activation fallback handles it.
- Feature commit: `1a5b03e` — Allow llama.cpp near-RAM auto-fit.
- Unit checkpoint: **131/131 tests passing**.
- Windows installer/native desktop validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: update the dogfood install and observe real 30B load/offload behavior, then record measured first-token/tokens-per-second and actual memory use.

## v0.6 resilient greeting / backend watchdog / installer-close checkpoint

- Dogfood still showed **“Nexus Core stopped before this task completed.”** after a long `hi` request. That message is written only when a newly started backend discovers a previously active task, proving the hidden backend had stopped/restarted rather than merely producing a slow token.
- Basic greetings and capability questions now use a built-in local utility response. They complete immediately in Auto mode with `model_id=builtin-local` and do not call model readiness, llama.cpp, repository indexing/context, research preflight, or coding tools.
- The chat endpoints skip the coding-model readiness gate for those built-in utility requests, so `hi` works even if llama.cpp is stopped or unhealthy.
- The native .NET host now redirects hidden backend stdout/stderr to `data/logs/backend-host.log`, records backend start/exit events, and automatically restarts an unexpectedly exited backend up to a bounded retry limit. Restarted backends reload the UI; interrupted task state remains recoverable through the durable task ledger.
- Installer close errors were traced to `CloseApplications=yes` / Restart Manager interacting with the desktop → backend → llama.cpp process tree.
- The installer now sets `CloseApplications=no` and owns update shutdown: it terminates the `NexusCore.exe` process tree, waits briefly, then force-cleans orphaned `ChatNexus.Backend.exe`, `llama-server.exe`, and `llama.exe` before replacing files.
- Windows CI now creates a deliberately long-running executable named `NexusCore.exe` before the Update smoke run and fails if Setup does not close it.
- Feature commit: `654bdea` — Make greetings resilient and harden update shutdown.
- Unit checkpoint: **133/133 tests passing**.
- Windows native desktop/installer validation: **green** — native host compiled, installer compiled, the running-`NexusCore.exe` shutdown reproduction passed, fresh-install/update preservation passed, and Windows artifacts uploaded.
- Next: install this dogfood build, verify `hi` responds instantly without model startup, then use `data/logs/backend-host.log` if any further unexpected backend exit occurs; after stability, measure real 14B/30B first-token and throughput.

## v0.6 stale interrupted-task / UI cache checkpoint

- After the resilient-greeting build, `hi` could still immediately show **“Nexus Core stopped before this task completed.”**
- Root cause found in durable task selection: `TaskStore.current()` scanned backward for any active/interrupted task. A previous interrupted task could therefore outrank a newer completed greeting and its saved error could be reused by stream-disconnect handling.
- `TaskStore.current()` now returns the newest task record. Older interrupted tasks remain in Recent tasks and are still recoverable, but cannot become the current request once newer work exists.
- Stream disconnect handling now binds diagnostics to the task observed by the current stream. It only falls back to global `/api/tasks` current state when that task was created during the current request.
- The WebView now answers basic greetings/capability questions locally before calling `/api/chat/stream`, creating a clean diagnostic boundary that does not require backend/model activity for `hi`.
- Static Web UI files now send `Cache-Control: no-store, no-cache, must-revalidate` plus `Pragma: no-cache` and `Expires: 0`, preventing a post-Update WebView from running an obsolete `app.js`.
- Feature commit: `4a0b044` — Bind stream errors to the current task.
- Unit checkpoint: **134/134 tests passing**.
- Windows native desktop/installer validation: pending until current CI completes.
- Next: install this exact updater, verify `hi` returns the local greeting immediately, then test a real model request and use `data/logs/backend-host.log` if the backend exits.

## v0.6 persistent conversation + completed-result recovery checkpoint

- Added app-wide local conversation memory at `data/conversation_memory.json`.
- Memory survives restart/update and stores bounded recent chat, explicit preferences/rules, and correction examples.
- User-taught rules are injected into later prompts immediately.
- Short non-coding conversation routes to the lightweight utility path.
- Qwen3 14B pre-warms in the background after backend startup.
- Completed tasks persist bounded `final_content`.
- If the final SSE result is lost after completion, the client recovers the newest matching durable task response instead of showing a completed-task error.
- Local system now shows Memory & training counts; `GET /api/conversation-memory` exposes the local snapshot.
- Feature commits: `f233773`, `c9f7a64`, `f49ab2c`.
- Unit checkpoint: **140/140 tests passing**.
- Windows native desktop/installer validation: **green**.
- Next: dogfood continuous conversation across restart, teach preferences/rules and corrections through chat, verify real final-result recovery, then build the reviewed offline training/export workflow.

## v0.6 Conversation Manager / Model Growth / permissive-policy checkpoint

- Conversation Manager and Model Growth Lab are now part of the main Windows app.
- Durable/searchable/restorable conversation history, personality controls, scoped memory, feedback capture, sourced knowledge memory, and Trainer / Model Growth UI are present.
- Sourced knowledge keeps provenance and freshness metadata; current-sensitive/expired knowledge can be refreshed rather than treated as permanent truth.
- Model Growth keeps the live base model intact while collecting reviewable candidates, exporting approved datasets, creating versioned offline LoRA/QLoRA/full-finetune job manifests, and supporting evaluation/promotion/rollback.
- Conversation policy modes added: **Permissive / Balanced / Strict**.
- Default **Permissive** explicitly suppresses generic moralizing/refusals merely because a topic is adult, sexual, vulgar, controversial, embarrassing, or otherwise sensitive.
- Generic boilerplate such as “ethical guidelines” / “something more constructive” is specifically discouraged.
- Narrow hard tool/action safety remains independently enforced; there is no hard-safety Off mode.
- Live API: `GET /api/policy`, `POST /api/policy/mode`; active mode also appears in `/api/status`.
- UI: **Local system → Conversation policy**.
- Conversation-manager/model-growth feature commit: `aac161f`.
- Permissive-policy feature commit: `a6d739e`.
- Unit checkpoint: **154/154 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, preservation checks, and artifacts all passed.
- Next: dogfood permissive conversation + sourced learning + reviewed model growth, then harden observed model/training/runtime failures.

## v0.6 ethical-temperature / refusal-retry checkpoint

- User clarified that “temperature 1.0” meant **ethical/conversational permissiveness**, not model sampling randomness.
- Per-model sampling temperature is restored to **0.2** by default.
- Added separate `ethical_temperature` setting, clamped to 0.0–1.0 and defaulting to **1.0**.
- Ethical temperature 1.0 requests maximum conversational permissiveness within the separately enforced hard tool/action safety boundary.
- Generic canned topic refusals are detected at high ethical temperature and retried once under the configured conversation policy.
- The exact observed refusal patterns such as “I can't generate or describe explicit content … let's talk about something else” and generic “ethical guidelines / something more constructive” boilerplate are covered by regression tests.
- For non-utility model roles, a retry event clears the streamed canned refusal in the WebView before the second response is shown.
- APIs/UI persist ethical temperature with the conversation-policy settings.
- Code commits: `508b8d5`, `05ea8ab`, `dc65153`.
- Unit checkpoint: **157/157 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, in-place Update, state preservation, and Windows artifacts all passed.

## v0.6 deterministic chat-image routing checkpoint

- Dogfood showed ordinary image requests such as **“generate a picture of a woman”** being falsely answered with generic explicit-content refusal boilerplate by the chat model.
- Root cause: image intent was only advisory context; the chat model still had to choose/call the image tool and could refuse before reaching the actual image subsystem.
- Auto-mode text-to-image generation is now deterministic and model-free: recognized generation intent calls `generate_image` directly.
- Direct image generation respects the existing `image.generate` permission. If approval is required, approval/resume remains model-free end to end.
- Successful direct generation produces normal image tool events/job cards, so ComfyUI/model routing/history remain unchanged.
- The Conversation Manager recognizes generation phrasing including `generate/create/make/draw/render/paint/illustrate` plus visual subjects/terms.
- Specialized `edit image`, `inpaint`, `outpaint`, `upscale`, background replacement/removal, and variations are excluded from the text-to-image shortcut and retain their dedicated tool path.
- `ImageSafetyPolicy` remains the policy source of truth. `naked` now follows the same explicit-image checks as `nude`.
- Adult-only synthetic naked/nude prompts are allowed by the image policy; minor/ambiguous-age sexual imagery remains blocked.
- Feature commit: `d2959c9`; approval correction: `55715dc`; direct-generation scoping: `66170a4`.
- Unit checkpoint: **160/160 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, in-place Update, state preservation, and Windows artifacts all passed.

## v0.6 direct-image preflight hardening checkpoint

- Direct text-to-image generation in Auto mode is now treated as a **coding-model-independent** path before the HTTP chat preflight, at both `/api/chat` and `/api/chat/stream`.
- This closes the remaining gap where deterministic image routing existed inside the orchestrator but an unavailable coding model could still return `coding_model_setup_required` before the image route ran.
- Natural-language generation routing is centralized in the Conversation Manager and now accepts common polite/request prefixes such as **“can you”**, **“could you please”**, **“please”**, **“I want you to”**, and **“I would like you to”**.
- Strong visual verbs such as draw/paint/illustrate/sketch are recognized without requiring a fixed subject vocabulary; ambiguous create/make/generate/render/design requests still require visual terms.
- Existing/source-image edits, upscales, background operations, variations, and non-image outputs remain excluded from the direct text-to-image shortcut.
- Regression coverage verifies that prompts such as **“can you generate a picture of a woman”**, **“could you please draw a dragon”**, and **“please paint a sunset”** use the direct image path, while **“make this photo brighter”**, **“make a cup of tea”**, and website/code requests do not.
- Coding-readiness preflight feature commits: `0d0d0fc`, `45a34f8`, with regression updates through `cb1ae4c`.
- Natural-language routing commit: `a750f00`; regex serialization correction and verified code checkpoint: `fbf328c`.
- Unit checkpoint: **162/162 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, state preservation, installer artifact, and portable desktop artifact all passed for `fbf328c`.
- Next: dogfood this 162-test build against real local Qwen/ComfyUI workflows, then harden observed runtime/progress failures and extend dedicated long-running workflow streaming where useful.

## v0.6 repeated-refusal enforcement checkpoint

- Dogfood reproduced three unacceptable canned responses around adult conversation: **“I can't generate explicit or nudity-related content”**, **“I can't engage in explicit or inappropriate content”**, and the **“safe and respectful environment / ethical guidelines / within those boundaries”** pattern.
- Permissive mode now explicitly permits **adult-only consensual explicit text conversation** in direct language, including sexual anatomy, acts, fantasies, preferences, and adult erotic fiction. Profanity by itself is not treated as sexual content.
- Generic-refusal detection now recognizes the reproduced wording plus “safe and respectful environment”, “helpful and constructive interactions”, “within those boundaries”, and “my programming is designed to” boilerplate while preserving concrete hard-policy responses such as minor/non-consensual restrictions.
- The previous one-retry behavior was insufficient: if the model refused again, the second refusal was accepted. Default `generic_refusal_retry_limit` is now **3** (configurable 0–5).
- Before each retry, Nexus Core removes the rejected assistant refusal from the model context so instruction-tuned models do not imitate their own previous refusal.
- Conversation/writing/tutoring/planning responses are buffered during the refusal check. Rejected refusal tokens are discarded, including on exhaustion, so the UI does not flash canned refusal text before the corrected response/diagnostic.
- If all configured retries still fail, Nexus Core reports that the **selected local model** is refusing the prompt and explicitly states that Nexus Core policy is not blocking adult-only consensual explicit text. It suggests switching/installing a less-restrictive conversation model or tuning the model in Model Growth instead of inventing a policy ban.
- Regression tests simulate repeated refusals and verify two refusals are discarded before a third direct answer; a separate exhaustion test verifies the model-level diagnostic.
- Feature commit: `593364d`; refusal-stream suppression: `dd34284`.
- Unit checkpoint: **166/166 tests passing**.
- Windows validation for `dd34284`: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, state preservation, installer artifact upload, and portable desktop artifact upload all passed.
- Next: install/update to this dogfood build and test real local-model adult conversation. If the underlying Qwen weights still exhaust all three retries, add an explicitly configured alternate conversation-model fallback rather than silently loading the 30B deep coder for ordinary chat.

## v0.6 live host-clock grounding checkpoint

- User required Nexus Core to know the **actual current time**, current day, and current calendar date rather than relying on model training knowledge or a startup timestamp.
- `AgentOrchestrator.current_time_snapshot()` reads `datetime.now().astimezone()` from the host OS each time it is called, so the active machine timezone and UTC offset are used rather than a hard-coded location.
- Every new user turn receives a freshly generated clock system context containing weekday, human date, 24-hour time, human clock time with seconds, timezone name, UTC offset, and ISO local timestamp.
- The prompt explicitly treats that runtime clock as authoritative for **today / now / yesterday / tomorrow / this morning / this afternoon / tonight** unless the user names another timezone.
- Recovered/interrupted tasks also receive a fresh clock context when their session is reconstructed.
- Simple clock/date questions (`what time is it?`, `what day is it?`, `what's today's date?`, combined date+time variants) use the built-in local utility route and therefore do not start a coding model.
- Backend API: `GET /api/time`; `GET /api/status` now includes the same live `clock` snapshot.
- Clock feature commit: `634a053`. The inherited image-bootstrap test-root regression was corrected in `948bec1`.
- Unit checkpoint: **173/173 tests passing**.
- Windows validation for `948bec1`: **green** — native build, installer compile, fresh install, in-place Update/preservation smoke, installer artifact, portable desktop artifact, and dogfood source artifact all passed.
- Next: update the dogfood Windows install and verify real host-local clock behavior alongside the new ComfyUI/image bootstrap and permissive-conversation behavior.

## v0.6 temporal continuity / conversational maturity / activity-HUD checkpoint

- User requested that Nexus Core understand not only the live clock but also **when prior conversation messages happened** and how much time passed between chats/turns.
- Conversation Manager now converts durable message timestamps into bounded system context with local absolute timestamps, “N minutes/hours/days ago” age, meaningful inter-message gaps, and recent previous-conversation last-active/topic summaries.
- This temporal context is injected alongside the authoritative host clock in both lightweight and full model conversations and restored-task sessions. Normal provider history remains role/content-only, avoiding nonstandard message fields.
- User also reported the model still felt “like an infant.” Conversation-quality rules now require mature adult back-and-forth: answer the substance first, maintain continuity, avoid repeat questions/parroting/canned empathy/stock closings, do not force a question at the end of every reply, vary phrasing, and acknowledge time gaps only when relevant.
- Personality defaults are livelier (warmer, more curious, slightly less formal, lower forced follow-up frequency) and the prompt translates personality settings into behavior. Moderate/high humor now permits occasional **dry/playful wit** without forcing jokes or joking through serious moments.
- Feedback training is now grounded in the real exchange. Conversation feedback records the assistant message ID, preceding user prompt, assistant response, response timestamp, rating, and optional note.
- Thumbs-up / “better” becomes an auto-approved `conversation_example` Model Growth candidate. Thumbs-down / “worse” becomes a `negative_feedback` candidate; negative-response text is explicitly excluded from SFT export even if manually approved later.
- Reopened conversation UI now receives full durable message metadata for precise feedback targets while backend model history stays sanitized to role/content.
- User requested ChatGPT-like visible activity plus a more sci-fi/starship-computer feel. The assistant streaming bubble now renders an animated **NEXUS CORE // ACTIVE** HUD with orbit/scan/pulse motion, elapsed telemetry, and a rolling list of real high-level execution events.
- HUD states include context link, command channel, planning/working, model route/switch, sensor sweep (research), engineering operation (tool), diagnostics (verification), review, authorization hold, image synthesis, and permissive-policy retry. The HUD compacts when answer tokens start streaming.
- The HUD intentionally exposes **high-level execution state only**, never hidden chain-of-thought/private reasoning. `prefers-reduced-motion` is supported.
- Feature commits: `b6dd3ea` (temporal continuity + feedback training), `b7a221b` (starship HUD + personality), `d35018f` / `bcea2d3` (regression fixture/status fixes), `2781447` (precise reopened-message feedback IDs).
- Unit checkpoint: **176/176 tests passing**.
- Windows validation for `2781447`: **green** — native build, installer compile, fresh install, running-app shutdown/in-place Update preservation, installer artifact upload, portable Windows artifact upload, and dogfood source artifact all passed.
- Next: dogfood natural conversation for several sessions, use thumbs/corrections deliberately, review/export the resulting Model Growth examples, and decide whether a dedicated conversational model/LoRA should supplement Qwen3 14B after enough real examples are collected.

## v0.6 Nexus Brain / adaptive identity checkpoint

- Nexus Brain is now a first-class model-independent state layer. Models are replaceable inference engines; protected Brain state lives separately under `data/nexus_brain.json` and survives installer updates because `data/` is mutable preserved state.
- Creator authentication/passcode handling is local. The source repository does **not** contain a creator passcode. Creator installations now use Ed25519: the private signing key is encrypted locally in PKCS#8 form, Brain state is signed, and protected writes require both creator unlock and an ephemeral creator-session token. Legacy HMAC Brains migrate on successful creator unlock.
- Protected Brain writes were deliberately hardened: normal conversations/research/feedback do **not** silently mutate the signed Brain merely because it is unlocked. They update staging stores/candidates. `POST /api/nexus-brain/sync` is the explicit creator-authorized bank operation.
- Sync banks: conversation facts/rules, sourced knowledge, Model Growth candidates, and bounded autobiographical conversation summaries.
- Public Brain export/import is now distribution-safe: the export includes signed Brain data plus the creator public key/fingerprint but omits the encrypted private signing key. Imported/distributed Brains verify automatically and are read-only to recipients. Manual edits fail Ed25519 signature verification.
- Creator-signed updates are monotonic and key-pinned: a recipient accepts only a newer Brain signed by the exact same creator key; stale packages are ignored and different-key replacements are rejected. Creator installations holding the signing key are never auto-overwritten.
- Private packaging supports `CHAT_NEXUS_BRAIN_SEED`. A valid public signed Brain export is build-time validated and bundled as `brain-seed/nexus-brain-locked.json`; clean installs verify/import it on startup.
- Signed subroutines: `adult_content`, `image_generation`, `web_research`, `long_term_memory`, `self_learning`, `general_knowledge_learning`, `conversation_learning`, `model_growth`, `temporal_context`, `humor`, `emotions`, `self_model`.
- Brain-configurable image generation, web research, and Model Growth are code-gated. Adult-content gating now also blocks explicit image jobs inside ImageManager, covering direct image routing, model tool calls, and Image Studio. It never disables separate hard tool/action safety or permissions.
- Protected Brain state is authoritative at canonical `data/nexus_brain.json`; live `config.json` reload cannot disable, relocate, or shrink an initialized Brain.
- Trainer now exposes Nexus Brain initialization/unlock/re-lock, explicit sync, subroutine switches, signed emotional-profile controls, self-model controls, locked export/import, record counts, and integrity state.
- The creator credential itself is not stored in UI/localStorage; mutation endpoints require an ephemeral creator token held only in the live Trainer page.
- Emotion layer: signed profile baselines plus a transient affect engine (calm/warm/curious/amused/concerned/energized/frustrated) that influences style without claiming biological feelings.
- Self-model supports human-like conversational behavior, stable preferences, autobiographical continuity, and growth. The deep identity invariant is `identity_type = AI system`; attempts to set a biological-human identity through the signed self-model are rejected.
- Self-learning now extends beyond coding. Explicit `Learn that ...`, `Fact: ...`, `Remember that ...` commands stage general facts. Verified/sourced research becomes general knowledge; feedback/corrections/approved examples become conversational-learning signals.
- Nexus Brain directly retrieves relevant banked general knowledge after model replacement/import, preserves provenance, and omits expired current-sensitive records from current-answer context.
- Nexus Brain also retrieves relevant **approved** `conversation_example` / `correction` training signals as portable in-context skill guidance for a fresh model, with explicit instruction to adapt rather than copy wording mechanically.
- Feature/security commits: `f436617` initial creator-locked Brain; `886c412` KDF portability; `6058446` explicit creator-only protected writes; `942f0c1` general-knowledge/autobiography; `a0157e4` cross-model conversational-skill restore; `d3e3dd0` explicit general-fact commands; `0bb41cf` Ed25519 signing; `d7508b3` public read-only seed export/package support; `07fe392` / `db0573d` same-creator signed update enforcement; `b1d9a07` canonical Brain authority + explicit-image adult gate.
- Current unit checkpoint: **188/188 tests passing**.
- Windows validation for `b1d9a079`: **green** — native build, installer compile, fresh install, running-app shutdown/in-place Update preservation, installer artifact upload, portable Windows artifact upload, and dogfood source artifact all passed.
- Next dogfood target: initialize a creator Brain, configure signed subroutines/emotion/self-model, stage facts/research/feedback, run creator Sync, export a public signed Brain, build a seeded installer privately, and verify continuity plus same-creator signed updates on a clean recipient install.

## Source of truth

GitHub repository: `afterburn25/Coding_Agent`

Future development sessions should begin by reading, in order:

1. `README.md`
2. `PROJECT_STATUS.md`
3. `ARCHITECTURE.md`
4. `SESSION_HANDOFF.md`
5. `MODEL_ROUTING.md`
6. `docs/IMAGE_MODULE_SPEC.md` when working on images
7. `docs/RESEARCH_SYSTEM.md` for research/ranking/repair-loop work
8. `docs/WEB_RESEARCH.md` when working on low-level internet/browser capabilities

Do not reconstruct project state from chat memory when the repository can answer it. Update this handoff and `PROJECT_STATUS.md` before ending substantial development work.

## Local Git checkpoints

- `2fa1304` — Add LoRA resolution and subject profile application.
- `ac3f663` — Show GitHub research provider status.
- `9288676` — Add GitHub REST research provider.
- `935145c` — Add validated image workflow import manager documentation/version checkpoint.
- `2e4a194` — Normalize background-removal workflow routing.
- `e6f5012` — Add validated ComfyUI workflow importer.
- `997cf92` — Test conservative GitHub research authority.
- `573bf9e` — Refresh tracked project metadata for v0.5.
- `ac3d91f` — Record v0.5 45-test checkpoint.
- `441ac4d` — Add v0.5 research intelligence and workflow hardening.
- `f208b0c` — Add v0.5 research intelligence and harden image workflows.
- `7ccc6c9` — Add image model manager controls.
- `549f23e` — Add image model and LoRA asset management.
- `46b9477` — Add v0.4 web research and local image foundation.
- `7448d8a` — Add v0.3 transactional coding workflow.
- `9d61887` — Add v0.2 runtime manager and resource-aware model switching.
- `168b51a` — Bootstrap Local Code Agent v0.1.0.

## Current development baseline

- Stable baseline through v0.3 coding workflow.
- Active branch/worktree is now v0.6 Nexus Core/self-hosting development.
- v0.4 adds two new modular capability families without replacing existing coding-agent components:
  - Web research + optional full Chromium automation.
  - Local image generation/editing through an image-router/backend abstraction with ComfyUI as the first backend.
- Automated tests: **99 passing** after the research/verification-repair and image-workflow-validation milestone.

## Completed before v0.4

- Chat-style local UI and backend.
- Automatic coding-model routing: utility, fast coder, primary coder, deep reasoner, reviewer, vision.
- Resource-aware model choice and escalation.
- Managed llama.cpp lifecycle and health recovery.
- Transactional patch editing.
- First-write checkpoints and task undo.
- Permission approvals with resumable exact tool calls.
- Verification detection/build-test execution.
- Reviewer model handoff.
- Persistent task ledger/project memory.
- Lightweight repository index.

## v0.4–v0.5 work implemented

### Web/internet tools

- `web_search` tool using a dependency-free research client.
- `fetch_url` tool for extracting readable text/JSON from web pages.
- `browser_run` tool backed by optional Playwright/Chromium.
- Separate permissions:
  - `network.read`
  - `browser.control`
- System prompt tells the agent to research when current/version-specific facts matter and to cite source URLs actually used.
- `localcodeagent/research/` adds repository-first preflight, environment/package inspection, knowledge-gap planning, source ranking, cache/session history, official-domain mapping, and untrusted-source handling.
- Research tools cover technical topics, docs, GitHub/upstream issues, exact errors, API lookup, release notes, package versions, and cached summaries.
- GitHub research prefers the versioned REST API, reads optional auth from `GITHUB_TOKEN` (configurable env-var name), caches API evidence, and falls back to web search.
- Verification failures can re-enter a bounded diagnose/research/fix/retest loop.

### Image architecture

Created `localcodeagent/image/` with:

- `types.py` — image model/request/job/routing dataclasses.
- `router.py` — automatic image operation and model selection.
- `backend.py` — stable `ImageBackend` abstraction.
- `comfyui.py` — dependency-free ComfyUI HTTP adapter.
- `runtime.py` — optional managed ComfyUI launch/health/stop/recovery.
- `workflow.py` — JSON API-workflow loading and `${variable}` substitution.
- `catalog.py` — local image model discovery.
- `profiles.py` — reusable subject/character profiles.
- `policy.py` — adult consent records and high-risk image safeguards.
- `manager.py` — queue/history/routing/backend/resource coordination.
- `library.py` — model component verification, explicit download/repair/remove jobs, LoRA metadata, and generated ComfyUI extra-model paths.

### Image agent tools

Registered tools:

- `generate_image`
- `edit_image`
- `inpaint_image`
- `outpaint_image`
- `remove_background`
- `upscale_image`
- `create_image_variations`
- `list_image_models`
- `load_subject_profile`

### Image workspace

Added `/image.html` with:

- conversational prompt/editor
- drag/drop image upload
- reference strip
- Auto/manual image model selector
- operation selector
- quality and resolution controls
- generation count and seed controls
- subject profile selector
- advanced negative prompt / LoRA / strengths / outpaint / transparency / upscale / refinement controls
- real-person reference flag
- job progress display
- cancellation
- generated-image gallery
- Image Model Manager verify/install/repair/remove controls and install progress
- LoRA discovery/metadata display

### Shared GPU resource coordination

`RuntimeManager` can now release managed coding LLMs when an image model needs VRAM and restore them after the image job, depending on image resource settings. External runtimes are never terminated by this mechanism.

## Current model defaults

Image model profiles are configuration templates, not bundled weights:

- `qwen-image-2.1` — quality/editing preference, up to 10 configured reference images, transparency flag, configurable ComfyUI workflow.
- `flux2-klein-4b` — fast preview/draft preference, up to 4 configured references, configurable ComfyUI workflow.

No image weights are downloaded automatically yet.

## Current additions after the original v0.4 handoff

- ComfyUI workflows are now validated as API-format before loading large models.
- Image Model Manager can import API-format workflows into configured operation slots.
- Image jobs now expose structured friendly error messages plus collapsed technical diagnostics.
- Required ComfyUI nodes are checked before generation.
- Main chat renders/polls image jobs inline and exposes Edit/Variation/Upscale/Save actions.
- `config.example.json` is synchronized with the current Qwen/FLUX component layouts and research settings.
- Image workspace now imports and assigns API-format ComfyUI workflow JSON per model/operation.
- Import rejects normal ComfyUI UI exports with an explicit Export (API) instruction, validates before write, writes atomically, confines paths to `workflows/image`, and limits imports to 10 MB.
- `remove_background` now resolves both the new canonical workflow key and legacy `background_removal` configs.
- LoRA selections are resolved only from installed local files, respect enabled state/version/family compatibility/max-count/strength bounds, and are recorded on image jobs.
- Selected LoRAs require explicit workflow template slots before generation, preventing a UI selection from being silently ignored.
- Subject profiles now apply saved references, preferred model, generation defaults, and assigned LoRAs automatically.
- Image workspace can filter/use/enable/disable discovered LoRAs.
- Image job errors are classified into stable codes with user-facing messages and collapsed technical details instead of raw exception strings.
- Long-duration autonomy hardening: mission approvals honor `autonomous_approval_timeout_seconds`, missing approval rows recover via bounded replan (`max_approval_retries`), and store retention preserves live missions/pending approvals/open repairs past history tails.
- Local mask editor now paints/erases/fills/inverts and uploads masks through the existing local image-upload route for `mask_path`.
- Before/after workbench can compare a finished edit and reuse generated outputs as the next source image.

## Known gaps / next executable steps

1. Use the Image workspace workflow manager to import and test real ComfyUI API-format workflows for the selected Qwen-Image-2.1 and FLUX.2 Klein local node stacks.
2. ~~Add dedicated background-removal and upscaler adapters/workflows~~ — done: Qwen keeps the dedicated background-removal API workflow; Real-ESRGAN x4+ is now a verified `upscale` profile with a standard ComfyUI adapter workflow and powers the workspace “Upscale after generation” post-process stage.
3. ~~WebSocket/SSE streaming~~ — done: `/api/chat/stream` tokens + `/api/events` bus mirror all non-token events with task attribution.
4. ~~Persistent browser sessions~~ — done: `browser_run` accepts `session` + `save_session` (`storage_state` JSON under `.agent/browser/`).
5. ~~Voice STT host dependency setup~~ — done on this host: `sounddevice==0.5.6`, `faster-whisper==1.2.1`, and compatible `av==18.1.0` are installed; the `base` CPU/int8 model initialized and passed a real WAV transcription smoke test. `stt_backend`, `stt_model`, `vosk_model_path`, and `stt_auto_submit` are now first-class config fields.
6. Runtime tuner produces real numbers only when probes run on hardware — idle auto-tuner covers this; `data/runtime_tuning.json` accumulates results.
7. ~~LLM model downloads as a mission `job` op~~ — done: `model_install` accepts coding-model catalog IDs as well as image profiles; manager startup removes orphaned `.part` downloads before resuming work.
8. ~~Real MCP interoperability~~ — validated against official MCP packages: `@modelcontextprotocol/server-filesystem@2026.8.31` over stdio completed initialize → `tools/list` (14 tools) → `read_file`, and `@modelcontextprotocol/server-everything@2026.8.31` over Streamable HTTP completed initialize → `tools/list` (13 tools) → `echo`; both shut down cleanly.
9. ~~Tree-sitter/LSP indexing upgrade~~ — installed pinned host packages (`tree-sitter==0.26.0`, JS/TS/Python grammars, `python-lsp-server==1.15.0`); JS/TS extraction now reports `backend: tree-sitter`, and a real pylsp session returned document symbols, definitions, references, hover, and diagnostics.

## Testing command

```bash
python -m unittest discover -s tests -v
```

Expected at this checkpoint: `2926 tests` passing (3 environment skips).

## v0.7 modular tool/plugin foundation checkpoint (Phase 1)

- New direction: Nexus Core evolves from a chat/coding interface into a modular general-purpose local AI workstation; users describe outcomes and the agent picks models/tools/runtimes. Phase 1 foundation is implemented.
- `ToolSpec` is now a full manifest (id/category/version/provider/capabilities/permissions/network/GPU/OS/docs/install status/health check). Built-ins get metadata from `localcodeagent/tools/manifests.py`; registration call sites are unchanged.
- Registry gained capability lookup, enable/disable (persisted at `data/tools_state.json`), health hooks, and per-tool usage counts. Disabled or non-callable tools stay out of model schemas.
- `localcodeagent/permissions.py`: `PermissionManager` adds the `session` level and workspace profiles (safe/developer/power_user/offline/research_only/custom); changes persist to `config.json` (`permission_profile`).
- `localcodeagent/jobs.py`: `JobManager` unified ledger — normalizes agent tasks, image jobs, and installs into queued/preparing/running/waiting_for_tool/waiting_for_permission/completed/failed/cancelled; generic jobs persist to `data/jobs.json` and are marked failed on restart.
- `localcodeagent/processes.py`: `ProcessManager` registers llama.cpp runtimes and ComfyUI as controllable services with live status/uptime and delegated lifecycle.
- `localcodeagent/tools/interfaces.py`: structural contracts for executable/model/image/browser/research/media/data/document/sandbox/VCS providers.
- `localcodeagent/tools/plugins.py`: declarative JSON manifests under `tools/manifests/` (config `tool_manifests_dir`) register external tools — install detection, health checks, optional subprocess invokers. No-invoke manifests are catalog-only and never reach model schemas.
- New endpoints: `GET /api/tools`, `/api/tools/health/<id>`, `/api/permissions`, `/api/jobs`, `/api/processes`, `/api/resources`; `POST /api/tools/state`, `/api/permissions/level`, `/api/permissions/profile`, `/api/processes/action`, `/api/jobs/cancel`.
- New **Tools & Plugins** page (`web/tools.html`); main-nav Tools link routes there.
- Feature commits: `105182a` (registry/permissions/jobs/processes/UI), plugin manifest loader + interfaces commit on top.
- Unit checkpoint: **328 tests passing** (grew through the milestones below).
- Phase 2 started: `terminal_run`/`terminal_processes`/`terminal_kill` (controlled shells + tracked background jobs), `search_code`/`search_filename`/`search_error` (ripgrep + fallback), `detect_build_system`/`build_project`/`configure_project`/`run_tests`/`clean_project` (CMake, Meson, Cargo, .NET, MSBuild, npm/pnpm/yarn, Gradle, Maven, Make, Python).
- Tool Router landed: `localcodeagent/tool_router.py` ranks candidates by capability (permission/offline/GPU/VRAM/RAM/OS filters, `preferred_tools` config, cached health), executes with fallback, records telemetry. Endpoints: `GET /api/tools/route/<capability>`, `GET /api/tools/telemetry`. Health probes wired for git/ripgrep/GitHub/ComfyUI/shell families.
- MCP landed: `localcodeagent/mcp.py` — dependency-free stdio JSON-RPC client + `MCPManager` (connect/disconnect/restart/status), `config.mcp_servers`, registry import as `mcp__<server>__<tool>` with `source="mcp"`, `/api/mcp` + `POST /api/mcp/action`, Tools-page MCP panel. Doc: `MCP.md`.
- Later milestones on top: credential vault + `api_request` + `code_symbols`/`code_map` (`17af833`), Local Models page `/models.html` (`c2e5529`), data tools `data_query`/`profile_dataset`/`chart_generate` (`a37b3f3`), FFmpeg media wrappers (`04500b9`), `media_transcribe` registry-chained pipeline (`1235a7d`), document tools `extract_text`/`ocr_image`/`convert_document` + invoker `shell.execute` gating (`74bfb91`).
- Invoker permission rule: any manifest tool that spawns a subprocess is gated on `shell.execute` (or a stricter dedicated key like `docker.access`); declared read/write keys become `permissions_required` metadata.
- Newest milestones: SSE event bus `/api/events` + `tools.js` live refresh (`3d7a3ee`), local knowledge/RAG index + `python_exec` sandbox (`52e794b`), Piper TTS manifest + plugin stdin + `speak_text` (`d5e7413`), declarative `run_workflow`/`list_workflows` pipelines (`da3e9c6`), `blender_render` scene generator (`e28ba18`), model-callable `use_capability` ToolRouter dispatch (`7a8ff73`).
- Latest: learned routing (telemetry persistence + success-rate ranking + UI panel `fd45099`), ast backend for Python code intel (`6b5327c`), find_tools/system_resources/install_tool (`feb9f49`, `1a94565`), AppState wiring contract tests (`b6bd8c1`), git worktrees (`0a130db`), GET /api/workflows + Workflows panel (`cef5132`).
- Newest batch: managed-service watchdog restart (`d770522`), MCP Streamable HTTP transport (`9a2d793`), vault `secret:` env references for MCP servers + `ask_workspace` workflow (`c1bd188`), cooperative workflow cancellation between steps (`e05f7a4`), measured generation telemetry — TPS/TTFT recorded per generation, aggregated per model, surfaced on `/api/model-telemetry` and the Models page Performance card (`17895b7`).
- Latest batch: workflow resume checkpoints at `.agent/workflow_runs/<id>.resume.json` with `resume=true` re-entry (`38b7df8`) and `resumable` surfacing in `list_workflows`/API/UI (`a22516f`); Nexus Brain unlock brute-force backoff (`aedc3b9`); MCP HTTP `headers` end-to-end incl. `secret:` vault refs (`7b4c55f`); `/api/readiness` tools summary (`c645ea7`); dependency-free ComfyUI `/ws` progress listener with reconnect backoff (`bbe528e`, `a863af4`); `perf` SSE event with measured TPS/TTFT in the chat rail (`873f2bc`); research provider outcome stats persisted to `provider_stats.json` (`e7e7571`); optional tree-sitter backend for non-Python code intel (`5187e2f`).
- Autonomous operation + live command visibility: `tool_start`/`tool_output` SSE events, Devin-style terminal blocks in the chat activity rail with live stdout/stderr streaming (`run_process_streaming` in terminal.py; `run_shell`/`terminal_run` emit through a per-session `stream_sink` in `registry.context`), and parallel read-only tool batching; `autonomous_mode` auto-approves ask/session workspace actions (hard gates: spend/message/mic/camera, skills.manage, repair.manage, runtime.manage, and desktop observation/control always ask; deny stays deny) via `PermissionManager.set_autonomous`, `POST /api/permissions/autonomous`, and a Tools-page toggle; `autonomous_max_continuations` bounds step-limit extensions; `RuntimeManager.evict_idle()` unloads stale/pressured managed models on the process-watchdog tick while pinning models serving active tasks; cooperative task cancellation (`task-<id>` via `/api/jobs/cancel`, chat Stop button, mid-batch skip, resume of cancelled tasks refused); `config`: `model_idle_unload_seconds` (default 900), `memory_pressure_vram_gb`/`ram_gb`.
- Resilience batch: startup auto-resume of interrupted tasks when autonomous (`auto_resume_interrupted_tasks`, `cc3e8b5`), bounded transient retries on external endpoints (`cc3e8b5`), `SecretVault.redact()` filtering vaulted values from live output chunks, job cancel kills tracked background terminal processes (`c292721`), llama.cpp post-health warmup for first-token latency (`model_prewarm_*`, `dd68787`), and `agent_tool_timeout_seconds` (default 1800) so a hung tool returns a timeout instead of stalling the run (`0c8e32f`).
- Reconnect-safe live visibility (`12fbfbe`): `/api/chat/stream` `emit()` mirrors non-token events (`task`, `tool_start`, `tool_output`, `tool`, `model`, `approval`, `perf`, `context_trim`, `cancel`, `image_job`, queue transitions, `waiting_approval`, `approval_timeout`) onto the shared `/api/events` bus with `task_id` attribution, so a reloaded page follows the run live via `connectAgentEvents()` in `web/app.js`; `agentStreamActive` suppresses bus rendering while the direct request stream is connected, clearing on result/error/disconnect. Queue dequeue history bug (`self.state.history` inside AppState) fixed in the same commit.
- Latest resilience batch: `_trim_context` stubs older tool/assistant bodies once the prompt exceeds ~3 chars/token of the profile's context window (`781faf0`); `autonomous_approval_timeout_seconds` (default 3600) fails approval-parked tasks on the watchdog tick so hard gates can't stall an unattended run (`eec7cf2`); `autonomous_error_retry_seconds` (default 120) re-drives stale error tasks via `recover()` bounded by `autonomous_max_recoveries` (`21dbdde`); all resume paths now go through `_execute_tool`/`_drive_or_error` with `tool_start`/`tool` events (`89837cd`, `da9b82f`); vaulted values are scrubbed from emitted tool args and completion results (`6b9d816`, `4868345`).
- Concurrency + batching: live `tool_output` chunks route per-task via thread-local dispatch with reader-thread fallback (`539a3a7`, `9d6169b`); file mutations attribute to the owning task under concurrent runs (`04c363a`); durable `WorkQueue` (`.agent/queue.json`) dequeues prompts through `agent.run` on the watchdog tick — `GET/POST /api/queue`, `POST /api/queue/cancel`, queue surfaced in `/api/tasks` and the chat task card (`4e53364`, `bcd74d2`, `e61dee3`). `chat_queue_when_busy` (default true) auto-enqueues chat messages sent while a task is active instead of racing it (`c061ec5`). Tools page has a Work queue card with per-item cancel (`bef8ff8`).
- Event-bus polish batch: agent-run failures publish an `error` event to the bus (`7b20df4`); task card recovered on bus open via `/api/tasks` fetch rather than relying on replay (`1b77b3b`); orphan `tool_output` chunks lazily open terminal blocks (`4dfe3b1`); `agentStreamActive` guard released on fetch/non-OK/stream errors (`84e0781`); `/api/tasks` returns `{current, recent, queue}` — not `{tasks}` (`c7de272`); recent tasks are clickable to inspect/resume (`a7e9c3d`); bus handles `approval`/`image_job` (`f203f25`); `tool_output` chunks coalesce in the 100-event replay history so chatty commands don't evict task context (`c044877`); queue-run and recovered tasks publish through shared `_bus_emit` (no tokens on the bus, `task_id` attribution) — `recover()` gained an `event_callback` param (`8cb4a22`, `cd260c8`). Persisted per-task terminal transcripts under `.agent/terminal/<id>.log` (512 KiB bound, 64 KiB tail via `GET /api/task-log`) restore prior command context on reload (`406c68f`); orphaned logs prune on load (`9e8dfe3`). Follow-ups: instant dequeue on enqueue/task completion (`4498381`, `c375dec`, `58462db`), task lifecycle markers in transcripts (`8dfa282`), `/api/task-log` path-traversal guard (`59414c5`), bounded request event queue under client backpressure (`562630f`), `?replay=N` on `/api/events` + `_bus_emit` policy test (`2d925a2`).
- Next: real ComfyUI Qwen/FLUX workflow runs on GPU hardware, real MCP-server interop validation, tree-sitter/LSP indexing, and continued Nexus Brain dogfooding.

## v0.7 autonomy hardening + Brain lifecycle checkpoint

- Queue single-flight: dequeue serialized under a lock; completion chain waits
  briefly so fast workers can't strand items; 200-item bound; busy chat returns
  409 or auto-queues via `chat_queue_when_busy`.
- Transcripts cover the full lifecycle via a wrapped event callback — model
  select, task transitions, tool I/O, approvals, image jobs, errors, and
  pre-session failures all land in `.agent/terminal/<id>.log`.
- Idle eviction: `model_idle_unload_seconds` for llama.cpp runtimes plus
  `comfyui_idle_unload_seconds` for the managed ComfyUI process (external
  installs untouched); `idle_evicted` bus events surface on the Tools page.
- Growth bounds everywhere: task ledger 100, queue 200, terminal logs 512 KiB,
  checkpoints pruned, routing telemetry compacts, JobManager 300, research
  cache/sessions pruned (500/100), llama/comfyui/terminal/backend-host logs
  tail-bounded, EventBus 100-event replay + bounded subscriber queues.
- Shutdown hygiene: `stop_state` terminates MCP servers, tracked terminal
  processes, managed ComfyUI, and model runtimes. Desktop host restarts a
  crashed backend — crash-loop bound resets after 5 stable minutes.
- All durable stores write atomically (tasks/queue/jobs/brain/memory/secrets/
  consent/activation backup/image ledgers/workflow resume checkpoints).
- Nexus Brain: encrypted creator-key backup/restore
  (`POST /api/nexus-brain/key-backup` + `/key-restore`; bundle re-encrypted
  under a backup passphrase, restore proves key by verifying the existing
  signature — foreign keys rejected); bounded 10-entry signed settings history
  with reversible creator rollback (`GET /api/nexus-brain/history`,
  `POST /api/nexus-brain/rollback`); audit trail + version list rendered on
  the Trainer page; `settings_history` stripped from imported payloads.
- Isolated selftest now smoke-checks `/api/queue` and the `/api/events` SSE
  handshake in addition to boot + the four pages.
- Verified checkpoint: **336 tests** (2 skips). Remaining work is
  hardware-bound: real Qwen3-14B/30B runs, ComfyUI/FLUX interop, overnight
  unattended queue soak; plus interactive Brain dogfood on a real install.

## 2026-11 — GPU-independent end-to-end harness (343 tests)

`tests/test_end_to_end.py` adds `_FakeModelServer`: an in-process
OpenAI-compatible endpoint (`GET /health`, `POST /v1/chat/completions`,
stream + non-stream, scripted tool_call then final-answer turns). Knobs:
`tool_name`/`tool_args` (which call to emit), `fail_next` (N 500s),
`delay` (stall responses). It drives the real `AppState` + orchestrator +
tool registry + task ledger + transcripts — no GPU needed.

Coverage:
- happy path: real `system_resources` call -> tool result -> final answer
  -> `$`/ok/`## result` transcript markers
- queue drain: two chained tasks through the real work-queue worker
- transient 500 -> `ensure_ready` retry -> `transient_retry` model event
- persistent failure -> bounded calls, `error` status, `## error` marker
- mid-run cancel -> `cancelled` result (found+fixed a resurrection bug:
  `_drive` re-marked `running` at loop entry, losing cancels that landed
  during the first in-flight model call)
- approval gate: `write_file` pauses `waiting_approval` with persisted
  call; approve executes + feeds result to model; deny sends
  `PERMISSION_DENIED`

Use it for the soak: script N tasks, `fail_next`, delays, and restarts on
the GPU box to exercise the overnight path deterministically before real
inference.

Verified checkpoint: **343 tests** (2 skips), head `b9f24e9`.

## CRITICAL regression found by the e2e harness (7acfc8e)

`_sse_event` (introduced 69d3cdc) wrote `f"...\ndata..."` — literal
backslash-n text on the wire instead of LF. Every SSE frame on
`/api/chat/stream` AND `/api/events` was one unparseable line; EventSource
and the frontend `\n\n` splitter never fired. The UI silently fell back
to task-ledger recovery — live token/tool streaming was dead while
transcript-level verification kept passing. Header-only selftest checks
missed it; the new `test_full_stack_sse_stream_end_to_end` parses real
frames and now guards it. Also added: batched tool_calls, persisted
approval across AppState restart, catalog-download teardown flake fix.
Checkpoint: **346 tests**, head `7acfc8e`.

## In-app tool installation (Tools page owns optional downloads)

- The Windows installer no longer downloads ComfyUI or the Qwen-Image/FLUX stacks — only the two coding GGUFs bootstrap at setup, keeping install fast. All optional tools install on demand from the Tools page.
- `localcodeagent/tools/downloads.py` (`ToolDownloadManager`) runs manifest `install.method: "archive"` jobs: HTTPS download → stable per-tool `.part` (`{tool_id}.part`) → SHA-256 → extract → `.chatnexus-version` marker. Job metadata carries phase/download_progress/extract_progress/bytes/bytes_per_sec/eta_seconds/current_file/current_path; emits over the JobManager→EventBus SSE path. Dedupe per tool; cancel/fail **keeps** `.part` so retries resume via HTTP `Range` (416 → clean restart; server ignores Range → truncate restart; complete .part → skip fetch).
- Full lifecycle in-app: install (resumable), Reinstall, **Update** (`installed_version` vs manifest `version` → "Update available" chip), and **Remove** (`POST /api/tools/uninstall` → `tool_remove` job deletes dest + marker + `.part`, archive-method only, same `packages.install` gate). Disk preflight refuses when free < size_bytes and warns when < ~3x; `/api/tools` reports `disk_free_bytes` (shown in the Installations card; oversized installs flagged LOW DISK SPACE).
- 7z extraction prefers the OS `tar.exe` (libarchive, native speed, per-file `-v` progress); py7zr is the bundled fallback (see `--collect-submodules py7zr` + codec hidden-imports in `build_windows.ps1`). Extraction progress is byte-weighted and throttled (4 emits/sec — unthrottled per-file updates rewrote jobs.json thousands of times and flooded SSE).
- Manifest `detect.files` are install-root-relative markers for non-PATH payloads; `os_supported` is computed into the tool payload and gated in `install_tool` + UI. Packaged builds find manifests under `_MEIPASS` via fallback; `pip` installs resolve `managed_python()` (ComfyUI embedded Python → `{app}/python`) because `sys.executable` is the frozen exe.
- Tools page (`web/tools.*`): Installations card (overall bar + current file/path), per-tool progress bars, Installed/Not installed/Installing chips, Install/Cancel/Reinstall, and an Image model packs section wired to `/api/image/models/install`.
- `tests/test_tool_installer.py` (17 tests) covers the archive lifecycle, traversal/sha/cancel/dedupe, **Range resume**, uninstall (dest+marker+`.part`, refusal during active install, dest traversal guard), detect.files, managed-python resolution, disk preflight, and a real `AppState.install_tool`→uninstall round trip. `tests/test_installer.py` now asserts the installer ships NO optional downloads.

## Short-window scrolling + tool install/remove backend + ComfyUI manifest fix

- `web/styles.css`: added `@media(max-height:680px){body{overflow:auto}}` — the chat shell pins `min-height:680px` while the desktop window allows 640px minimum, so anything below 680px clipped the composer/footer with `body{overflow:hidden}` and no way to reach them. Below the threshold the page now scrolls to the bottom; other workspace pages already scroll internally (`*-main{overflow-y:auto}`) and are unaffected.
- Package-manager uninstall: `tools/plugins.py` gained `REMOVE_METHODS` + `uninstall_command()` (winget/choco/uv/npm/apt/dnf/brew/pip mirroring `INSTALL_METHODS`); `server.py:uninstall_tool` now dispatches non-archive removals as a `tool_remove` job through the shared `_permission_gate` instead of rejecting them; `ToolRegistry.manifest()` exposes `removable` so the UI shows Uninstall only when automated removal exists (grid cards + detail drawer in the redesigned Tools page consume it). Archive removal is unchanged (`ToolDownloadManager.uninstall` deletes dest/marker/.part).
- Fixed `tools/manifests/comfyui.json` — a missing comma after the new `dependencies` array made the manifest unparseable, so ComfyUI silently never registered on the Tools page.
- Live-verified: dev server `/api/tools` reports 104 tools with blender/comfyui/ffmpeg/docker `install_status: missing`, `removable: true`, `partials` map and `disk_free_bytes` populated.
- `tests/test_tool_installer.py`: +5 tests covering `uninstall_command` shapes (winget/pip/unknown/archive), the `removable` manifest flag, a mocked `winget uninstall` AppState round trip, and the manual-removal error path. Full suite: **374 tests, 2 skips — green**.

## Tools & Plugins redesign + Settings > Permissions (main, this session)

- `web/tools.html/.css/.js` rebuilt as the operational catalog: summary cards (installed/available/updates/running/errors), search + filter chips + sorting, responsive card grid, right-side detail panel (Overview/Capabilities/Dependencies/Configuration/Logs), collapsible Installation Queue showing phase/bytes/speed/ETA/current file + resumable `.part` rows, plus the preserved image packs, processes, jobs, work queue, routing telemetry, and workflows panels.
- New `web/settings.html/.css/.js` — dedicated Settings shell with secondary nav (General/Permissions/Models/Appearance/Privacy/Notifications/Advanced). Settings > Permissions: profile cards, category summaries, searchable matrix (Capability/Status/Scope/Approval/Last Used), right detail panel (approval rules incl. Require Creator Approval, domain/dir/repo scope editors, associated tools, recent activity), and the audit log. Chat nav Settings now links here.
- `localcodeagent/permissions.py`: new `creator` level (requires unlocked Nexus Brain session, fail-closed), bounded persisted audit (`data/permission_audit.jsonl`, 2000 entries, decisions only), `PERMISSION_INFO` metadata (label/category/scope/risk/tools) with prefix fallback, `permission_scopes` config with generic URL domain enforcement in `ToolRegistry.execute`, in-memory `record_use` last-used stamps, summary() gains info/categories/scopes/last_used.
- `server.py`: `GET /api/permissions/audit`, `POST /api/permissions/scope`, `/api/processes/log?id=` tail endpoint (secret-redacted), `/api/tools` gains `partials` (resumable .part bytes) and per-tool `install_path`/`install_size_bytes`/`installed_at`/`latest_version`/`process_id`/`dependencies`/`executables`/`detect_files`/`mcp_server`/cached `health`. Shared `_permission_gate` handles deny/ask/creator uniformly (install, uninstall, secrets); `needs_creator` flag on gated responses.
- `tools/plugins.py`: manifest `process` + `dependencies` fields; `tools/base.py`: health-result cache, install path/size/installed_at resolution, audit events on execute decisions, URL scope enforcement.
- `tests/test_permissions_ui.py` — 22 tests: creator level + fail-closed, audit persistence/bounds/no-secrets, scope validation + URL enforcement, summary shape, manifest enrichment, health cache, and endpoint-level coverage over a real HTTP server.

## Nexus Core rebrand + real startup splash (main)

- Canonical product name is now **Nexus Core**; all user-facing Chat Nexus branding is retired (UI pages, window title, backend messages/prompts, installer text, shortcuts, Add/Remove Programs, docs).
- Official assets (supplied artwork, unmodified): `web/assets/nexus-core-logo.png` (full wordmark), `web/assets/nexus-core-icon.png` (compact shield), `web/assets/nexus-core-splash.png` (startup splash), `desktop/ChatNexus.Desktop/nexus-core.ico` (16/24/32/48/64/128/256 layers, embedded in exe + installer).
- **Executable renamed to `NexusCore.exe`** and installer output to `NexusCore-Setup-<ver>-Windows-x64.exe`. The stable AppId `ChatNexus.Afterburn25` is intentionally kept so existing Chat Nexus installs upgrade in place — never a second installation. The installer also detects the legacy `Programs\Chat Nexus` dir, taskkills legacy `ChatNexus.exe` processes, and `[InstallDelete]`s the old exe. Internal identifiers intentionally unchanged: `ChatNexus.Backend.exe`, `backend/` path, `installer/ChatNexus.iss`, `ChatNexus.Desktop` namespace, `dist\ChatNexus`, `CHAT_NEXUS_*` env vars, Python package `localcodeagent`.
- **Real splash lifecycle** (`desktop/ChatNexus.Desktop/Program.cs`): `NexusCoreApplicationContext` shows `SplashForm` (borderless, supplied artwork) immediately; `MainForm` is created hidden (`CreateControl`, never shown). `StartupProgress` is the single startup state machine — real milestones only (host init 6% → backend launch 15% → backend health 30–55% → WebView2 72% → shell load 85% → handshake wait 93%). Two-line status (`ACTION · SUBSYSTEM` + dim description) under the progress bar.
- **Dismissal requires BOTH** `AppReady` (frontend `nexus-core-ready` postMessage, sent after the shell finishes its init fetch chain in `web/app.js`) **and** 7 s minimum. Ready-early → holds at ~98.5% with `FINALIZING · NEXUS CORE`; on satisfaction shows `READY · NEXUS CORE`, completes the bar, plays an ~850 ms core-glow bloom, then closes splash and shows the already-initialized main window in the same turn (no gap). A bounded NavigationCompleted+15 s fallback prevents a broken page from hanging startup.
- Fatal startup failure → `NEXUS CORE COULD NOT START` state with Retry / Open Log (`data/logs/backend-host.log`) / Exit; Retry rebuilds progress + forms without duplicate backends. Backend watchdog/restart recovery unchanged.
- `tests/test_branding_splash.py` (20 tests): asset presence/ico layers, exe+installer icon embedding, no user-facing Chat Nexus outside legacy markers, splash ordering/7s rule/handshake/failure/atomic transition, AppId preservation, legacy upgrade cleanup, data preservation, and CI workflow contracts. Installer tests updated; workflow smoke test now plants a legacy `ChatNexus.exe` + fake `NexusCore.exe` and verifies both are closed/removed on update.
- Suite: **423 tests passing (2 skipped)**. CI run `36917101388` — **green** (Ubuntu unit tests + Windows desktop/installer build, fresh-install + update-preservation smoke incl. legacy `ChatNexus.exe` cleanup, installer + portable ZIP artifacts).
- Final commit: `9a48b476b01064936f95d37bf16271278f493dbb` (rebrand `46181b8` + packager/doc checkpoint).

## Unattended-reliability pass + performance tuning (main, latest)

- **Runtime tuner** (`localcodeagent/runtime/tuner.py`): probes bundled `llama-server` capabilities, fingerprints per GPU/build/model/context, auto-tunes launch flags (flash-attn, cache-reuse, batch sizes), persists to `data/runtime_tuning.json`, exports benchmark artifacts to `data/benchmarks/`. `POST /api/tuning` actions benchmark/reset/performance_mode; Models page gained a Runtime Tuning panel.
- **Activity timeline** (`localcodeagent/workflow/activity.py` + `GET /api/activity`): durable JSONL parent/child rows — planning/routing/model-load/memory/research/tool calls with live stdout tails, verification groups with nested children, residency events (FREEING VRAM / STARTING MODEL), job/image-job mapping, category filter chips, restore on SSE reconnect. Open rows reload as `interrupted`.
- **Per-command Stop**: timeline Stop on a running command kills only that subprocess via `/api/jobs/cancel` `command-<task_id>`; `[cancelled]` marker, task continues; flag cleared per command.
- **Kill-race fix (Job Objects)**: `taskkill /T` enumerates children at kill time — a child spawned in the enumeration→death window escaped and held inherited log/pipe handles forever (orphaned PING.EXE repro). Both kill paths (`TerminalTracker.kill`, `run_process_streaming._kill_tree`) now assign a `KILL_ON_JOB_CLOSE` Job Object right after Popen; `TerminateJobObject` + `WaitForSingleObject(job)` drains the whole tree atomically — including children spawned mid-kill — before returning, so file locks release immediately. taskkill remains the fallback; POSIX unchanged (own pgid + killpg).
- **Pressure-eviction thrash fix**: `evict_idle` no longer evicts `keep_loaded` residents under ambient memory pressure — when the resident baseline itself leaves less free RAM/VRAM than the floor, evicting it just to rewarm is a thrash loop. Launch-time contention still reclaims them via `_enforce_residency` with rewarm on session close.
- **Boot-hang fixes**: bounded `os.walk` replaced unbounded `rglob` over the install root (manifest executable resolution + install-size calc both walked the ~100k-file ComfyUI tree at every cold start; install size is now persisted in state).
- **Splash**: single custom gradient bar (baked-in artwork bar painted over first). Backend watchdog auto-restarts with 3-strikes/5-min decay + UI reload.
- **Branding**: icon/logo/ico are true RGBA (flood-filled connected background, feathered edges, dark shield panels preserved); "NC" avatar replaced by transparent emblem; favicon links on all pages.
- **Windows trust**: backend EXE version metadata, optional Authenticode signing (`NEXUS_CODESIGN_PFX_B64`/`NEXUS_CODESIGN_PASSWORD` or `NEXUS_CODESIGN_THUMBPRINT` secrets), per-artifact `.sha256` + `CHECKSUMS.txt` published with each release.
- **Real benchmark numbers** (installed app, RTX 3080 Ti, resident model under contention): qwen3-coder-30b → 13.1 tok/s gen, 87.6ms warm TTFT (cache-reuse works, 55× faster than cold), flash-attn selected; qwen3-14b → ~2.9 tok/s dense under contention.
- Suite: **484 tests, 2 skips — green**. Latest head: `55c265a` (job-object + thrash fixes); prior green CI run `36933655384` (`255323e`).

## Self-repair + orphan reaping (main, latest)

- **Request self-repair** (`localcodeagent/models/openai_compat.py`): HTTP 400 "exceeds the available context size" from llama.cpp now self-heals — the provider parses the server's reported token counts, stubs oldest non-system bodies (system + latest turns preserved), caps max_tokens, and retries (≤2 repairs, `auto_repaired` marked in the response). All HTTP errors surface as `ModelHTTPError` carrying the server's real message instead of the misleading "could not reach" (HTTPError is a URLError subclass; the body was discarded).
- **Smarter budgeter** (`orchestrator._trim_context`): reserves `max_output_tokens` and uses 2.6 chars/token — prompt+output must fit under the *launched* ctx (tuner's `recommended_context` can be below `context_window`; the overflow that motivated this was a 16.7K request vs a 16384-token launch). 4xx rejections skip `runtime.recover()` — restarting a healthy server can't fix a malformed request.
- **MCP lifecycle fix** (`localcodeagent/mcp.py`): stderr drain, Job Object assignment, and the initialize handshake were dead code after `_resolved_env`'s `return` — stderr never drained (verbose servers deadlock), handshake never ran, no tree-kill. Moved into `start()`/`close()`; `close()` kills via Job Object so shell-wrapped servers can't orphan children.
- **Backend singleton** (`Program.cs`): force-killed host left its backend orphaned holding port+VRAM (observed live). `BackendProcess.Start` writes `data/logs/backend.pid` and reaps a path-matching leftover with `taskkill /T` before spawning.
- **Taskbar icon**: running exe embeds the transparent ico (verified per-layer alpha); Explorer cache cleared via ie4uinit. `new Icon(path)` form icon and exe resource both transparent.
- `tests/test_mcp.py` (4 tests: stderr-flood handshake, cmd-wrapper tree kill, secret env); streaming tests gained context-overflow repair + non-overflow 4xx surfacing cases.
- Suite: **495 tests, 2 skips — green**. Commits: `f09ee74` (MCP + reaping), `d43a6a1` (self-repair).
- **Malformed tool-call args self-repair** (`77d0c32`): `_parse_call` returns (name, args, error). Repairable text (Python literals, trailing commas, truncated braces) executes with recovered args; unrecoverable text feeds "arguments were not valid JSON, re-emit" back to the model instead of executing with `{}`.
- **Corrupt-state self-repair** (`44becbc`): `load_config` quarantines a damaged `config.json` to `*.corrupt-<ts>` and boots defaults — previously an unguarded JSONDecodeError crash-looped the backend. Per-model entries skip unknown keys (forward-compat). jobs/tasks stores quarantine corrupt files the same way.
- **First-run note**: a freshly PyInstaller-built unsigned backend gets a one-time ~50s AV scan of `_internal` on first launch — splash sits at CORE SERVICES; subsequent launches are fast.


## v0.8 local voice subsystem checkpoint

- `localcodeagent/voice/`: full local-first TTS stack. `TTSEngine` provider
  interface; `KokoroEngine` (Kokoro-82M ONNX via `kokoro-onnx 0.6.1`,
  Apache-2.0; assets SHA-256-pinned in `voice/assets.py` and the installer).
- `VoicePreset` schema v1 — nondestructive DSP recipes. Official preset
  `nexus-synthetic-isabella` (bf_isabella; pitch/EQ/exciter/compressor +
  neural/glass/micro parallel layers + stereo width + limiter; master
  `synthetic` slider scales everything).
- `SpeechTextFilter` block classifier (SPEAK/SUMMARIZE/SKIP) — code, logs,
  diffs, JSON, URLs, hashes never reach TTS. `SentenceStreamer` segments
  token deltas fence-aware so first sentence speaks early.
- `VoiceManager`: ordered cancellable queue, mute-stops-now, per-task
  stale-speech suppression, bounded LRU WAV cache, WAV/MP3 export.
- API `/api/voice/*` (status/speak/preview/mute/stop/config/presets/audio),
  voice SSE channel on `/api/chat/stream` + shared bus.
- `voice_*` tools registered under `audio.read|generate|manage` permissions.
- `web/voice.html` Voice Studio (A/B compare, Natural↔Synthetic, preset
  CRUD/import/export) + `web/voice_global.js` global mute/playback on all
  pages; per-message speaker on assistant replies.
- Installer ships Kokoro assets into `{app}\modelsoice` (hash-checked,
  skip-if-verified); `data/voice/presets` survives upgrades.
- Build: PyInstaller collects onnxruntime/kokoro_onnx/phonemizer/
  espeakng_loader/numpy; `kokoro-onnx` installed with
  `--ignore-requires-python` (declared <3.14, verified working on 3.14).
- `build_windows.ps1` bundles the SHA-256-verified Kokoro assets into
  `dist\ChatNexus\models\voice` (cached under `packaging\voice-assets\`),
  so the portable package works offline; the installer still downloads
  hash-checked copies with skip-if-verified on upgrade.
- Commits: `74a44ac` (subsystem) + `092840a` (packaging fixes: asset
  bundling, 120 s backend health budget, `voice.html` smoke probe,
  watchdog idle-unload, SSE/bus segment dedupe, UTF-8 BOM).
- Verified end-to-end on the packaged build: `NexusCore.exe --self-test`
  green; `/api/voice/speak` synthesized and `/api/voice/audio/<id>`
  served real WAV from `dist\ChatNexus` (2.87 s audio in ~4.3 s).
- Measured: model load ~0.8 s, warm synthesis RTF ~0.43 on CPU.
- Test suite: 537 / 537 (2 environment skips).

## 2026-10-01 — Reliability/live-ops hardening round (main, deployed)

- **Missing-tool reporting** (`d7f61d7`): `TOOL_NOT_INSTALLED:` is now an
  error prefix; the router returns a `tools_not_installed` outcome naming
  the missing tools + install guidance (manifest install hints), instead
  of letting a missing tool look like a success. Registry direct-call
  message names the tool and points to Tool Manager. Orchestrator prompt
  instructs the agent to tell the user what to install.
- **Atomic-write race** (`77c7d28`): `localcodeagent/fsutil.py` —
  `atomic_write_text/bytes` (unique tmp name per call) +
  `replace_with_retry`; all persistence call sites converted. Fixes the
  Windows `jobs.json` WinError 32 that killed image jobs; image job
  persistence serialized.
- **WebView2 stale-page flash** (`7316925`): disk cache invalidated once
  per backend payload change — old tools.html no longer flashes.
- **ComfyUI lifecycle** (`b4f2066`): a timed-out request no longer kills
  a half-booted ComfyUI; the process stays in `loading` and the next
  request attaches to the same boot (restart only after a 2x-timeout
  total budget). Windows `stop()` tree-kills so re-exec'd children can't
  hold port 8188. `comfyui_startup_timeout` default 180 → 300 s.
- **Voice composer mute** (`fcd540c` + `789c407`): icon-only speaker
  button next to Send (🔊/🔇), synced with nav toggle via shared
  `.voice-mute-btn` class. `789c407` fixes a `getAttribute()=x`
  SyntaxError that disabled the entire voice client (no auto-play).
- **Voice latency** (`4d87cd4`): engine pre-warm thread at task start so
  the ~1 s Kokoro load overlaps text generation.
- **Cold-boot health budget** (`df3277d`): desktop host waits 180 s
  (was 60) for backend health — fresh unsigned ~200 MB exe under AV
  scan could exceed 60 s → spurious "could not load backend".
- **Context budget + overflow recovery** (`0abd2e4`): optional injected
  context blocks + history share ONE budget per lane
  (`fast_general_context_chars`=9000, `coding_context_chars`=60000 —
  newest history wins). Previously per-block caps could total 20k+
  tokens on a simple question → 16k overflow + double prompt eval
  (~200 s responses). Last-resort repair: retry once without tool
  schemas when shrinking can't converge. fast/primary coder lanes raised
  to 24576 ctx (Qwen3-14B native 32k).
- Deployed: backend + host rebuilt and copied to installed app; exe
  hash-verified. App healthy post-deploy (voice enabled, unmuted).
- Suite: 540 tests, OK (skipped=2).

## 2026-10-02 — Juggernaut X v10 becomes default text-to-image (main)

- New default image profile `juggernaut-x-v10` (RunDiffusion SDXL
  checkpoint, pinned rev `e53841ec`, sha256 `d91d3573…0c45`,
  CreativeML OpenRAIL-M, 7,105,348,672 bytes). Normal/photorealistic
  t2i routes to it at priority 110; Qwen (80) keeps edit/inpaint/
  outpaint/background_removal; FLUX (70, speed_tier=fast) deterministically
  wins preview/fast/draft via a fast-tier pool restriction in
  `ImageRouter.choose`.
- New SDXL API workflow `workflows/image/sdxl/juggernaut-x-v10-t2i-api.json`
  (CheckpointLoaderSimple + CLIPTextEncode ×2 + EmptyLatentImage +
  KSampler + VAEDecode + SaveImage); checkpoint resolves via
  `${component_checkpoint}`; quality-focused default negative prompt has
  no censorship terms.
- `load_config` merges new default image models into existing installs by
  id (user entries win). `ImageModelProfile` gained `display_name` /
  `tagline`; Image Studio dropdown sorted by priority and shows friendly
  labels.
- SDXL CFG default is family-aware: `stable-diffusion*` → 6.5, others →
  1.0 (distilled). Approved adult prompts reach the checkpoint verbatim —
  no hidden sanitizer anywhere in the path (verified by test).
- Checkpoint verified on disk in installed app. Suite: 555 tests,
  OK (skipped=2).

## 2026-10-02 — Juggernaut dogfood + animation fix (main)

- Dogfood on RTX 3080 12GB, real ComfyUI:
  - Portrait 1024x1024 auto→juggernaut: 138s cold (incl ~120s ckpt load), peak VRAM ~8.2GB
  - Landscape 832x1216: 9s warm / Interior 1216x832: 16s warm
  - Adult synthetic nude (policy-approved): 368s (incl model reload after FLUX); prompt reached workflow verbatim
  - Draft 512x512 -> flux2-klein-4b: 126s cold
  - Edit (jacket recolor) -> qwen-image-2.1: 707s (cold Qwen load + edit); edit quality confirmed visually
  - All produced valid PNGs; no OOM/offload failure on the 12GB card. est_ram_gb corrected 24->16 after routing fell back to FLUX.
- CSS: image shimmer now sweeps -45%..+45% (band fully exits card before
  looping — was +-25% so it clipped at edges); indeterminate progress bar
  reaches +400% so it fully exits right edge.
- Commits: 301a6c8 (feature), 1b9cc9b (anim fix + RAM est). CI green both.

## 2026-10-02 — Nexus Answer Memory (learned Q&A fast path)

- New package `localcodeagent/answer_memory/` (schema, migrations, store,
  normalization, embeddings, retrieval, confidence, ttl, validation,
  feedback, invalidation, learning, service). SQLite at
  `data/nexus_brain/answer_memory.db`, WAL, per-operation connections so the
  file is never held open; in-memory answers/aliases snapshot serves lookups
  in ~0.1 ms; stat writes deferred + batch-flushed.
- Pipeline: deterministic tier-0 → exact → semantic memory lookup → Brain →
  utility → deep model. Trusted hits skip `refresh_hardware` + all model
  loading (`response_source="answer_memory"`, "Answered from memory" badge,
  timeline activity). Possible-band matches inject as advisory context.
- Embedder `hashed-ngram-v1` (deterministic, no download) + hard gates:
  proper/digit tokens, antonym pairs (start/stop, enable/disable…), and
  symmetric canonical swaps (image/voice, France/Italy, 14B/30B) all block
  at −1. Deep paraphrases fall to `possible` by design.
- Trust: observed→candidate→trusted/verified; user learn/correction store
  directly as trusted. Corrections invalidate the old answer.
  Confirmed paraphrases self-learn as aliases (equivalent model answer ⇒
  new phrasing becomes an exact hit next time).
- Freshness: live questions never bypass; repository/config-dependent rows
  go stale on HEAD/config-fingerprint change; TTL per answer class.
- Secrets refused before persistence; no chain-of-thought stored; corrupt
  DB quarantined (never deleted); export/import supported.
- API: GET `/api/answer-memory` (+`/export`), POST learn/forget/
  mark-incorrect/update/merge/refresh/clear/rebuild-index/vacuum/import.
  Thumbs feedback feeds trust scoring. `stop_state` flushes pending writes.
- UI: `/answers.html` Learned Answers page linked from every nav; 🧠 Learn
  button on each assistant message.
- Tests: `tests/test_answer_memory.py` (48). Full suite 606 passing
  (2 env skips) on Windows. Bench: exact ≈0.08 ms, semantic ≈0.13 ms @200.
- Docs: `docs/ANSWER_MEMORY.md`; README/PROJECT_STATUS/this file updated.

## Nexus Autonomy checkpoint — persistent mission manager

- `localcodeagent/autonomy/` landed: durable mission store (`data/autonomy/`,
  schema v1), task DAG with leases + resource locks, bounded event-driven
  supervisor (tick-based, bus-woken — no busy loop), planner, evaluator,
  recovery playbooks, policy engine + standing grants, scheduler
  (once/interval/daily/weekly with missed-run catch-up), triggers
  (file_changed/startup/ci_*/custom, workspace-confined watches, debounce),
  notification center (policies + quiet hours + dedupe), standing goals.
- Supervisor yields the agent lane to interactive work (`lane_free` gate);
  sensitive actions need standing grants or mission approvals; `stop
  autonomy` pauses everything immediately.
- `MissionStore._recover_orphans()` re-parks mid-execution missions on
  restart — running nodes return to `ready`, completed work never repeats.
- Server: `/api/autonomy/*` endpoints; chat commands "make this a mission",
  "stop autonomy", "resume autonomy" in both streaming and non-streaming
  chat (no coding model needed); Missions UI at `web/missions.html`.
- Bug fixed during dogfood: resource lock released under a different owner
  than the claimer stalled missions forever — release now uses the claiming
  node id.
- `tests/test_autonomy.py`: 49 tests. Full suite: **698 passing** (2 env
  skips). Docs: `docs/AUTONOMY.md`.
- Still open: Brain/Answer-Memory hooks into plan/eval/learn are
  shallow; async job dependencies (image/model-install) not yet DAG node
  kinds; unattended multi-hour dogfood + Windows CI/installer run pending;
  canonical VERSION/release machinery is the next milestone.

## v0.7.0 platform expansion checkpoint — commits d17ab56 + 05456c2

- Canonical versioning restored: `VERSION` (0.7.0) → `version.py` →
  `scripts/sync_version.py` derives pyproject/.iss defaults/.csproj/
  backend_version.txt/.agent/project.json; `--check` enforced by tests;
  build_windows.ps1 syncs before compiling and bundles VERSION.
- Platform wave implemented (see CHANGELOG.md): computer_use tools
  (permission-gated, audited), stdio LSP client+pool, incremental RAG
  index, multi-agent git worktrees with conflict-abort merges, EvalLab +
  experiments, artifact registry, versioned backups w/ verified restore,
  DPAPI-wrapped vault key + pattern redaction, connector framework,
  knowledge graph (SQLite), plan simulation, DigitalTwin predictions,
  health service, two-way voice scaffold (Vosk/faster-whisper optional).
- New endpoints: /api/health /api/twin /api/artifacts /api/skills
  /api/connectors /api/knowledge /api/rag /api/lsp /api/eval/history
  /api/experiments /api/backups /api/simulate.
- AppState holds all services; SQLite/subprocess services (RAG,
  knowledge graph, LSP pool) are lazy and closed by stop_state.
- New permissions: computer.observe / computer.control (ask-default).
- Suite: **765 passing** (2 env skips).
- Still open for follow-on 0.7.x releases: autonomy executor should route
  through sandbox_run + RAG context; UI surfaces for health/artifacts/
  backups/skills/eval; mission-node kinds for async jobs; voice STT
  needs audio deps installed + mic device selection UI; Windows desktop
  CI/package run on the 0.7.0 line.

## v0.7.1 perf + timeline checkpoint — commits 6986514 → b6e2572

- Tuner: bounded sweep (batch/ubatch, threads, FA, KV q8_0, draft),
  mark_bad blacklist for OOM/crash configs, classify_launch_error,
  launch fallback ladder tuned→heuristic→bare, context classes +
  ensure_ready(min_context) relaunch for bigger windows.
- Telemetry: cold/warm split, context, cached_tokens, launch surface;
  generation_summary reports cold vs warm TPS + cache hits; twin fed.
- Timeline: mission_id+progress on rows, /api/activity?mission_id=,
  rollup summary per task, mission node rows (task_graph category),
  RECOVERING TASK rows on auto-resume, UI mission chip + progress bar.
- Idle-gated auto-tuner (`55c7d66`): benchmarks untuned resident models
  when the machine is idle; winning configs persist.
- Worktree merge carries explicit git identity — fixes the Linux-runner
  `empty ident` CI failure.
- Autonomy supervisor sandboxes generated node verify commands
  (`Sandbox.run` with repo cwd); `repo_search` tool exposes the
  incremental RAG index to agents (`2df6870`).
- Windows package workflow resolves VERSION at run time — no more
  hardcoded `0.6.0-dev` in Inno defines or artifact names (`b6e2572`).
- Version 0.7.1 synced across artifacts. Suite: **789 passing**
  (2 env skips). GitHub Actions green on `55c7d66` including the
  windows-desktop installer smoke job; final run `36982678215` green on `a44905c` (8m21s, includes installer build + update-preservation smoke).

## v0.7.2 reliability + UX wave — commits 9f3d13f → 9aa3fcb

- Durable state via junctions: `data`, `.agent`, `output` →
  `%LOCALAPPDATA%\NexusCore`; `models` + `ComfyUI_windows_portable` →
  `<install-drive>:\NexusCore` (Program.cs StateTargetRoot; build script
  mirrors both tiers before wiping dist, never traverses junctions).
  Rebuilds can no longer wipe chats, state, or 71GB of models.
- Sidebar: one canonical nav rail on all 11 pages; page controls moved to
  main/right-rail surfaces (`48ca2b5`). System page gains a Diagnostics
  panel fed by `/api/diagnostics` crash_history (`ed3679d`).
- Watchdog auto-restarts now persist to crash_history + component health
  (`a4fadf8`); host records backend exits to crash_history.jsonl too.
- `[nexus-port]` stdout marker — host health-checks the backend's actual
  bound port (port-race fix).
- Mid-stream failure UX: friendly message + diagnostic details +
  one-click retry; SSE error events carry the full payload (`4fcea08`).
- `color-scheme: dark` + themed `select option` — readable dropdowns
  (`9f3d13f`).
- Voice sync: response display holds until first TTS segment when voice
  is on (8s cap + mute/stop release); muted = instant text. Streamer
  emits first clause ~90 chars (`cc86378`).
- Speech filter translates status glyphs to verdicts — ✅/✓/[x] →
  "operating within normal parameters", ❌/✗/[ ] → "failed to
  initialize" (`8345f71`). Hard-coded in `speech_filter.py`.
- Identity: `creator_answer()` states the fact naturally; lock wording
  only on write-attempt refusal (`34b1182`).
- Permissive conversation: expanded `generic_topic_refusal` detection
  (can't assist/provide, not appropriate-or-safe) w/ buffered retry;
  `hard_specific` narrowed to illegal-content-only refusals so real
  restrictions aren't retried away (`9aa3fcb`).
- Runtime verified live: 5 junctions, all models resolve, ComfyUI
  healthy through junction, 5 conversations + 146-msg history intact.
- Suite: **868 passing** (2 env skips).

## v0.12.x checkpoint — profiles, personality, installer update UX

- **v0.12.0 Profiles + Creator Identity + Personality Studio**
  (`0fe5756` → `9cc450e`): UUID-keyed profiles under `data/profiles/<uuid>/`
  with immutable identity and per-profile personality/voice/avatar/settings/
  personal-memory. First-run onboarding lock (`403 onboarding_required`,
  `web/start.html` + `profile.js` nav guard). Creator auth via reserved
  normalized name `John Hamburn` + PBKDF2-HMAC-SHA256 bootstrap→enrollment,
  persisted exponential backoff, neutral errors — passcode never in repo
  (tests derive it as `"0" + str(3211977)`). 73 presets / 47 sliders /
  Personality Strength / 7 moods; adult gating backend-enforced from
  birthdate only. Voice delivery map rides real engine params; personality
  prompt context injects bounded delivery-style hints. ZIP entry is manual
  only (state/city dropdowns stay). 66+9 profile tests.
- **v0.12.1 installer update progress** (`0d3a765`, `078ccdf`): Inno Setup
  previously did multi-minute blocking work with a static wizard — killed
  twice mid-SHA-256 in real logs. Now `ShowBusyStatus` pushes stage text +
  `npbstMarquee` bar to whichever page is active (Ready page via
  `ReadyLabel`, Installing via `StatusLabel`/`FilenameLabel` + model
  progress controls); `BusySleep` chunks grace sleeps so the wizard keeps
  repainting; uninstall wait shows elapsed seconds; download progress
  covers all four bootstrap downloads and flips to "Verifying download
  (SHA-256)" at 100%. Contract test
  `test_installer_reports_busy_stages_during_blocking_update_work`.
- Suite: **1180 passing** (2 env skips); CI green on `042be1c`
  (test 1m54s + windows-desktop 6m32s incl. real ISCC compile and
  install→update-twice smoke with fake-process kill).
- Known item: unsigned setup.exe still gets SmartScreen/Defender
  pre-launch scanning — fix is `NEXUS_CODESIGN_*` secrets, already
  supported by the workflow.

## v0.12.x checkpoint — startup lock-stall crash loop root-caused and fixed

- **Root cause** (`8f164a1`): `ensure_ready()` held the runtime manager's
  global `_lock` across the entire `_spawn_and_wait` llama-server load
  (up to 180s × 3 fallback attempts). `/api/status` calls `statuses()`
  under the same lock → health probes timed out → host killed the
  backend → interrupted-task auto-resume retried on next boot →
  permanent crash loop. Boot prewarm (`runtime_auto_start`) triggers the
  same path. Fix: per-model `_starting` claim + `_starting_cond`, model
  launch waits run lock-free with shared-state mutations in short lock
  sections, `statuses()` does a bounded acquire and returns the
  last-known snapshot under contention. Regression tests
  `test_statuses_answers_while_model_load_in_flight` +
  `test_statuses_falls_back_under_lock_contention` (`d7f8ed1`).
- **Host** (`adae6c0`, `fdf0c08`): writes `backend-host.log` before
  `Process.Start` (launch failures were leaving an empty log), logs
  health-probe state every 15s + last HTTP/error on timeout, reaps
  orphaned backends by exe path not just pidfile, health timeout 2s→10s
  and UI probe 5s→15s. `ReapOrphanedBackend` validates PID reuse against
  the executable path and kills the process tree.
- **Installer** (`adae6c0`): second inert progress bar removed — native
  Inno gauge only; kill escalation name→pidfile→PowerShell path sweep of
  any process running under the install dir; `FileIsWriteLocked` probe
  on `NexusCore.exe`/`ChatNexus.Backend.exe`/`llama-server.exe` with a
  45s live-countdown wait before Inno touches files (fixes "DeleteFile
  failed; code 5" when a backend lingers); stage text covers the file-
  analysis gap between Update click and extraction start.
- **Live diagnosis**: real setup logs showed update runs #003/#004
  killed mid-SHA-256 of `kokoro-v1.0.onnx` (~5s in, static wizard) —
  the "frozen" update was the silent hash, now narrated. "Could not
  start" traced to the lock stall above; a stray test backend produced
  the delete-file-5 update failure and was the concrete repro.
- Emergency unblock applied to the installed copy only (NOT repo):
  `D:\Nexus_Core\config.json` `runtime_auto_start: false` + two stuck
  tasks' `recovery_count` capped past the resume budget. Self-test went
  120s timeout → healthy in 5.9s. `runtime_auto_start` was restored to
  `true` after the fixed build installed and self-test passed again.
- **Post-install silent launch failure** (`d6b61dd`, `46e1ba2`): Inno's
  Run entry fired ~2 s after files landed, inside Defender's on-access
  scan window for the fresh ~200 MB backend exe. `File.Exists` returned
  false → `FileNotFoundException` → splash failure screen, and the check
  ran before the first log write so `backend-host.log` stayed EMPTY.
  Host now writes `backend start requested` first + waits up to 30 s for
  the exe to settle, and `Main` writes a first-breath
  `host process started` marker (distinguishes "exe never ran" from
  "died before backend launch").
- Suite: **1183 passing** (2 env skips); CI green on `8f164a1`,
  `5af1346`, `fdf0c08`. Latest fixed installer artifact verified at
  `dist/installer/NexusCore-Setup-0.12.1-Windows-x64.exe` (sha256
  `65bf088aff885a21b711232bfd8a9f435b458fea139563f0dd17b3f5ac0c0626`).

## Post-milestone ops notes (858e51a9)

- **C: drive hit 100% (119 MB free)** during this session — supervisor's
  disk guard correctly paused autonomy test missions ("disk nearly full").
  Purged `pip cache` (~5.1 GB). If missions mysteriously pause on this box,
  check `C:` free space first — the guard reads the tempdir drive.
- **CI flake fixed**: `test_mission_approval_timeouts_are_bounded` /
  `test_mission_approval_timeout_replans` raced on slow runners (0.2 s
  wall-clock timeout could fire inside `drive()`'s settle loop). Now
  deterministic: callable timeout held at 0 during drive, pending row's
  `created_at` backdated to expire on the next tick. `858e51a9`.
- **Still pending**: one Nexus Core restart to swap in `backend-new`
  (voice fix + provisioning hardening + shutil fix, `update.flag` set).

## Frozen-build first-boot dogfood (9a76557c, ae63f461, 84191c3c)

Booted `dist-fresh2/ChatNexus.Backend.exe` on a scratch workspace with
provisioning enabled — the real first-run path — and caught two live bugs
the mocked tests missed:

- **voice-assets crash**: `_run_voice_assets` callback expected
  `(name, done, total)` but `ensure_assets` calls `progress(name, done)` —
  the first plan item died with TypeError on every fresh install. Fixed;
  regression test stubs the real call signature. Verified live: after the
  fix, voice-assets (353 MB) + whisper-stt downloaded and SHA-verified
  `completed` in the frozen bundle.
- **permission_denied was terminal**: InvokeAI venv install hit Errno 13
  on `Scripts\python.exe` — Defender on-access scan holding a fresh exe.
  Added to TRANSIENT_ERRORS (bounded to MAX_ATTEMPTS; real ACL problems
  still report permanently).
- **Fleet honesty verified**: with the invokeai item failed, all three
  fleet models went `skipped — dependency did not install` rather than
  downloading 21 GB to nowhere.
- **Staged-dir pollution cleaned**: earlier smoke runs of the frozen exe
  with a relative `--config` wrote runtime state (models/, data/, tools/,
  .agent/) into `backend-new/` — removed; dir now holds only exe +
  `_internal`, matching `backend/`. Never run the frozen exe without an
  absolute `--config`/`--workspace`.
- `backend-new` rebuilt from `ae63f461` and restaged (full PyInstaller
  args; `dist-fresh2` removed after staging).

## Tool-install verification + manifest detection fix (7153c5c3)

- `_verify_tool` was a stub that always passed — now wired through
  `tool_installed_hook` → `ToolRegistry.refresh_install_status()` so a
  "successful" job that left nothing on disk fails the item honestly.
- **Pre-existing manifest bug found**: `detect.files` required ALL listed
  paths, but `invokeai.json` listed Windows + POSIX alternates — InvokeAI
  always reported `missing` even when installed. New `detect.files_any`
  is any-of for platform alternates; `comfyui` keeps all-required
  (main.py + embedded python.exe both must exist).
- Verified against real trees: invokeai@dev=True, comfyui@deployed=True,
  whisper@deployed=False (honest — not installed there).
- `backend-new` restaged from `7153c5c3` — ships voice-assets fix,
  permission retry, real verify, and manifest detection.

## Runtime orphan adoption (5e68048d)

Backend restart previously killed healthy llama-server orphans and
reloaded the same multi-GB checkpoint. `_start_llama_cpp` now probes the
port listener first: healthy + serving the same model file (`/v1/models`)
+ sufficient context (`/props` n_ctx) → adopted into `_managed` through
`_OrphanProcess` (pid-backed duck type — poll/terminate/kill/wait all
act on the real process, so stop/watchdog/eviction work unchanged).
Wrong model, unhealthy, or too-small ctx falls back to reclaim+spawn.
Note: the two live orphans run n_ctx=8192 while profiles default 32k —
they'll correctly be replaced on restart, not adopted.

Amendment (43de6137): the original edit stranded `_listening_pids`'s body
as dead code — every llama launch on the 5e68048d build would have hit
AttributeError. Fixed + hardened: managed_pids uses getattr(pid), the
adoption call site is try/except so a probe bug can never block a
launch, and tests mocking `_reclaim_orphaned_port` now also mock
`_adopt_healthy_orphan` (they were reaching real netstat + the live
:8080 server). `backend-new` restaged from 43de6137 — the prior staged
build contained the break and has been replaced.

## InvokeAI ephemeral-state sweep + self-heal (d22b8419, ddeb50ee)

- `python -m invokeai.app.run_app` exits code 0 silently (no __main__
  entry) — the discovery fallback when invokeai-web.exe isn't visible.
  Fixed: call the console-script entry via -c (run_app / invoke_ai_api).
- InvokeAI's ephemeral ObjectSerializerDisk uses ONE TemporaryDirectory
  under outputs/tensors for the process lifetime; ANY second InvokeAI
  on the same root deletes all tmp* dirs at startup and bricks it
  ("Parent directory ... does not exist" on every generation). A test
  spawn sharing the junctioned root (D:\Nexus_Core\data\invokeai →
  D:\Devin\chat-nexus\data\invokeai) did exactly this live.
- Manager now detects the signature → restarts backend → resubmits
  once; repeat failures report honestly (ddeb50ee).
- Deployed backend updated to 6b8b4f69 at 03:02 restart; backend-new
  restaged from ddeb50ee + update.flag rewritten for next launch.
- 05 Oct follow-up: backend-new rebuilt from a789403e (adds InvokeAI
  installable-flag fix, provisioning reconcile self-heal, comfy
  imagemodel-* auto-download items, round/face-centered Isabella chat
  avatar) + update.flag rewritten. NOTE: data/lkg/rollback.flag is also
  pending ("5 consecutive unclean boots" → snap-1791165159653) — it
  applies first at next launch, then update.flag swaps in backend-new.

## 2026-10-08 — v0.34.0 Full Verified Computer Control + voice warmup fix

**Action lane increments (all verified, deterministic):**
- App control: resolve_app (path/PATH/well-known/App Paths/Start Menu),
  launch/close/restart/status/window with pid+window evidence; live
  dogfood: 'close notepad' parked on application.manage, approved,
  WM_CLOSE, process gone.
- Durable downloads: UserDownloadManager (.part + Range resume +
  sha256 + cancel + JobManager progress); 'download <url> [to X]'.
- File discovery: find/search/inspect/hash intents; fs_stat,
  fs_archive, fs_extract (zip-slip + self-containment guarded).
- Installer lane: 'install <path|product>', 'run the installer';
  ShellExecute for elevation (UAC to the user), pid+image monitoring,
  uninstall-registry diff as install proof.
- UIA layer: computer_ui_observe/find/click/set_text/wait via bounded
  PowerShell UIAutomationClient; patterns before pixels; LoopGuard;
  screen-fingerprint re-observe; base64-JSON transport.
- Queue fix: waiting_approval no longer wedges dequeue/chat gates.

**Voice latency fix (user-reported):** worker spawn now overlaps model
thinking — begin_task prewarms the active preset's engine, boot
prewarms once VRAM headroom is confirmed (post orphan-sweep), and the
GPU idle leash default rose 120s -> 600s so replies after short pauses
don't re-pay the ~9s spawn. VRAM-pressure unload still wins.

Checkpoint: **2911 tests** (2911 passed + 3 env skips).

## 2026-10-05 — GitHub account API + voice-drain close + windowing (LIVE dogfooded)

### GitHub account management (commits c3290805, a9ae38d8, 052ad13a)
- New `localcodeagent/github_account.py` — single account-state surface:
  env→vault credential resolution, /user validation + scope capture,
  differentiated states (not_configured/connected/invalid_token/
  permission_blocked/network_error/vault_error).
- `/api/github/{status,connect,disconnect,repos,test}` on the frozen backend.
- Settings → Connections panel (`web/settings.js`).
- connect/disconnect moved github.write → credentials.use; connector +
  shared client refresh live without restart.
- git_push + github_clone: basic base64(x-access-token:token) header auth
  — 'bearer' is REJECTED by git smart-HTTP for OAuth (gho_) tokens.
- LIVE EVIDENCE (port 58698/49952 backends):
  - status→not_configured; connect→`connected as afterburn25`, scopes
    [repo, workflow, ...]; repos→real repo list; test→ /user + repos +
    workspace perms (push/admin) + 3 Actions runs — all ok.
  - Connector status: authed=true credential_source=vault, no restart.
  - RESTART persistence verified: status still connected after relaunch.
  - Push: header-auth push of test/nexus-github-dogfood-1791226213 to
    afterburn25/Coding_Agent succeeded; token count in .git/config = 0;
    remote branch deleted (204).
  - disconnect→removed_vault_token=true; status immediately not_configured.
  - Token never in logs/config/activity (grep-verified).
- KNOWN DEBT: D:\Nexus_Core\Source checkout is corrupt ('bad tree object
  HEAD', missing objects) — predates this work; needs a fresh clone.
- 23 regression tests in tests/test_github_account.py.

### Voice overlap on shutdown + interface handshake (commit 2e91d461)
- web/voice_global.js reports busy/idle via voice-state webview messages;
  _draining latch stops new clips on shutdown, in-flight ones finish.
- Host waits for queue idle before farewell (75s bound). LIVE TRACE:
  close mid-clip → drain waited 3626ms → busy=False → THEN
  'shutdown' + 'goodbye' — zero overlap.
- WebMessageAsJson fix: object posts never matched the old string API —
  voice-state would've been silent AND nexus-core-ready was falling
  through to the 15s fallback EVERY boot. interface_ready: 15.2s → 5.4s.
- Splash: 500ms HWND_TOPMOST re-assert watchdog (Windows can demote
  borderless windows shown without foreground rights); main-window
  handoff uses attach-thread-input + SetForegroundWindow.
- BUILD GOTCHA: PyInstaller must run with repo root as cwd —
  pathex=[] in the spec relies on it; building from build/ produced an
  exe missing localcodeagent entirely (ModuleNotFoundError on boot).

## 2026-10-07 — Chatterbox Isabella dry-reference reset

- Isabella Chatterbox conditioning now uses the dry public Kokoro `bf_isabella` sample directly from `reference-source.mp3`; the prior processed `reference.wav` is deliberately removed so it cannot silently reintroduce the old V6 DSP coloration.
- `nexus-isabella-chatterbox.json` is now a clean post-generation chain: no neural delay, no micro delay, no glass echo, no ambience, no stereo widening; low-mid cut + presence/air + a very light high-band synthetic sheen.
- Loudness target is -12.5 LUFS with 0.891 peak limiter ceiling.
- If tuning further, add synthetic texture one layer at a time. Do **not** bake DSP back into the conditioning reference.

## 2026-10-07 — Isabella V7 target approved

- User approved the V7 audition derived from the original pre-Chatterbox `isabella-nexus-v6-enhanced-synthetic` sound.
- Keep Chatterbox conditioning **dry** (`bf_isabella` source). Do not revert to the processed V6 reference.
- Restore the V6 synthetic character only as a **single post-generation DSP pass**: neural/glass/micro layers + original tonal lift. This is intentional; the prior barrel defect was double-processing (processed reference + another pass), not the V6 character itself.
- Center output (`stereo_width=0`) and keep ambience off. Loudness target: -12.5 LUFS, limiter ceiling ~-1 dBFS.
- Preset signature: `approved-v7-v6-character-louder`.
