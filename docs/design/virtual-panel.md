# Virtual panel, PasarGuard-compatible (design: contract and architecture)

Status: **draft for review**. No code has been written yet.
Sources examined (source code, not guesses):

- PasarGuard `panel` at 2b75db1 (version 5.6.0): `app/routers/{admin,user,system,group,core}.py`,
  `app/models/{user,system,group,admin}.py`
- Mirza (`mahdiMGF2/botmirzapanel`) at 92c0ed0: `marzban.php`, `panels.php`, `index.php`, `admin.php`

## 1. Goal

The bot exposes a "virtual panel" with PasarGuard's API. Each sold reseller account sees only the
groups that the shop owner allowed (any group on any of the bot's real panels). A user created on
the virtual panel becomes real users on the matching real panels, and gets one subscription link
served by the bot. Sales bots (Mirza and similar) and the bot's own web panel are both clients of
this same API.

## 2. Mirza's actual requests (the minimum contract)

Mirza has no separate type for PasarGuard. It connects PasarGuard through its `marzban` connector.
It uses only the following:

| Request | What it sends | What it reads from the response |
|---|---|---|
| `POST /api/admin/token` | **form-urlencoded** `username`, `password` | `access_token`; caches it for **one hour** |
| `GET /api/system` | — | `version` (if > 0.8.4, it uses groups), `total_user`, `users_active`*, `mem_total`, `mem_used`, `incoming_bandwidth`, `outgoing_bandwidth` |
| `GET /api/groups` | — | `{groups:[{id,name}]}` or a bare list |
| `POST /api/group` | `{name, inbound_tags[]}`. **It creates the groups `mirza_paid` and `mirza_test` itself** | `name` |
| `GET /api/cores` (fallback `GET /api/inbounds`) | — | `cores[].config.inbounds[].tag` (or `[].tag`), only to build the group |
| `POST /api/user` | `username`, `data_limit` (bytes), `expire` (**unix seconds or 0**), `proxies`, optional `inbounds`, `group_ids`, `status:on_hold` + `on_hold_expire_duration` | the raw response text (ignored) |
| `GET /api/user/{u}` | — | `username`, `status`, `data_limit`, `expire` (**unix number**), `online_at`, `used_traffic`, **`links`**, `subscription_url` (if not absolute, it prefixes the panel address) |
| `PUT /api/user/{u}` | renewal: `{expire, data_limit}`; extra volume: `{data_limit}` | `detail` on error |
| `DELETE /api/user/{u}` | — | `detail` on error |
| `POST /api/user/{u}/reset` | — | — |
| `POST /api/user/{u}/revoke_sub` | — | `detail`; then it reads the user again for `links` and `subscription_url` |

\* PasarGuard returns `active_users` while Mirza reads `users_active` (a Marzban leftover). Its admin
statistics are wrong with real PasarGuard as well.

Errors: Mirza only looks at the `detail` key (FastAPI style), so every error must be
`{"detail": "..."}` with an appropriate HTTP status.

## 3. Incompatibilities between real PasarGuard and Mirza (important)

1. **`expire`**: PasarGuard 5.x returns `expire` and `online_at` as ISO date strings, but Mirza treats
   them as numbers. PasarGuard accepts `expire` as either a number or a date on input.
2. **`links`**: PasarGuard's `UserResponse` has no `links` field, while Mirza reads it to show configs.
3. **`users_active`**: see the footnote above.

So exact PasarGuard compatibility and Mirza compatibility conflict on these fields. **Decision: each
reseller account has a "client profile":**

- `pasarguard` (default): exactly matching PasarGuard 5.x's schemas (ISO dates, no `links`).
- `marzban-compat` (for Mirza): the same endpoints, but `expire`/`online_at` as unix numbers, `links`
  filled in, and `users_active` returned alongside `active_users`.

The profile is chosen on the reseller account's page (no guessing per request).

## 4. Required PasarGuard API (stage 2, beyond Mirza)

For other bots and the web panel: `GET /api/admin` (current admin), `GET /api/users` (with `offset`,
`limit`, `search`, `status`, `sort`, `group`), `GET /api/users/simple`, `GET /api/user/by-id/{id}`,
`PUT /api/user/{u}` with `status: disabled|active|on_hold`, `group_ids`, `note`,
`data_limit_reset_strategy`, `next_plan`. The output is `UserResponse` with `id`, `username`, `status`,
`used_traffic`, `lifetime_used_traffic`, `data_limit`, `expire`, `created_at`, `edit_at`,
`online_at`, `subscription_url`, `group_ids`, `note`, `on_hold_*`, `admin`. Each contract item has a
"golden" test taken from PasarGuard's models and Mirza's requests (section 9).

Endpoints the virtual panel deliberately does **not** provide, returning 403 or 404 with `detail`:
node, host, core, and admin management; settings; anything that needs real sudo access.
`GET /api/cores` only returns virtual tags (see 6.3).

## 5. Data model (new tables)

- `virtual_accounts`: one per sold reseller account (`panel_account_id`, `shop_id`, `username`,
  `password_hash` (argon2/bcrypt), `client_profile`, `data_limit`, `max_users`, `expire_at`,
  `status`, `token_version`).
- `virtual_groups`: the groups the shop owner allowed for each plan/account: (`id` shown to the
  client, `title`, `panel_id`, `real_group_ref`). A virtual group = one group on one real panel.
  The real names stay hidden.
- `virtual_users`: (`account_id`, `username` **unique per account**, `public_id`,
  `data_limit`, `expire_at`, `status`, `on_hold_*`, `note`, `sub_token`, `used_bytes` (aggregate),
  `version` (CAS), `deleted_at`).
- `virtual_bindings`: (`virtual_user_id`, `panel_id`, `real_username`, `state`
  ∈ {pending, live, removing, gone, orphan}, `last_synced_at`, `used_bytes`).
  **Unique on `(panel_id, real_username)`**, and every real user name is minted with a fixed prefix
  for the account (section 7).
- `virtual_ops`: the saga log (section 6).

## 6. Multi-panel operations: Saga (not a single transaction)

### 6.1 Rules
- Each request (create, edit, delete, reset, revoke) is **first recorded** in `virtual_ops` with an
  `op_key` (idempotent), the target state, and one **step** per real panel, all in one local
  transaction. Only after that does any external request go out.
- The worker runs each step with a lease, in the same pattern the bot already uses for orders and
  services (`claims`, lease, `uncertain`).
- Each step's outcome is one of `done`, `failed` (certain failure), or **`uncertain`** (timeout or an
  unclear answer). An `uncertain` step is **never** automatically resent. It is first **read back**
  with `GET` on the real panel (reconciliation), and the next decision is made from what is actually
  there.
