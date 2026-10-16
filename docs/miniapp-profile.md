# F-07: Mini App profile and settings

Stacked after PR50 (which depends on PR49). No new migration: alerts_muted already exists.
More -> Profile (/app/profile) shows read-only Telegram identity, shop, membership date and a masked
registered phone. Only the last two ASCII digits are exposed for sufficiently long phone values;
short/malformed values stay fully masked. No raw phone, staff notes, trust/risk flags or credentials
are added to the client response. The phone is not described as verified. Telegram identity and
phone edits, outside-Telegram login and TOTP are not part of this feature.

The same CustomerSettings component on Profile and More controls language, marketing opt-out and
alerts_muted. Alerts only mute low_volume and expiring reminders, per the existing delivery handler.
Payment/support and actual service-expired/limited/deleted notices are not disabled. Marketing stays
independent and shop-specific. No preference is written just by opening a screen.

POST /api/webapp/me/alerts requires signed initData, a strict alerts_muted boolean, and no extra
fields/target customer ID. It uses the authenticated shop customer, row locking, explicit idempotent
set (not toggle) and atomic customer audit. Locale now also rejects extra fields. Existing supported
locale validation remains. There are no new staff permissions or identity-edit endpoints.

UI controls lock during save, ignore synchronous duplicate clicks, show errors without optimistic
success and use only confirmed response fields. Locale updates no longer depend on a silently
failed profile reload. Profile reloads started before a confirmed write cannot later overwrite that
write; among reloads only the newest response is applied. Separate fields are merged functionally.
This client guard is not a cross-device transaction guarantee; last committed server choice wins.

Release gates: full CI, real main/child-bot staging checks, masked-phone/identity display, language
persistence, notification delivery behavior, keyboard/mobile/RTL and failure/retry. This PR does not
merge, deploy, enable feature flags or migrate a server. Preserve PR48 and PR50 CI steps/scripts
when eventually merging the stacked branches into main.
