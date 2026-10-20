# Automatic receipt safety exclusions (audit S03, F-23)

Immediate receipt approval and the delayed `cards.approve_late` job now share an eligibility
filter. An automatic decision requires a card payment under review, a receipt file, no
known duplicate or staff correction history, and a customer in the payment's shop who is
neither blocked nor medium/high risk. Trusted status does not override these exclusions.

Previously a customer's trust/risk could change between checkout and upload, yet immediate
approval could still pay a blocked customer or a trusted, high-risk customer. A corrected
receipt was excluded from immediate approval, but the delayed job could later pay it anyway.
Those cases now stay VERIFYING for a human. No automatic refund or rejection is introduced.
Malformed, non-string or timezone-naive `receipt_at` values also stay manual without aborting
the entire batch; valid aware timestamps (including non-UTC offsets) retain their meaning.

## Preserved policy and explicit residual risks

- A clean trusted customer still bypasses the immediate enable flag, ordinary amount cap and
  history threshold (PAY-17), as existing product tests require. This remains a financial
  exposure; **this PR does not claim to close S03's trusted amount-cap policy gap**. A mandatory
  cap/opt-in trust model requires an explicit, separately reviewed policy and migration.
- Delayed approval remains controlled by `card_auto_approve_after_minutes`, independently of
  the immediate `card_auto_approve` flag. Existing delayed amount-cap semantics are unchanged,
  including zero-as-unlimited. No settings migration or silent default change occurs.
- An image/receipt is not bank settlement. Human approval and manual override remain available.
  Existing mark_paid idempotency is retained; no new bank verification, distributed lock, or
  serialisation guarantee against a simultaneous policy update is claimed.
- The filter uses the current transaction's loaded customer state. Concurrent policy updates
  and stale ORM objects need separate locking/isolation design if strict ordering is required.

## Verification and release

Test policy changes after checkout, trusted high/medium risk, blocked customers, corrected
receipts after timeout, invalid timestamps followed by a valid receipt, repeated timer runs,
and preserved clean-trusted/normal paths. Run existing cards, receipt-fix, duplicate detection
and payment tests. No real receipt, Telegram message or gateway settlement is needed.

No schema/environment change. Roll back code if necessary, but reverting restores the unsafe
automatic paths. Disable immediate and delayed auto-approval during investigation; remember
that the legacy trusted bypass is independent of the immediate enable flag. Removing that
bypass or marking trusted customers normal must be a deliberate authorised policy action.
