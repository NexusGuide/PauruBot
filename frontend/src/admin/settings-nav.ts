// The bot's settings as two levels: a few areas on top, the area's pages below. A page is one
// feature's settings (`/settings?group=<feature>`, see settings-features.ts) or a page of its own
// (bot texts, button looks, menu, Mini App look, links). Every feature has one page and one only.
// Pure, so it is tested on its own.

export interface SettingsTab {
  key: string;
  to: string;
  permission?: string;
}

export interface SettingsNavGroup {
  key: string;
  tabs: SettingsTab[];
}

const group = (name: string): SettingsTab => ({ key: name, to: `/settings?group=${name}`, permission: "settings.read" });

export const SETTINGS_NAV: SettingsNavGroup[] = [
  {
    key: "look",
    tabs: [
      { key: "messages", to: "/texts?kind=messages", permission: "texts.write" },
      { key: "buttons", to: "/texts?kind=buttons", permission: "texts.write" },
      { key: "button_looks", to: "/button-looks", permission: "settings.read" },
      { key: "menu", to: "/bot-menu", permission: "settings.read" },
      { key: "miniapp", to: "/appearance", permission: "settings.read" },
    ],
  },
  { key: "sell", tabs: [group("buy"), group("renewal"), group("extras"), group("custom"), group("trial")] },
  { key: "service", tabs: [group("delivery"), group("notify"), group("cleanup"), group("sync")] },
  { key: "pay", tabs: [group("wallet"), group("card"), group("gateways"), group("crypto")] },
  { key: "grow", tabs: [group("referral"), group("games"), group("loyalty"), group("discount"), group("resellers")] },
  {
    key: "shop",
    tabs: [
      group("general"),
      group("signup"),
      group("tickets"),
      group("reports"),
      group("business"),
      { key: "links", to: "/quick-links", permission: "settings.read" },
      { key: "channels", to: "/channels", permission: "settings.read" },
      { key: "variables", to: "/variables", permission: "settings.read" },
    ],
  },
];

/** Settings features with a tab on this nav. */
export function navGroups(): string[] {
  return SETTINGS_NAV.flatMap((g) => g.tabs).flatMap((tab) => {
    const match = tab.to.match(/^\/settings\?group=(\w+)$/);
    return match ? [match[1]!] : [];
  });
}

/** Query values a page takes when the URL leaves them out. */
const DEFAULTS: Record<string, Record<string, string>> = {
  "/texts": { kind: "messages" },
  "/settings": { group: "general" },
};

/** Old links named a server group (`?group=services`); they open the feature that took its first options. */
export const OLD_GROUPS: Record<string, string> = {
  services: "renewal",
  payments: "wallet",
  growth: "referral",
  guest: "buy",
};

/** The feature a settings link opens: the one asked (or its old group's), else general, else the first. */
export function featureFor(asked: string | null, present: string[]): string | undefined {
  const wanted = asked ? (OLD_GROUPS[asked] ?? asked) : "general";
  return present.includes(wanted) ? wanted : present.includes("general") ? "general" : present[0];
}

function matchesTab(tab: SettingsTab, pathname: string, search: string): boolean {
  const [path, query = ""] = tab.to.split("?");
  if (path !== pathname) return false;
  const current = new URLSearchParams(search);
  for (const [key, value] of new URLSearchParams(query)) {
    const asked = current.get(key) ?? DEFAULTS[pathname]?.[key];
    if ((key === "group" && asked ? (OLD_GROUPS[asked] ?? asked) : asked) !== value) return false;
  }
  return true;
}

/** The group and tab of the page shown, if it is one of the settings pages. */
export function activeSettings(pathname: string, search: string): { group: SettingsNavGroup; tab: SettingsTab } | undefined {
  for (const g of SETTINGS_NAV) {
    for (const tab of g.tabs) if (matchesTab(tab, pathname, search)) return { group: g, tab };
  }
  return undefined;
}

/** The nav with only the tabs this person may open; groups left empty are dropped. */
export function visibleSettings(can: (permission: string) => boolean): SettingsNavGroup[] {
  return SETTINGS_NAV.map((g) => ({ key: g.key, tabs: g.tabs.filter((tab) => !tab.permission || can(tab.permission)) })).filter(
    (g) => g.tabs.length > 0,
  );
}
