# Guest checkout reservation and release gates

## Scope

This change routes the public `POST /buy/{shop}` through one durable guest/order reservation and the existing per-order payment attempt. It is stacked on PR124, independently of the renewal-preview and password-recovery branches. It does not merge or deploy the stack, enable gateways, select financial policy, or contact real providers during validation.

A signed form and matching HttpOnly cookie are required; old tokenless bookmarks/scripts POSTing directly must first GET the store. A normal GET/reload preserves the same form identity. An explicit `new=1` / Buy again action starts another intentional purchase; independent browser identities are not globally deduplicated. Two different deliberate intents can buy the same plan.

## Durable boundaries

- HMAC-SHA256 with a purpose-separated master-key namespace; random 32-byte nonce; shop and issue time bound; 30-minute absolute validity. Bad/future/expired/missing/mismatched forms cause no provider call. Production/HTTPS form cookies use the `__Host-` prefix, Secure, HttpOnly, SameSite=Strict, Path=/ and no Domain. HTTP development uses a non-prefixed cookie. The separate purchase return cookie remains SameSite=Lax for gateway returns.
- Only a SHA-256 form digest is stored. A unique database insert reserves the identity and serializes first requests. The guest, order, and identity commit together in a private session before starting any payment. Rollback of the HTTP caller cannot remove the reservation.
- Plan ID and method are immutable per identity. Replays do not create a new customer, reprice the existing order or silently switch provider. Row/customer/order/shop ownership is checked again. Claimed, banned or removed guests and missing/moved orders fail closed.
- The inherited payment helper persists a CREATED/uncertain attempt before provider creation. Timeout/cancellation/process death never permits a replacement payment on the same order. Ready snapshots replay. An error does not prove the provider rejected the payment.
- PaymentError/TimeoutError after reservation returns the owned private status page, not another checkout. Cancellation/unexpected errors propagate, leaving committed evidence for retry/review. A crash between reservation and payment can resume the same order by submitting the retained form.
- Guest pages require an exact saved order ID/amount/provider/currency fingerprint and use the existing saved checkout projection for new durable attempts. Uncertain snapshots never show redirect instructions or an auto-redirect, even if legacy metadata contains a stale URL. GET never creates/checks a provider invoice. Historical guests keep only their existing sanitized HTTPS links; they are not adopted into the new creation protocol.
- `guest_checkout_intents` retains tombstones when Customer/Order is deleted (SET NULL); a claim also never frees the identity. No automatic tombstone pruning is added. A shop deletion cascades its identities; do not manually reuse shop IDs. Only hashes/IDs/method/timestamp are stored, not browser or purchase tokens.

## Deliberately not closed

Legacy internal `core.guest.start`, bot checkout, unkeyed top-ups and non-participating callers are unchanged. Do not claim all-channel exactly-once charging. Unknown-reference recovery from authenticated provider evidence, manual resolution of uncertain invoices, late renewal guarantees, trusted caps and step-up rules are separate work. No invoice is auto-refunded/cancelled/replaced. Provider I/O and PostgreSQL are not distributed-atomic.

A user can deliberately request another identity; this is not a hard global purchase cap. The store page for a retained reservation shows only its status/resume actions, not a Buy again invitation. An uncertain first payment must be reviewed before instructing a customer to buy again. No availability promise is made for a lost/expired browser form or a disabled shop; the saved private purchase link/return cookie remains the status route where available.

## Validation evidence

- Local validation: 11 actual-helper SQLite scenarios, 13 signed-token tests, 11 actual-router HTTP scenarios and one isolated Alembic SQLite migration test passed. Ruff, format, compile and dependency-skipping targeted mypy also passed. Catalogue/order-draft, authentication and unrelated provider dependencies use controlled seams; this is NOT real PostgreSQL concurrency or full-app QA.
- Committed security regressions use independent PostgreSQL commits and the existing disposable-schema fixture: visible reservation/attempt before provider I/O, in-flight and simultaneous-first retries, immutable plan/method, timeout/cancellation, reservation gap, claim/delete tombstones, invalid choice rollback. Existing full-app guest tests obtain real form tokens; added HTTP cookie/replay/uncertain-page checks.
- Do not report CI green until the exact pushed head has passed. No real Telegram, mobile browser, provider, PostgreSQL/full-chain migration or staging QA has run locally.

## Full-app CI fixture correction

The user-supplied log for head `81ed5f0` contains 4 failures / 1236 passes, all in
`tests/unit/test_guest.py`. The shared global test fixture binds all sessions to one
connection and nests their "commits" inside savepoints. A read-only caller rollback can
then undo the inner payment writer's released savepoint; that is not independent durability.
A controlled actual-router/SQLAlchemy reproduction with that transaction topology yielded
customer/order/payment/intent counts `[1, 1, 0, 1]` and no gateway redirect, matching the
observed missing-payment symptoms. No production provider request was made.

The guest HTTP module now uses a fresh UUID-named PostgreSQL schema per test, migrated
with the existing `_migrate` helper, and a maker bound to its engine (not one connection).
The sessionmaker is reset and that specific schema is dropped during teardown. Other
modules keep their original rollback fixture. No payment implementation or safety checks
were relaxed. The full purchase/claim observer refreshes its cached order/payment/customer
after separate HTTP connections commit, before running provisioning/checking spent totals.

A new HTTP regression checks independent visibility of reservation/payment before mock
provider I/O, preservation through observer rollback, no accidental observer writes, and
one provider call across replay. It passed locally using the actual test body with the
SQLite/router/model/provider seams; the previous 11 actual-router scenarios passed again.
Ruff/format and compile passed. These are NOT a local run of the PostgreSQL fixture or the
full 1240-test app suite; the new exact-head GitHub run is required for that evidence.

## Migration / staging gates

1. Apply inherited migrations and `0027_guest_checkout` in an isolated empty database and a restored test backup; verify a single Alembic head after combining sibling branches. Reconcile any sibling migration with the same number explicitly, never renumber blindly.
2. Stop guest creation before any downgrade. Downgrading drops replay tombstones; the old route would again permit duplicate provider creation. Do not treat schema downgrade as a safe financial rollback. Retain the new table/evidence during an application rollback until all outstanding invoices and form lifetimes are resolved.
3. Run complete Python security/unit suite and frontend CI on the combined branch. Tests/conftest resets public schema: ONLY disposable test DBs, never live staging/prod data.
4. Exercise actual HTTPS production cookie prefix, same-origin form POST, reload, double-click, stale tab, explicit new purchase, gateway cross-site return, Android/iOS browsers, lost response, process restart during provider creation, callback before response save, staff review and Telegram claim. Use sandbox providers and staging bot, never real charges.
5. Verify monitoring/log redaction never records hidden form identities, private purchase URLs or provider secrets. Set release/rollback owner and review customer recovery for ambiguous invoices before activation.
