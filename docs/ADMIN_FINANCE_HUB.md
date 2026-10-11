# Finance, payment gateways and card-transfer settings in Telegram

## Pages

The existing **Finance** admin landing button now opens a dedicated hub: existing financial reports/exports, payment review, gateway management, card-transfer policy, wallet top-up reward rules and gift codes. Staff see only permission-appropriate entries. The existing report export logic is unchanged and returns to the finance hub.

The gateway table has three inline-keyboard columns, Actions / Status / Title, inspired by the reference screenshots. All 14 rows correspond to existing supported methods: card, Plisio, NOWPayments, Tronado, Tetra98, CubePay, Blupal, Variza, AbanGateway, AtlasPay, Zarinpal, direct crypto, Telegram Stars and Aqayepardakht. No new gateway integration or provider endorsement is implied.

Status is effective availability, not merely an enabled flag: **Off**, **On**, or **Incomplete setup**. The same provider `enabled()` checks used by payment creation are called, including active destination cards, public callback URL, required key/IPN fields, crypto network/address and cached/manual exchange rate. No transaction is created or bank balance checked. Enabling is rejected when prerequisites are missing and checked again at confirmation.

Each gateway page has enable/disable, supported non-secret options and the secure web settings link. API keys, callback secrets, wallet destinations and destination-card add/edit/delete remain in the existing authenticated web settings; this new flow does not collect credentials or full bank card numbers in Telegram.

## Card transfer

- Destination-card list shows only the last four digits, holder/bank and active state; enable/disable requires confirmation. Disabling the last active card makes the method unavailable. No hard-delete action was added to this flow.
- Selection mode: all active cards, random, or rotation.
- Random amount tail, customer-card verification/amount threshold, duplicate-receipt blocking.
- Existing conditional immediate auto-approval settings: switch, positive amount cap, at least one staff-reviewed prior payment.
- Existing timed auto-approval setting: minutes, zero disables; it is a separate policy from the immediate switch.
- Common top-up minimum, payment maximum and expiry. Bounds are **shop-wide**, not newly invented per-gateway limits; existing order/amount semantics remain unchanged.
- Top-up rewards are existing bot-wallet credits, not bank cashback. The most generous matching rule is displayed; edit the validated percentage/minimum/cap rules in the web settings.

Auto-approval is not proof of a bank deposit. Its preview warns about automatic wallet credit without human review, possible effects on already pending receipts, separate immediate/timed policies, and the existing trusted-customer bypass. This PR does not enable either policy by default, approve a receipt, transfer funds, refund a payment or change any production setting.

## Confirmation and safety

Every editable setting in the new flow uses preview → explicit confirmation. Confirmations are bound to the staff member's FSM session, shop, key, random nonce and a 15-minute lifetime. The pending value is not written during preview. Cancellation/navigation clears it.

Confirmation locks the actual setting row (including implicit defaults), reloads current settings and compares a digest of the old value. Conflicting edits/card-list reorders invalidate the preview. Amount bounds and auto-approval dependencies are locked in a consistent order and revalidated together. Gateway readiness is rechecked. Duplicate callbacks cannot invert a setting or duplicate its audit entry.

`settings.write` is rechecked for callbacks and text inputs. The editable whitelist contains only known non-secret keys. Raw card lists do not enter confirmation state or audit payloads: card changes audit masked identifiers and the enabled state. Full API keys/receipt data are not displayed or audited.

The web link requires a configured public URL; missing URL is clearly reported rather than guessed. Existing owner-only API-key access rules remain enforced by the web backend. Legacy general settings/card screens are unchanged and still reachable under Settings; the new financial hub does not use their secret/card-entry prompts.

## Scope exclusions

Reference-screen features without matching existing backend fields were not added as fake buttons: custom display names per gateway, a new Noby provider, provider-specific top-up bounds, support-ID/offline-PV card instructions, requiring card verification only after the first payment, and unconditional receipt approval. Low-level gateway credentials/destinations, reward-rule editing and card additions/deletions use the existing web editor.

No migration or production deployment is included. Stacked on the panel-management PR, not on main directly.

## Release checklist

After green tests/CI, use a test shop/customer and sandbox credentials where supported:

1. Check the hub and three-column keyboard render in Telegram.
2. Check missing credentials/card/rate display and blocked activation.
3. Confirm a setting, repeat its callback and verify one audit entry and no unintended money movement.
4. Change the setting externally between preview/confirm and verify rejection.
5. Test card selection and actual sandbox checkout/receipt review; opening settings is not a provider transaction test.
6. Verify the finance role cannot edit settings and API keys remain owner-only in web.
7. Leave auto-approval disabled unless its risk has been explicitly accepted; restore test settings before production.

Never merge into main or deploy without live validation and approval.
