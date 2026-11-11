// How the settings screen is laid out: groups gathered into a few categories, options that only
// matter while a switch is on, and search across everything. Pure, so it is tested on its own.

export interface SettingCategory {
  key: string;
  groups: string[];
}

// The groups of the approved design; looks, texts, menus and links have pages of their own.
export const CATEGORIES: SettingCategory[] = [
  { key: "general", groups: ["general"] },
  { key: "sales", groups: ["services", "trial", "custom", "guest"] },
  { key: "pay", groups: ["payments", "gateways", "crypto"] },
  { key: "growth", groups: ["growth", "resellers"] },
  { key: "alerts", groups: ["reports"] },
  { key: "other", groups: ["business"] },
];

/** The categories with the groups the server sent, in order; a group no category names goes last. */
export function categorize(groups: string[]): SettingCategory[] {
  const known = new Set(CATEGORIES.flatMap((category) => category.groups));
  const present = new Set(groups);
  const out = CATEGORIES.map((category) => ({
    key: category.key,
    groups: category.groups.filter((group) => present.has(group)),
  }));
  const other = out.find((category) => category.key === "other");
  for (const group of groups) if (!known.has(group)) other?.groups.push(group);
  return out.filter((category) => category.groups.length > 0);
}

// A switch named `<prefix>_enabled` controls the options named `<prefix>_…` in its group. These
// are the switches whose options are named otherwise, and the one whose prefix is not its family.
const CONTROLS: Record<string, string[]> = {
  require_phone: ["phone_country_codes", "phone_when"],
  terms_required: ["terms_version", "terms_when"],
  reseller_terms_required: ["reseller_terms_version"],
  guest_buy_enabled: ["guest_plan_ids"],
  extras_enabled: ["extra_gb_price", "extra_day_price"],
  crypto_enabled: ["crypto_networks", "tron_address", "ton_address", "fx_source", "usdt_rate", "trx_rate", "ton_rate", "fx_markup_percent"],
};
const PREFIX: Record<string, string> = { custom_buy_enabled: "custom_" };
/** Families that share a prefix but are their own gateway, not an option of the shorter one. */
const OWN_FAMILY: Record<string, string[]> = { tonpay_enabled: ["tonpay_custom_"] };

/** For each option, the switches that must all be on for it to matter. */
export function controllers(keys: string[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  const link = (key: string, toggle: string) => {
    if (key !== toggle) (out[key] ??= []).push(toggle);
  };
  for (const toggle of keys) {
    for (const key of CONTROLS[toggle] ?? []) if (keys.includes(key)) link(key, toggle);
    const prefix = PREFIX[toggle] ?? (toggle.endsWith("_enabled") ? toggle.slice(0, -"enabled".length) : null);
    if (!prefix) continue;
    for (const key of keys) {
      if (!key.startsWith(prefix)) continue;
      if ((OWN_FAMILY[toggle] ?? []).some((own) => key.startsWith(own))) continue;
      link(key, toggle);
    }
  }
  return out;
}

/** An option is shown while every switch that controls it is on. */
export function shown(key: string, deps: Record<string, string[]>, valueOf: (key: string) => unknown): boolean {
  return (deps[key] ?? []).every((toggle) => Boolean(valueOf(toggle)));
}

/** Search matches the option's label, its hint or its key, ignoring case and Persian/Arabic forms. */
export function normalize(text: string): string {
  return text.toLowerCase().replace(/ي/g, "ی").replace(/ك/g, "ک").replace(/‌/g, " ").trim();
}

export function matchesQuery(query: string, ...fields: (string | undefined)[]): boolean {
  const q = normalize(query);
  if (!q) return true;
  return fields.some((field) => field !== undefined && normalize(field).includes(q));
}
