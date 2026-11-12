// The admin panel's navigation: a short sidebar of hubs, each a set of pages shown as tabs.
// Every page keeps its own URL; a hub only groups them, so bookmarks and links keep working.

export interface NavItem {
  to: string;
  key: string;
  icon: string;
  permission?: string;
  /** Hidden from resellers, whose scope is only their own customers. */
  shopWide?: boolean;
  /** Only for reseller accounts. */
  resellerOnly?: boolean;
}

export interface Hub {
  /** Sidebar label; for a one-page hub, the page's own label. */
  key: string;
  icon: string;
  tabs: NavItem[];
}

export interface NavGroup {
  key: string;
  icon: string;
  hubs: Hub[];
}

const page = (to: string, key: string, icon: string, extra: Partial<NavItem> = {}): NavItem => ({
  to,
  key,
  icon,
  ...extra,
});
const single = (item: NavItem): Hub => ({ key: item.key, icon: item.icon, tabs: [item] });

export const NAV_GROUPS: NavGroup[] = [
  {
    key: "nav_group_daily",
    icon: "inbox",
    hubs: [
      single(page("/", "nav_inbox", "inbox", { permission: "dashboard.view" })),
      {
        key: "nav_hub_customers",
        icon: "users",
        tabs: [
          page("/customers", "nav_customers", "users", { permission: "customers.read", shopWide: true }),
          page("/services", "nav_services", "services", { permission: "services.read" }),
          page("/trial-users", "nav_trial_users", "users", { permission: "customers.read", shopWide: true }),
        ],
      },
      {
        key: "nav_hub_sales",
        icon: "payments",
        tabs: [
          page("/payments", "nav_payments", "payments", { permission: "payments.read" }),
          page("/orders", "nav_orders", "orders", { permission: "orders.read" }),
          page("/stuck-orders", "nav_stuck_orders", "orders", { permission: "orders.read", shopWide: true }),
        ],
      },
      single(page("/tickets", "nav_tickets", "tickets", { permission: "tickets.read" })),
      single(page("/billing", "nav_reseller_hub", "resellers", { permission: "dashboard.view", resellerOnly: true })),
    ],
  },
  {
    key: "nav_group_shop",
    icon: "catalog",
    hubs: [
      {
        key: "nav_hub_products",
        icon: "catalog",
        tabs: [
          page("/catalog", "nav_catalog", "catalog", { permission: "catalog.read", shopWide: true }),
          page("/manual-sales", "nav_manual_sales", "catalog", { permission: "catalog.read", shopWide: true }),
        ],
      },
      single(page("/admin-accounts", "nav_admin_accounts", "panels", { permission: "resellers.manage", shopWide: true })),
      {
        key: "nav_hub_panels",
        icon: "panels",
        tabs: [
          page("/panels", "nav_panels", "panels", { permission: "panels.read" }),
          page("/connection", "nav_connection", "trend", { permission: "panels.read" }),
        ],
      },
    ],
  },
  {
    key: "nav_group_marketing",
    icon: "broadcast",
    hubs: [
      {
        key: "nav_hub_marketing",
        icon: "broadcast",
        tabs: [
          page("/broadcasts", "nav_broadcasts", "broadcast", { permission: "broadcast.send" }),
          page("/announcements", "nav_announcements", "inbox", { permission: "broadcast.send", shopWide: true }),
          page("/lotteries", "nav_lotteries", "discounts", { permission: "broadcast.send", shopWide: true }),
        ],
      },
      single(page("/discounts", "nav_discounts", "discounts", { permission: "discounts.write" })),
    ],
  },
  {
    key: "nav_group_resellers",
    icon: "resellers",
    hubs: [
      single(page("/resellers", "nav_resellers", "resellers", { permission: "resellers.manage" })),
      single(page("/customer-bots", "nav_customer_bots", "bot", { permission: "resellers.manage", shopWide: true })),
      {
        key: "nav_hub_my_sales",
        icon: "catalog",
        tabs: [
          page("/desk", "nav_desk", "catalog", { permission: "services.create", resellerOnly: true }),
          page("/sub-resellers", "nav_sub_resellers", "resellers", { permission: "resellers.sub", resellerOnly: true }),
          page("/my-bot", "nav_my_bot", "channels", { permission: "resellers.bot", resellerOnly: true }),
        ],
      },
    ],
  },
  {
    key: "nav_group_reports",
    icon: "reports",
    hubs: [single(page("/reports", "nav_reports", "reports", { permission: "reports.read" }))],
  },
  {
    key: "nav_group_settings",
    icon: "settings",
    hubs: [
      {
        key: "nav_hub_bot",
        icon: "settings",
        tabs: [
          page("/settings", "nav_settings", "settings", { permission: "settings.read" }),
          page("/texts", "nav_texts", "texts", { permission: "texts.write" }),
          page("/bot-menu", "nav_bot_menu", "texts", { permission: "settings.read" }),
          page("/button-looks", "nav_button_looks", "texts", { permission: "settings.read" }),
          page("/appearance", "nav_appearance", "account", { permission: "settings.read" }),
          page("/quick-links", "nav_quick_links", "link", { permission: "settings.read" }),
          page("/variables", "nav_variables", "variable", { permission: "settings.read" }),
          page("/channels", "nav_channels", "channels", { permission: "settings.read" }),
        ],
      },
      single(page("/staff", "nav_staff", "staff", { permission: "staff.manage" })),
    ],
  },
  {
    key: "nav_group_system",
    icon: "jobs",
    hubs: [
      {
        key: "nav_hub_server",
        icon: "jobs",
        tabs: [
          page("/health", "nav_health", "jobs", { permission: "settings.write" }),
          page("/jobs", "nav_jobs", "jobs", { permission: "settings.write" }),
          page("/backups", "nav_backups", "backup", { permission: "backups.manage" }),
          page("/migration", "nav_migration", "backup", { permission: "backups.manage" }),
          page("/updates", "nav_updates", "refresh", { permission: "backups.manage" }),
        ],
      },
      single(page("/audit", "nav_audit", "audit", { permission: "audit.read" })),
      single(page("/help", "nav_help", "help")),
    ],
  },
];

