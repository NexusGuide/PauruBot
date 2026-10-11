# In-bot user management

The admin landing menu's **User management** section provides:

- Positive wallet balances, referrals, approved bank cards, negative balances, active subscriptions, all users and resellers.
- Existing customer lookup/cards, audience-selected wallet credits and broadcast composition.
- Order search by order number (including Persian digits) or exact panel service username/label, with pagination and customer/service shortcuts.
- Mass time/data gifts with a preview, explicit confirmation and operation-status screen.

## Permissions and safety

The menu and every callback remain permission-checked and shop-scoped. A card is considered active for this list only when its registration status is `approved`; its full number is never displayed.

Free shop-wide service gifts require both `services.write` and `wallet.adjust`. Only active, non-trial services belonging to non-banned Telegram users on active, gift-capable panels qualify. Unlimited services are excluded only in the selected dimension. Up to 1,000 services are previewed; larger operations must use the web panel's filters.

The preview is a durable, creator-bound draft valid for 15 minutes. It snapshots service IDs and absolute targets. Confirmation locks that draft, queues one deduplicated job per service and cannot execute twice. It does not include services created after preview.

At execution, the worker rechecks tenant ownership, eligibility and expected limits in both the database and the panel. Changed services are skipped, not overwritten. Retried calls use the same absolute targets; an already-applied update whose response was lost is reconciled without adding the gift again. The queue supports retries, audits and per-operation progress. Queued is not the same as successfully applied.

Wallet-credit confirmations are bound to the specific amount/audience preview, with a stable batch id for duplicate-click deduplication. Financial changes continue through the existing ledger and gift worker.

## Manual checks before production

1. Test with an owner and a support account; restricted mass operations must not appear or execute for support.
2. Open each user filter and return to user management.
3. Search an order by ID and service username; test a nonexistent ID and pagination.
4. Preview a small gift in a test shop. Cancel it first and verify no service changed.
5. Confirm a fresh preview, inspect operation status and verify actual panel limits.
6. Verify duplicate confirmation/retry does not credit or extend twice.

This feature does not add a database migration. Automated tests are not a substitute for checking the Telegram UI and a real panel on a test deployment. Do not merge into `main` until required tests and CI pass and the change is approved.
