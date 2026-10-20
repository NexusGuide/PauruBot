# Shared trial quota concurrency (audit S08 / F-24)

`request()` keeps its initial eligibility check, then obtains a PostgreSQL transaction advisory lock using the stable `paurubot:trial-quota:` key and the customer's bigint shop ID (PostgreSQL `hashtextextended` → bigint) and repeats the real eligibility check. The shared count and reservation/order/job insert therefore remain serialized until the caller commits or rolls back. There is no internal commit, Redis lease, calendar-day counter or new schema.

## Preserved policy

- `trial_daily_quota` remains the existing rolling last-24-hours count of TrialGrant rows for the customer's shop, including reservations and previous generations. A zero quota remains unlimited.
- Same-customer/generation uniqueness remains enforced by the existing database unique key.
- Reseller children still obtain policy/panel/price from their parent and use their own shop's grant count, **as before**. This PR does not silently turn the quota into a shared parent-wide cap across different shops. A parent-wide quota would be a separate product/data-model decision.
- Rollback releases reservation and lock; existing failed-provisioning `release()` removes the grant. No change to refund/wholesale/provisioning semantics.
- The dedicated advisory key does not lock a Shop row; distinct shop keys proceed independently (a hash collision would only serialize unrelated shops, never loosen the cap). This count/recheck pattern uses the application's normal PostgreSQL READ COMMITTED isolation; it is not a guarantee for a deployment changed to REPEATABLE READ. Keep the caller's transaction short; do not add remote provisioning inside this transaction. All writers must use this reservation path for the invariant.

## Regressions

`tests/security/test_trial_shared_quota.py` forces every contender's initial real eligibility check to pass before insertion. It then checks 2 customers/cap1, 8/cap3, unlimited mode, exact order/job/grant counts, rollback, failed-provisioning release, cross-shop independence and the rolling window across generations. Real commits run in a separately migrated disposable schema, dropped on teardown; immutable ledger triggers are never disabled.

Use disposable PostgreSQL/Redis. Run that file plus commerce/resale/provisioning tests and the full suite. These checks do not hit a real panel or payment gateway. No deploy or migration is required by this code change itself; workers/processes still need a normal rollout after review.