- Retries: only for steps whose outcome is certainly "not done", with backoff and a cap. Persistent
  failure parks the op as `needs_attention` and alerts the owner.

### 6.2 Compensation (not a rollback)
- **Create**: if some panels succeed and one fails for certain, either a) retry that one panel
  (default), or b) once the retry cap is exceeded, put the user into a **partial** state: the
  subscription only has the successful panels, and the error is visible to the owner. Removing the
  successful users is only done as a compensation step when the creating request itself was
  cancelled, and that compensation is itself an op with its own steps and its own reconciliation.
  Nothing assumes deletion always succeeds.
- **Edit** (volume, expiry, status): it is idempotent by nature (sets the target state, not a delta).
  Retrying is enough, so no compensation is needed. Extra volume in Mirza is a delta
  (`data_limit + X`), but it reaches us as an absolute value, so we only apply the absolute target.
- **Delete**: first `status=disabled` on every panel (quick and safe), then deletion. Real users that
  are not deleted become `orphan` and are removed by a sweeper job.

### 6.3 Response to the client
- The API answers **only once** the local record is durable. Whether it answers immediately with the
  target state (and the saga runs in the background) or waits a short time for the steps depends on
  what the client expects. Mirza reads the user right after creating it, so the user must be in the
  database immediately and `subscription_url` must be available straight away (the bot's link does not
  depend on the real panels).
