# Mini App wallet transfers

## Scope and dependency

This PR adds the existing same-shop wallet transfer feature to the Mini App, end to end: API, explicit recipient/amount consent, Wallet UI, durable replay, tests and release notes. It is based on #108 (`373c862f3fb401ac97ed41f4784c0f4819b77979`), which includes #96/#105. It does not include any Tonpay branch or change a payment gateway, trusted receipt policy, password policy or production setting. It does not merge/deploy its prerequisites.

## Contract

- `GET /api/webapp/wallet/transfer-policy`: fresh authenticated shop settings, minimum, maximum, rolling daily cap and currency. Disabled by default according to the existing setting.
- `POST /api/webapp/wallet/transfers/quote`: strict integer amount and a bounded Telegram ID/@username. The shared core resolves an existing non-banned, non-guest recipient in the same shop; no shop, role, balance or internal recipient ID is accepted from the client. The response shows the exact recipient Telegram ID, display fields, amount, currency and expiration. A quote does not move money.
- `POST /api/webapp/wallet/transfers/confirm`: accepts only the quote token. A purpose-separated HMAC under the existing master key binds shop, sender, recipient, amount, currency, expiry and random nonce. Consent lasts five minutes. Noncanonical/tampered tokens cannot create alternate journal keys.
- All endpoints use the existing `WebApp` dependency, including signed Telegram principal, shop resolution, join/maintenance/ban gates and per-user rate limit. Responses are `no-store`.

The confirmation locks both wallet accounts in stable order, checks for an already committed winner, then checks expiry and refreshes party bans and shop settings. `core.transfers.send` remains the sole financial implementation. Its existing ledger row locks, balance validation and rolling-24-hour limit are shared with Bot transfers. No Redis-only money state, direct balance update, new transfer fee or replacement daily policy is added.

The immutable ledger journal is the durable operation record. A lost response reuses the exact same token/key; replay returns the original journal even after quote expiry without another debit. An expired unposted token is rejected only after wallet-lock serialization and rechecking for a prior winner. Database commit precedes the successful HTTP acknowledgement. No external network operation is involved.

## UI and recovery

The Wallet form shows policy, resolves the intended recipient before confirmation, supports Persian/Arabic digits, and displays exact amount/unit and consent expiry. It shares the synchronous gate with top-up and gift actions.

The owner-scoped quote is stored in sessionStorage **before** any confirmation request. Storage failure fails closed. After a confirmation attempt, cancellation/new transfer is unavailable until a successful same-token result or server-confirmed unposted expiry. Network/5xx failures do not clear or replace the token. A reload in the same tab restores the same confirmation. Read refresh errors never replay the transfer. Owner unmount ignores late UI completion. Independent intentional quotes on different tabs/devices are not globally deduplicated; this is once-per-consent, not a ban on multiple legitimate transfers.

Master-key rotation invalidates outstanding unposted quotes. Quiesce transfers and reconcile any in-flight acknowledgements before rotation; do not advise deleting a retained uncertain token to create a new transfer. This PR does not add a master-key rotation system or claim serializable policy/ban edits against every in-flight financial operation.

## Validation

Local, isolated PostgreSQL 16 / Python 3.13:

- 38 backend tests passed, including actual concurrent transactions with independent commits: duplicate consent, distinct consents racing the daily cap, and Bot vs Mini App using the same cap.
- Balanced ledger reconciliation, insufficient funds, rollback/retry, expiry, stale settings cache, party bans, tenant boundary, malformed amounts, forged/scope-rebound tokens, and HTTP adapters.
- Local harness executes the exact transfer, account, ledger-posting, new quote/router and existing base settings modules. It uses six extracted actual ORM classes without unrelated relationships, controlled customer lookup/config/auth/unused-secret seams, and does **not** run full application migrations or signed-initData integration. The committed tests run against the real repository models and fixtures in CI. Concurrent tests use a private temporary schema and do not leave committed shops/journals in the suite's public schema.

Local Node 24:

- Exact updated `npm run test:ux`: 128 passed, zero failures/skips (including existing #108 alert/history tests and 20 new transfer tests).
- Targeted Wallet TypeScript strict check passed using the existing dependency tree and explicitly typed unrelated Payment widget seams; not a clean full frontend build.
- Ruff lint/format and targeted mypy (`--follow-imports=skip`) passed. Full-project lint/typecheck/build and supported-version results must come from exact-head CI; local targeted evidence is not a substitute.

## Release checks still required

Review #96/#105/#108 dependency stack, run exact-head CI and combined-candidate tests, stage signed-initData requests, test Telegram/mobile presentation, and exercise rollout/rollback. Keep the shop's existing transfer flag and limits under owner control. No merge, deployment, real customer transfer, provider call or financial policy decision was performed.

## First Actions run and corrections

Run `39122171709`, original head `384ffc05137ba8313a90ab78af9ff15a989565d7`:

- The owner-supplied Python job log reports **1136 passed / 1 failed**, with all 38 new transfer tests passing. The only failure is `test_reference_docs_are_current`: `docs/reference/api.md` omitted the three new routes. This is an actual code/documentation integration failure, not a Billing/not-started failure.
- The frontend screenshot shows `test:ux` passed, then `npm run lint` (`tsc --noEmit`) failed at the two `setMessage(describe(error))` calls. The repository enables `noUncheckedIndexedAccess`; the original scoped local compiler configuration omitted that flag, so its earlier success was insufficient.
- `describe` now has an explicit `string` return contract and reads the optional dictionary label once, falling back with `??`. Repository compiler flags are unchanged. The failing compiler flag reproduced the original two errors locally before the fix; the scoped strict check passes after the fix with that flag, `isolatedModules` and `noFallthroughCasesInSwitch` enabled.
- Four runtime cases cover known/unknown API errors during quote and confirmation. Updated UX command on **Node 22.23.3: 132 passed, zero fail/skip**, including 24 transfer cases. These are not additional unique backend tests.
- Added precisely three API rows rendered from the actual new APIRoutes by the unchanged docgen access/summary/table functions, preserving every existing reference row. The full application `python -m paurubot.ops.docgen --check` still requires exact-head CI; the local check is an incremental reference validation, not a claim of booting the entire app.
- A second isolated PostgreSQL run with the **entire unchanged ORM model package** (rather than the earlier six-class slice) also passes all 38 transfer tests. Customer/config/auth seams remain; this is not a local full migration/application test.

The original failure remains in history. New-head Actions must pass before declaring the PR CI-green; no test, workflow, compiler flag or branch protection was disabled.