/** The phone's bottom bar, before "more": a hub and the short word under its icon. */
export const MOBILE_TABS: { hub: string; label: string; icon: string }[] = [
  { hub: "nav_inbox", label: "nav_tab_home", icon: "home" },
  { hub: "nav_hub_customers", label: "nav_tab_customers", icon: "users" },
  { hub: "nav_hub_sales", label: "nav_tab_sales", icon: "orders" },
  { hub: "nav_hub_marketing", label: "nav_tab_messages", icon: "broadcast" },
];

/** Pages with a count beside their name: the figure from the dashboard that says what waits there. */
export const NAV_COUNTS: Record<string, "pending_receipts" | "open_tickets"> = {
  "/payments": "pending_receipts",
  "/tickets": "open_tickets",
};

/** The count shown beside a hub: the sum of its pages' counts. */
export function hubCount(hub: Hub, counts: Partial<Record<"pending_receipts" | "open_tickets", number>>): number {
  return hub.tabs.reduce((sum, item) => {
    const field = NAV_COUNTS[item.to];
    return sum + (field ? (counts[field] ?? 0) : 0);
  }, 0);
}

export type VisibleFn = (item: NavItem) => boolean;

export function visibleFor(
  can: (permission: string) => boolean,
  role: string | undefined,
): VisibleFn {
  return (item) => {
    if (item.permission && !can(item.permission)) return false;
    if (item.shopWide && role === "reseller") return false;
    if (item.resellerOnly && role !== "reseller") return false;
    return true;
  };
}

/** A hub with only the tabs this person may open; hubs left with none are dropped. */
export function visibleGroups(visible: VisibleFn): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    key: group.key,
    icon: group.icon,
    hubs: group.hubs
      .map((hub) => ({ ...hub, tabs: hub.tabs.filter(visible) }))
      .filter((hub) => hub.tabs.length > 0),
  })).filter((group) => group.hubs.length > 0);
}

export function matches(item: NavItem, pathname: string): boolean {
  if (item.to === "/") return pathname === "/";
  return pathname === item.to || pathname.startsWith(`${item.to}/`);
}

/** The hub the current page belongs to, among the visible ones. */
export function hubFor(groups: NavGroup[], pathname: string): Hub | undefined {
  for (const group of groups) {
    for (const hub of group.hubs) {
      if (hub.tabs.some((item) => matches(item, pathname))) return hub;
    }
  }
  return undefined;
}

/** The group holding the current page: the one the sidebar keeps open. */
export function groupFor(groups: NavGroup[], pathname: string): NavGroup | undefined {
  return groups.find((group) => group.hubs.some((hub) => hub.tabs.some((item) => matches(item, pathname))));
}

/** Where a hub's sidebar entry leads: its first page this person may open. */
export function hubTarget(hub: Hub): string {
  return hub.tabs[0]?.to ?? "/";
}

/** The sidebar label: the hub's name, or its page's own when the hub has one page. */
export function hubLabel(hub: Hub): string {
  return hub.tabs.length === 1 ? (hub.tabs[0]?.key ?? hub.key) : hub.key;
}

export function allPaths(): string[] {
  return NAV_GROUPS.flatMap((group) => group.hubs.flatMap((hub) => hub.tabs.map((item) => item.to)));
}

/**
 * The sidebar keeps one folder open at a time: opening a folder closes the one that was open, and
 * clicking the open one closes it. (Moving to a page opens that page's folder.)
 */
export function nextOpen(open: string | null, clicked: string): string | null {
  return open === clicked ? null : clicked;
}
