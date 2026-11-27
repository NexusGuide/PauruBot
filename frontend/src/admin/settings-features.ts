// Settings by feature: every option lives on exactly one page, the page of the feature it belongs
// to (all of renewing on "renewal", all of card to card on "card"…), whatever group the server
// files it under. An option no feature names is never lost: it shows on "other". Pure, so it is
// tested on its own.

export interface Feature {
  key: string;
  /** Options named one by one. */
  keys?: string[];
  /** Every option of these server groups that no feature names one by one. */
  groups?: string[];
  /** Every option starting with one of these that no feature names one by one. */
  prefixes?: string[];
}

export const FEATURES: Feature[] = [
  { key: "general", keys: ["bot_enabled", "discounts_enabled", "languages", "support_username", "typed_confirm_enabled", "typed_confirm_refund_min", "audit_retention_days", "light_mode"] },
  { key: "signup", keys: ["require_phone", "phone_country_codes", "phone_when", "terms_required", "terms_version", "terms_when"] },
  {
    key: "buy",
    keys: [
      "max_active_services",
      "max_daily_purchases",
      "buy_by_duration",
      "order_expire_minutes",
      "username_method",
      "username_prefix",
      "username_suffix",
      "custom_username_enabled",
    ],
    groups: ["guest"],
  },
  { key: "renewal", keys: ["renew_mode", "renew_queue_when_active", "renew_resets_usage", "upgrade_enabled", "move_enabled", "move_price"] },
  { key: "extras", keys: ["extras_enabled", "extra_packages"], prefixes: ["extra_"] },
  { key: "custom", groups: ["custom"] },
  { key: "trial", groups: ["trial"] },
  {
    key: "delivery",
    keys: ["info_card_enabled", "info_card_theme", "info_card_qr", "qr_send_enabled", "qr_show_link", "qr_caption", "qr_background_url", "branded_sub_links", "app_links"],
  },
  {
    key: "notify",
    keys: ["warn_days", "warn_time_percent", "warn_remaining_percent", "warn_remaining_gb", "first_use_nudge_hours", "alerts_quiet_start", "alerts_quiet_end", "ip_notice_enabled"],
  },
  { key: "cleanup", keys: ["delete_expired_after_days", "delete_warn_days", "self_delete_enabled", "self_delete_refund_percent", "self_delete_require_approval", "ban_disables_services"] },
  {
    key: "sync",
    keys: ["usage_sync_minutes", "capacity_warn_percent", "service_import_enabled", "service_import_auto", "service_import_panel_ids", "remnawave_loyalty_tags"],
  },
  {
    key: "wallet",
    keys: [
      "min_topup",
      "max_topup",
      "wallet_topup_presets",
      "topup_bonus_rules",
      "wallet_transfer_enabled",
      "wallet_transfer_min",
      "wallet_transfer_daily_max",
      "payment_expire_minutes",
      "payment_method_rules",
    ],
  },
  { key: "card", keys: ["receipt_duplicate_block", "reject_reasons"], prefixes: ["card_"] },
  { key: "gateways", keys: ["gateway_vpn_off_reminder"], groups: ["gateways"], prefixes: ["zarinpal_", "stars_", "nowpayments_", "plisio_"] },
  { key: "crypto", groups: ["crypto"] },
  { key: "referral", prefixes: ["referral_", "start_gift_"] },
  { key: "games", keys: ["lottery_enabled"], prefixes: ["wheel_"] },
  { key: "loyalty", keys: ["loyalty_enabled", "loyalty_tiers"] },
  { key: "discount", prefixes: ["catalog_discount_"] },
  { key: "resellers", groups: ["resellers"] },
  { key: "reports", groups: ["reports"] },
  { key: "business", groups: ["business"], keys: ["auto_replies"] },
  { key: "tickets", groups: ["tickets"] },
];

const NAMED = new Map<string, string>();
for (const feature of FEATURES) for (const key of feature.keys ?? []) NAMED.set(key, feature.key);

/** The feature an option belongs to: named first, then by prefix, then by its server group. */
export function featureOf(key: string, group: string): string {
  const named = NAMED.get(key);
  if (named) return named;
  for (const feature of FEATURES) if ((feature.prefixes ?? []).some((prefix) => key.startsWith(prefix))) return feature.key;
  for (const feature of FEATURES) if ((feature.groups ?? []).includes(group)) return feature.key;
  return "other";
}

/** The server's groups regathered by feature, in the order of FEATURES; "other" last, if any. */
export function byFeature<T extends { key: string }>(groups: Record<string, T[]>): Record<string, T[]> {
  const out: Record<string, T[]> = {};
  for (const [group, items] of Object.entries(groups)) {
    for (const item of items) (out[featureOf(item.key, group)] ??= []).push(item);
  }
  const ordered: Record<string, T[]> = {};
  for (const feature of [...FEATURES.map((f) => f.key), "other"]) if (out[feature]) ordered[feature] = out[feature];
  return ordered;
}
