# Gateway callback operation IDs — F-28 (partial)

## Change and dependency

Stacked on PR #93 (`fix/mender-streamed-callback-limits`,
`ad2016864dae409d1a6f8d6a3286b5a7efbdec1d`). Keep its bounded streamed body reader.
The existing multi-method `/api/pay/gw/{name}` route gives GET and POST the same generated
OpenAPI operationId. Set iteration also made the old generated suffix unsuitable as a
stable client-generator contract. The schema now explicitly uses `gateway_callback_get`
and `gateway_callback_post`. Regenerate any generated client that consumed the old IDs.
No HTTP path rename, request payload migration, provider or financial change is made.

FastAPI applies decorators bottom-up. The original combined GET/POST dispatch registration
remains **first**, excluded only from the schema. Two method-specific registrations of the
**same callable and dependency** supply the schema. This deliberately adds two raw route
registrations, not two new URL operations or implementations. Keeping the combined dispatch
first also preserves the old unsupported-method 405 `Allow` set containing both GET and POST;
a naive two-route split would return only one of them with the tested Starlette versions.
Do not move the combined registration below the single-method registrations at runtime.

The handler body, imports and the rest of pay.py have identical ASTs to #93 once only this
function's decorators are removed. No verification, throttling, commit, fulfillment,
redirect, raw signature bytes or body-limit implementation is changed.

## Regressions

Run in a full project checkout with its normal dependencies and isolated test services:

```sh
python -m pytest -q tests/contract/test_payment_callback_openapi.py
ruff check paurubot/api/routes/pay.py tests/contract/test_payment_callback_openapi.py
ruff format --check paurubot/api/routes/pay.py tests/contract/test_payment_callback_openapi.py
```

The 22 cases check payment-router schema uniqueness, explicit repeatable method IDs,
registration order and shared endpoint/dependency, unchanged single-method callback IDs
and parameter aliases, unknown gateways, unsupported methods and their combined Allow set,
GET/POST raw-byte preservation, mocked verification/fulfillment wiring, HTML GET redirect
versus POST JSON, and the inherited 64KiB streaming limit with absent/understated/oversized
Content-Length. Overflow stops before the third chunk. All provider and DB effects are faked.
These tests do not establish bank settlement, full app authorization or live-provider safety.

## Local evidence and limits

- Python 3.12.15, FastAPI 0.143.3 / Starlette 1.8.0: **22 passed**, no warning/skip/error.
  On the exact #93 source, **3 failed, 19 passed**, with duplicate-operation-ID warnings;
  no fixture errors. The three failures concern one schema/registration defect, not three
  independent vulnerabilities.
- Python 3.13, FastAPI 0.115.14: the same **22 passed** without warnings. This is a second
  execution of the same tests, not 22 additional unique tests.
- Selected-file Ruff lint/formatter and Python compilation passed. AST equivalence passed.
- This workspace contains only a partial source extraction. The local runner imports the
  exact production pay.py and body.py with **unavailable import-time business modules
  replaced by explicit stubs**; the committed test uses normal production imports in a full
  checkout. Real FastAPI/Starlette/httpx handle schema and ASGI dispatch. The test then
  explicitly replaces DB/provider effects. Thus these results are isolated route evidence,
  **not a full application boot, full checkout pytest run, full mypy run or all-app OpenAPI
  uniqueness proof**. The stub bootstrap is evidence tooling, not production code.
- Full-project CI, integration with other open PRs, real DB/Redis/provider/Telegram/staging
  tests remain unverified for this change. No frontend files changed.
- F-28 is not closed: other routes, local/CI lint scope parity and dependency locking remain
  separate work. No workflow/check weakening, billing change, merge or deployment is included.
