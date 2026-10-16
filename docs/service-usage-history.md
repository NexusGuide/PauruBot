# F-06: observed service-counter history

Stacked on PR49/F-15. Migration 0023 follows 0022: merge/rebase in that order, not as independent
migration heads. This is a counter-history chart, NOT exact per-day consumption or lifetime totals.

## Data contract

Only successful panel reads and accepted webhooks whose PanelUser explicitly declares usage_known
record data. The default is false. TokenApiDriver (including inheriting Marzban/PasarGuard mappings)
opts in only when used_traffic is an exact, non-negative integer/string within JavaScript's safe
integer range. Missing/null/bool/fractional counters do not create synthetic zero observations.
Other parsers must be verified and opt in before their charts populate; unsupported is not zero.
No backfill, interpolation, sums across renewals, or per-node/upload/download detail is invented.

One row per service per UTC hour keeps the last observation, with the actual receipt timestamp.
The first observed drop/change/gap within that hour remains a sticky line break. Writes serialize
on the service row after network I/O. Replayed timestamps cannot replace newer data. Locks live
until the caller commits; the existing panel-sync batch/transaction behavior is unchanged.
Receipt order is not provider event order: drivers/webhooks have no common trusted event timestamp.
Known omissions cannot reveal resets that occur and recover entirely between observations.
Limits/expiry/panel/user identity changes conservatively break the line, even if no reset occurred.
A gap over two hours also breaks it. Same-hour overwrites intentionally omit intrahour detail.

GET /api/webapp/services/{service_id}/usage?days=7|30 reads local history only. Both customer ownership
and shop are checked. Other tenants/customers and deleted services are 404. No panel requests on
chart/window changes. At most days*24+1 real observations; no missing periods filled with zero.
The UI uses UTC timestamps on the wire and explicitly labels device-local display times. It shows
loading, error/retry, insufficient-data and stale states. Response cleanup prevents a slower old
service/window request from replacing the current chart. Exact values are in an accessible selector
and hidden table; visual marks use actual elapsed time, with discontinuities rather than fake joins.

## Retention and release gates

An hourly cleanup removes up to 5000 rows older than 35 days per run; retention is a target and can
lag under sustained large backlogs. Monitor table growth/cleanup throughput and raise capacity before
large deployments. Hard service deletion cascades history removal. No additional customer identity,
connection credentials or raw panel payloads are stored in the history table.

Before release: apply migrations on staging, confirm valid and missing counters on each enabled
panel driver, webhook/poll behavior, concurrent writes, long outages, renewals/counter drops and
retention throughput. Review the additional write/row-lock cost in the existing sync transaction.
No production deployment, merge or retrospective data reconstruction is performed by this PR.