- `GET /api/cores` and `POST /api/group` (Mirza's `mirza_paid`/`mirza_test`) create a **virtual
  alias** group that points to "all groups allowed for this account". Mirza works without changing its
  settings, and no real panel is touched.

## 7. Preventing interference between resellers

- Every real user name is **built by the bot**, `<account prefix>_<virtual name>`, with a prefix that is
  unique, random and immutable for each account. The client never chooses a real name.
- Every operation finds the real users **only** through `virtual_bindings` of the same `virtual_user`,
  under the same `account_id` taken from the token. Requests are never sent to a real panel by name
  alone.
- Before any change, `virtual_bindings` and the prefix are checked against each other. A real user not
  recorded in `virtual_bindings` (for example one made by hand on the real panel) is **never** taken
  over (the same rule as `SEC-F03`).
- The token carries the `account_id` and the `token_version`. Disabling the account, changing the
  password or its end invalidates every token. Every query is filtered by `account_id`. Tests:
  a user of reseller A cannot be read, changed, deleted or reset with reseller B's token, by name, by
  id, or through the subscription.
- `sub_token` is long and random, and revocable (`revoke_sub`).

## 8. Volume, expiry and the subscription

- **Volume is one combined allowance**: `used = Σ used_bytes(binding)`. On each sync, if
  `used ≥ data_limit` or expiry has passed, an op is created that sets `disabled` on every panel.
  Because syncs happen every few minutes, **slight overuse is possible**. To reduce it, each real panel
  can be given `data_limit = remaining allowance` as a ceiling, refreshed on each sync.
- The reseller account's own allowance (total volume, user count, expiry) is checked at create, edit
  and sync time.
- Subscription: `GET /vsub/<sub_token>` reads each real panel's subscription (base64 v2ray), renames
  the server labels (the "my servers" feature) and returns them combined. Clash and sing-box come in a
  later stage. If a panel is unreachable, its part is served from a short-lived cache.

## 9. Compatibility tests (before the code)

- **Contract tests from the source code**: one test per Mirza request in section 2 (body, header
  `Content-Type: application/x-www-form-urlencoded` for the token, the keys it reads), for both
  profiles.
- **Schema tests**: the `pasarguard` profile's output is validated against PasarGuard 5.6.0's pydantic
  models (`UserResponse`, `UsersResponse`, `SystemStats`, `GroupsResponse`, `Token`) in a test with
  PasarGuard's models copied in or pinned.
- **Saga tests**: for each step, failure, timeout (uncertain), duplicate (idempotent), and stopping
  midway with the worker restarted.
- **Isolation tests**: section 7.
- Before release, at least one real end-to-end run with Mirza against a test setup.

## 10. Address

Mirza adds `/api/...` to the panel's base URL itself and does not support a path prefix. The virtual
panel therefore needs **its own host or port** (for example `panel.shop.ir` or `bot.shop.ir:2053`),
served by the bot's Caddy, with its own rate limit for `/api/admin/token`.

## 11. Stages

1. Data model + saga + bindings + sync and enforcement + combined subscription (PasarGuard and
   Marzban backends), with the contract, saga and isolation tests.
2. The API in both profiles + the separate address + end-to-end tests with Mirza.
3. The web panel (the agreed design) on the same API.
4. Per-reseller servers, templates, buying volume from inside the panel, more backend types,
   Clash/sing-box.

## 12. Decisions to make

1. The default profile for new accounts: `pasarguard` or `marzban-compat`?
2. When a create partly fails: keep retrying (default) or remove the successful parts?
3. A separate host or a separate port for the virtual panel?
4. Backend types in stage 1: PasarGuard and Marzban only?
