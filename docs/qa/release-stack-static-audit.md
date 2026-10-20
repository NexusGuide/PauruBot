# Static release-stack integration audit

## Scope and frozen evidence

Inspection only; no production merge/deploy, provider calls, full combined build,
PostgreSQL DDL rehearsal, Telegram/mobile/staging session or policy activation.

| PR | Inspected head | Role / base |
| --- | --- | --- |
| #117 | fd530a7c5ec35bebceac7c71312b52ef29f6831e | wallet transfer / #108 |
| #118 | 860e03628d37f05ab9da3e045f9405045fc0c055 | announcements / #108 |
| #119 | 5f8f40fc0522795c5e30ba3726b122babee7dc7f | ticket media / #108 |
| #124 | 20e9771e1e44c384e311b243541fee531c837684 | durable checkout / #123 |
| #125 | 95271d6dc7a773a1f529cda073b29d8cce85339a | renewal preview / #124 |
| #126 | 58bc5d38cb5cf5b0baf757c28d100964ed3f8ccd | recovery / #91; passing functional head before this audit |
| #127 | 81ed5f0e954751551a60ef472243b7f0b4739aeb | signed guest checkout / #124 |

## Migration topology: reproduced blocker, not a database test

The inspected declarations form:

```text
0025
├── 0026_announcements
├── 0026_ticket_media
└── 0026_card_verification
    └── 0027_guest_checkout
```

A local Alembic RevisionMap with a synthetic 0025 root and these exact revision/parent
values yields three heads. Resolving `head` raises MultipleHeads. This does not certify
earlier baseline revisions, SQL generation, schema compatibility or upgrade/backfill behavior.

On a complete integration branch only, add a new uniquely named merge revision whose
`down_revision` tuple contains ALL three leaf revisions. A topology-only simulated merge
resolved to one head. Do not add that file to an individual feature branch with missing
parents, renumber already-applied revisions, delete a sibling migration, or stamp production
to hide the disagreement. The real merge revision must be validated with all model exports,
then `upgrade head` rehearsed on disposable empty and restored PostgreSQL databases.
Do not promise financial safety from downgrade: dropping guest replay tombstones, read
receipts or support evidence while active operations exist is not an acceptable rollback.

## Test-command overwrite: concrete divergent edits

All inspected package diffs touch `frontend/package.json`'s `test:ux` value:

- #117 adds `tests/wallet-transfer.test.mjs`.
- #118 adds `tests/announcements.test.mjs`.
- #125 retains `tests/card-verification.test.mjs` from its base and adds
  `tests/renewal-preview.test.mjs`.

For these slices, the integrated explicit command must preserve at least:

```sh
node --test tests/ux.test.mjs tests/topup-intents.test.mjs tests/wallet-transfer.test.mjs tests/announcements.test.mjs tests/card-verification.test.mjs tests/renewal-preview.test.mjs
```

This command was NOT executed against a combined checkout. It is the required union,
not proof that these are all suites in every other open PR. Preserve unrelated scripts
and any additional tests chosen for the final stack as well.

## Shared-path semantic integration checklist

| Path | Inspected PRs touching it | Required preservation |
| --- | --- | --- |
| docs/reference/api.md | 117, 118, 119, 125 | regenerate from the full integrated app, retaining all routes/access labels |
| frontend/package.json | 117, 118, 125 | union test coverage; do not select a single feature command |
| frontend/src/app/api.ts | 117, 118, 119, 125 | retain each owned response/request contract and error handling |
| frontend/src/app/locales/en.json + fa.json | 119, 125 | retain matching support + renewal keys/placeholders |
| paurubot/api/routes/webapp.py | 117, 119, 125 | retain both webapp_transfers and webapp_ticket_media includes plus renewal route |
| paurubot/db/models/__init__.py | 118, 127 | export Announcement, AnnouncementRead and GuestCheckoutIntent |

These are changed-path overlaps, not a claim of demonstrated git merge conflicts.
#118 also adds `announcements` to the route registry; keep it in the combined app.
#119 changes worker/jobs.py for support media; retain cleanup registration when combining
with other worker work. Review both request ownership and retention behavior after merging.

## Still not a release certificate

Refresh the chosen heads before integration. Child CI may predate current parent fixes:
#122 records an older #120 base snapshot; the #105/#108 line has also moved. Avoid blindly
merging overlapping older audit/fix PRs. Tonpay remains a separate gated feature line.

Full backend security/full suite, frontend build/tests, migration rehearsal, actual mobile/
Telegram/staging QA and approved financial/privileged-access policy remain separate gates.
Signed guest checkout is not Bot/legacy coverage; a read-only renewal forecast is not a
locked late-payment execution result. No trusted limit, step-up factor, fee/refund policy,
automatic unknown-invoice retry or production authorization was chosen in this audit.
