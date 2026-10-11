# In-bot panel management

## Navigation

Admin → Panels → Manage [panel] opens the panel-specific screen. The panel list is paginated (10 per page). The legacy panel-settings screen also links to the new manager.

The manager shows the panel's name/type, new-sales state, last connection result and UTC check timestamp, non-deleted/active service counts, direct-product count, capacity and username policy. It does not display addresses, credentials, tokens, certificates, raw driver errors or arbitrary options.

Staff with `panels.read` can view the manager, feature states and driver capabilities. `panels.write` is checked again for every setter, connection test and text input. Products require `catalog.write`; the financial report requires `reports.read` as well as panel access. All lookups and aggregates are shop scoped.

## Feature switches

| Switch | Persisted value | Actual effect |
| --- | --- | --- |
| New sales / provisioning | `Panel.is_active` | Existing catalog and provisioning eligibility; does not turn existing accounts off |
| Allow trials | `options.allow_trials` | New trial selection/request and pending trial provisioning recheck this flag |
| Allow renewals | `options.allow_renewals` | Customer renewal button hidden and order drafting rejects a disabled renewal; accepted/paid renewals are not retroactively cancelled |
| Sell extra data | `options.sell_extra_volume` | Existing extra-data pricing and order validation |
| Sell extra time | `options.sell_extra_days` | Existing extra-days pricing and order validation |
| Reset usage on renewal | `options.renew_resets_usage` | Existing renewal default; explicit product policy takes precedence |

Missing trial/renewal flags default to allowed, preserving existing installations. API panel options validate and retain these booleans, and the existing web edit form retains unknown-to-the-form options from its loaded record. No migration is needed.

Switch rows contain the state and label side by side. Both buttons set an explicit desired value; replaying the same callback does not invert the state or duplicate its audit entry. Row locks prevent lost updates to unrelated options. Audit entries contain only the changed safe field, not the full options dictionary. Renewal reset has a separate **inherit shop policy** action.

Unsupported extra/renewal capabilities are disabled rather than presented as working toggles. Global shop settings, plan addons and driver capabilities still restrict availability even if a panel's switch is on.

## Driver capabilities

A separate read-only screen describes driver support for renewal, upgrade, extra data/days, gifts and connection-credential rotation, plus the delivery mode. These reflect the declared driver implementation, not a live compatibility test or permissions audit. They are not feature switches and cannot be enabled by pressing the labels.

Screenshot-specific options with no matching implementation (group f/n/n2 flags, arbitrary inbound/protocol editing, custom subscription switches, per-panel custom-buy bounds and refund-button switches) were not added as fake toggles. Existing plan/shop/web settings stay in their respective workflows.

## Management operations

- Rename (one line, 1–64 characters), edit capacity (0/- means unlimited) and edit username prefix (up to 16 alphanumeric/underscore characters).
- Explicit username-method picker for future accounts only; existing service usernames are unchanged.
- Test the stored connection via the existing safe driver, with a 20-second timeout; store success/failure and sanitized error state, audit the result, never echo credentials/raw errors.
- Direct products (paginated; location-pool products are not included).
- Financial order report scoped to completed orders of this shop for services currently on the selected panel. The report shows gross completed paid-order amounts, rolling 30-day total/daily average, completed trial count and renewal/extra data/day totals. Free orders and trials do not add to sales amounts. It is not profit or net sales after refunds; service moves change attribution.
- Secure web link for panel address, keys, kind, groups and protocol/inbound configuration. No credential-entry prompts in Telegram.
- Back to panel list/admin, and close (generic notice, no inline keyboard; old messages are not deleted).

No hard-delete operation was added: deactivating new sales preserves accounts, audit history and panel references. No production operations or deployment are performed by this PR.

## Live validation before main

Use a test panel and test customer, after backup:

1. Confirm the list/manager and paired status rows render correctly in Telegram.
2. Turn a panel switch off/on and confirm a second panel is unchanged.
3. Check trials, renewal draft rejection and extra pricing; re-enable test settings afterwards.
4. Rename/prefix/method tests affect future accounts, not existing accounts.
5. Check connection success/failure, restricted staff, back/close, and product/report navigation.
6. Validate the real panel's driver capability claims separately.

Stacked on the user-detail PR; keep it out of main until automated CI and live smoke tests pass and deployment is approved.
