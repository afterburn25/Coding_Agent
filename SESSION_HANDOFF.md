diff --cc SESSION_HANDOFF.md
index 4e4b5fa6,e0fa4f13..00000000
--- a/SESSION_HANDOFF.md
+++ b/SESSION_HANDOFF.md
@@@ -4521,28 -4521,3 +4521,31 @@@ inside a double-quoted Write-Host strin
  UTF-8-no-BOM file as ANSI (byte 0x94 → stray quote). ASCII hyphen
  restores it. Deploy completed rc=3, app relaunched, v0.42.0 live on
  port 63356 with all Phase A endpoints verified.
++<<<<<<< Updated upstream
 +
 +Image resource arbitration hardened (commit 722b1406):
 +- Live incident: ComfyUI job sampled an SDXL checkpoint then died at
 +  VAEDecode with cudaErrorUnknown / "fatal : Memory allocation failure"
 +  on the saturated 12 GB card.
 +- Root causes: (1) release_managed_models_for_* only saw _managed
 +  entries — llama-server orphans surviving a backend restart were
 +  invisible to eviction until a chat adopted them; (2) both image lanes
 +  proceeded to backend startup even when VRAM/RAM stayed below the
 +  job's estimate after eviction.
 +- Fix: _adopt_orphans_for_pressure sweep claims healthy orphans
 +  (endpoint-port fallback when profile.port=0) inside the release
 +  paths, only when memory is already short. Both lanes then poll up to
 +  3s for teardown to settle (only when something was evicted) and
 +  raise insufficient_resources at admission instead of crashing
 +  mid-decode. cudaErrorUnknown now maps to cuda_out_of_memory.
 +- InvokeAIBackend default timeout 4s -> 15s; three stale mission-added
 +  tests (605ba3f7) repaired to match reality.
 +- Verified: 223 targeted tests + full suite 3192 tests OK (skipped=4).
 +  Deployed, live on port 58325. Dogfooded a Juggernaut job while a
 +  mission held a model busy: arbiter evicted qwen3-14b, correctly
 +  refused the busy model, and the job failed honestly with
 +  insufficient_resources ("need 10.0 GB free, have 3.0 GB after
 +  evicting qwen3-14b") — no CUDA fault — then restore_managed_models
 +  relaunched the evicted LLM.
++=======
++>>>>>>> Stashed changes