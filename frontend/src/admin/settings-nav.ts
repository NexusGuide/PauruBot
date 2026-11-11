// The bot's settings as two levels, like the approved design: a few groups on top, the active
// group's tabs below. A tab is a settings group (`/settings?group=…`) or a page of its own (bot
// texts, menu, Mini App look, links). Pure, so it is tested on its own.

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
      { key: "menu", to: "/bot-menu", permission: "settings.read" },
      { key: "miniapp", to: "/appearance", permission: "settings.read" },
      group("general"),
    ],
  },
  {
    key: "sales",
    tabs: [group("services"), group("trial"), group("custom"), group("guest"), { key: "variables", to: "/variables", permission: "settings.read" }],
  },
  { key: "pay", tabs: [group("payments"), group("gateways"), group("crypto")] },
  { key: "growth", tabs: [group("growth"), group("resellers")] },
  { key: "alerts", tabs: [group("reports"), group("business")] },
  {
    key: "connect",
    tabs: [
      { key: "links", to: "/quick-links", permission: "settings.read" },
      { key: "channels", to: "/channels", permission: "settings.read" },
    ],
  },
];

/** Settings groups whose tab is on this nav; any other group from the server still opens by its URL. */
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

function matchesTab(tab: SettingsTab, pathname: string, search: string): boolean {
  const [path, query = ""] = tab.to.split("?");
  if (path !== pathname) return false;
  const current = new URLSearchParams(search);
  for (const [key, value] of new URLSearchParams(query)) {
    if ((current.get(key) ?? DEFAULTS[pathname]?.[key]) !== value) return false;
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
