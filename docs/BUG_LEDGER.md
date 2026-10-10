# Bug Ledger — product-wide polish/QA pass

Format per the milestone contract: ID · severity · subsystem ·
reproduction · root cause · fix · regression · live-verified · commit.
Severity scale: P0 (data/state/credential/false-success) · P1 (broken
function/routing/stuck/perf) · P2 (usability/messages/latency) ·
P3 (cosmetic).

| ID | Sev | Subsystem | Defect | Root cause | Fix | Regression |
|----|-----|-----------|--------|------------|-----|------------|
| BUG-001 | P1 | API (`server.do_GET`) | Any in-handler exception propagated to `BaseHTTPRequestHandler` — connection aborted, client gets no JSON body at all | `do_GET` dispatch had no outer try/catch (do_POST had one) | `do_GET` → thin wrapper over `_do_GET()` routed through shared `_api_error` | `test_api_errors.test_get_handler_fault_returns_json_500`, `..._is_403` |
| BUG-002 | P1 | API (`server.do_POST`) | Malformed JSON body → `json.JSONDecodeError` fell into generic `except Exception` → **HTTP 500** for a client error | body parse inside the same try as dispatch; no `JSONDecodeError` clause | `_api_error` maps `JSONDecodeError`/`_ApiBodyError` → 400 `invalid JSON body` | `test_api_errors.test_malformed_json_is_400_not_500` |
| BUG-003 | P1 | API (`server._body`) | Valid JSON but non-object body (`[1]`, `"x"`, `42`) → `body.get` AttributeError → 500 | `_body()` returned any parsed JSON; all routes assume dict | `_body` raises `_ApiBodyError` for non-dict → 400 | `test_api_errors.test_non_object_json_is_400_not_500` |
| BUG-004 | P1 | API query/body casts | `int(q.get("limit")…)` ×8 GET sites and `float(body.get(…))`/`int(body.get(…))` ×27 POST sites — garbage numeric input → `ValueError` → 500 | bare numeric casts on client-controlled strings | `_qint`/`_bnum` helpers: garbage → bounded default (never 500); all 35 sites patched | `test_api_errors.test_garbage_numeric_fields_fall_back_not_500` |

## Discovery queues (carried)

- Per-clause numeric type errors *inside* individual handlers remain
  mapped 500 only where genuinely a server fault; query params are
  covered by `_qint`, body fields by `_bnum`.
- `ValueError` raised by internal logic (not input coercion) correctly
  stays a 500 — do not widen the 400 map.
- Part 4 dead-code sweep, Part 6 tool fuzzing, Part 7 permission
  confusion matrix, Part 28 false-success audit: in progress.
