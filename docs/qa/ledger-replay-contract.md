# Ledger idempotency contract (audit S07)

An idempotency key now identifies the complete financial meaning: shop, journal kind,
reference type/id, reversed-journal id, and the canonical account-to-net-amount mapping.
A conflicting retry raises the existing LedgerError inside its savepoint instead of falsely
reporting that a different amount/customer/reference was already posted successfully.

Canonicalisation merges repeated account lines, removes zero effects and ignores line order,
exactly as normal posting already does. Memo and actor are intentionally not financial
identity: a retry may have different diagnostic context, but must not overwrite the original
journal's metadata. A valid replay still returns the original journal with created=False.
Current balances are not rechecked or changed for an already committed equivalent operation.

## No hash-column migration

The comparison uses immutable persisted ledger entries and journal fields, so it works for
pre-existing journals without rewriting append-only history or backfilling a new digest.
The unique global idempotency key, atomic journal/entries/balance transaction, stable account
lock order and non-negative wallet checks remain unchanged. Under READ COMMITTED, concurrent
ON CONFLICT contenders wait for the first transaction and compare its committed effect.
No separate Redis lock, independent transaction or destructive ledger update is introduced.

## Operational boundary

This intentionally rejects callers that previously reused a key with different financial
inputs. Investigate such a conflict; do not generate a fresh key blindly, because doing so
could double-charge/credit. The exception is the existing LedgerError; this patch does not
promise a new HTTP 409 response or add a reconciliation dashboard. Existing worker retry /
dead-letter handling still applies; conflicts require operator inspection, not automatic
repair. It does not repair any historical incorrect caller acknowledgement, merge old keys,
or solve payment-intent/provisioning idempotency outside ledger.post.

Rolling back code restores the false-success behaviour but changes no schema. Keep normal
backup/restore and reconcile controls; do not modify/delete immutable entries to clear a
conflict. No provider, production wallet or real money was used in validation.

## Regression tests

Reject changed amount, target account, business-reference type/id and reversal id. Validate
that rejection changes neither balances nor journal/entry counts and that the original key
can still replay after the rejected savepoint. Accept equivalent split/reordered lines and
metadata-only changes without altering the original journal. Four concurrent transactions
with the same key must create one journal and accept only the winning payload; matching
retries return it, different payloads fail. Concurrency tests run in a separately migrated,
disposable schema without disabling append-only triggers.
