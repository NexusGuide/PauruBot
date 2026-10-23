# Manual inventory and national-network routing

## Setup

Run the normal database upgrade to revision `0031_manual_national` before starting the new API,
bot and workers. Deploy frontend and backend together. Existing stock payloads remain readable;
manual delivery stays a stock order rather than creating a fake remote-panel service.

1. Create a stock-delivery product in Catalog (with a location, price and validity).
2. Open **Manual sales & national network** in the admin sidebar.
3. Import **Single** (one sale unit), **Bundle** (up to 20 parts delivered together), or
   **Bulk** (each part becomes a separate unit, with numbered names).
4. Choose text or uploaded files. In multi-part text mode, separate parts by a blank line.
   Multiline contents inside each part are preserved. Text can also be delivered as a file.
5. Each part may have a separate HTTP(S) subscription URL. These URLs are never fetched by ingestion.

Files: txt, conf, ovpn, json, yaml, yml, npv, pcf, ehi; up to 1 MiB each, 8 MiB per import,
200 bulk units per import and 10 GiB encrypted inventory storage per shop. Paths, active browser
formats, executables, invalid base64 and credential-bearing subscription URLs are rejected.
The storage meter includes available, reserved, sold, disabled and legacy units. Delete only
unsold/unreserved inventory; sold order/customer history cannot be deleted through this console.

## National-network mode

For each **fixed-panel** product, select a stock product as its replacement inventory.
Enable national mode on the panel explicitly. Unmapped products become unavailable; panels in
national mode are excluded from automatic location pools. Pool products use any remaining normal
panel; they do not guess a replacement-product mapping.

New purchases and renewals use the original product's pricing and a frozen replacement route.
The replacement unit is reserved in the wallet-charge transaction, unavailable to other buyers,
and hidden from customer downloads until fulfillment. Failed fulfillment refunds release an
unexposed reservation. A stock unit already exposed to a buyer is never recycled.
Changing the switch or mapping before payment invalidates that quote; the buyer must start again.
Paid inventory orders honor their recorded route even if the switch is later disabled.

**Renewal in national mode delivers a new replacement configuration; it does not extend or modify
the original panel subscription.** Checkout and service details say so explicitly. Renewal queues
are bypassed for replacement delivery. The original service, usage, expiry and financial history
are retained. Disabling national mode restores normal panel operations and original subscriptions.
Remote panel access is blocked while national mode is active. Public branded subscription endpoints
return an uncached 503 instead of proxying stale remote content. Extra volume, upgrades, revocation
and other remote mutations are unavailable. There is no automatic fallback after an uncertain panel
API response, avoiding duplicate provisioning.

## Customer retrieval and access

Delivery notifications send every text/file in a bundle. Telegram's ready-made configuration list
supports pagination and full resending, not truncated credentials. The Mini App's Services page
has a separate ready-made inventory section, including purchases without a Service row. File
requests carry Telegram authentication; they are not public share URLs. Replacements for existing
services also appear in this inventory.

Admin reads require catalog.read; import, reveal and bulk operations require catalog.write.
National switches/mappings additionally require panels.write. Scoped reseller staff cannot operate
the shop-wide inventory console. All reveals, imports, mutations and national switches are audited
without plaintext credentials in the audit records. Full-content/download responses are no-store.

## Verification

`tests/unit/test_manual_sales.py` covers validation, legacy compatibility, encrypted bundles,
file identity/deduplication, quota enforcement, independent concurrent buyers and same-order retries,
reservation/refund behavior, national purchase and renewal, unchanged original services,
private binary retrieval, actual Telegram file bytes, HTTP filtering, permissions and audited reveal.
The normal Alembic graph test verifies the single migration head. Review mobile/desktop import,
inventory, national routing, error/empty states and customer bundle retrieval before deployment.
