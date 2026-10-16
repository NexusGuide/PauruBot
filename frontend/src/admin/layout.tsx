import { useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useSession } from "./session";
import { Button, Icon, Sheet } from "./ui";

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

export const NAV_GROUPS: { key: string; items: NavItem[] }[] = [
  {
    key: "nav_group_daily",
    items: [
      { to: "/", key: "nav_inbox", icon: "inbox", permission: "dashboard.view" },
      { to: "/customers", key: "nav_customers", icon: "users", permission: "customers.read", shopWide: true },
      { to: "/services", key: "nav_services", icon: "services", permission: "services.read" },
      { to: "/orders", key: "nav_orders", icon: "orders", permission: "orders.read" },
      { to: "/payments", key: "nav_payments", icon: "payments", permission: "payments.read" },
      { to: "/tickets", key: "nav_tickets", icon: "tickets", permission: "tickets.read" },
      { to: "/billing", key: "nav_reseller_hub", icon: "resellers", permission: "dashboard.view", resellerOnly: true },
    ],
  },
  {
    key: "nav_group_shop",
    items: [
      { to: "/catalog", key: "nav_catalog", icon: "catalog", permission: "catalog.read", shopWide: true },
      { to: "/panels", key: "nav_panels", icon: "panels", permission: "panels.read" },
      { to: "/discounts", key: "nav_discounts", icon: "discounts", permission: "discounts.write" },
      { to: "/broadcasts", key: "nav_broadcasts", icon: "broadcast", permission: "broadcast.send" },
      { to: "/lotteries", key: "nav_lotteries", icon: "discounts", permission: "broadcast.send", shopWide: true },
      { to: "/resellers", key: "nav_resellers", icon: "resellers", permission: "resellers.manage" },
      { to: "/admin-accounts", key: "nav_admin_accounts", icon: "panels", permission: "resellers.manage", shopWide: true },
      { to: "/desk", key: "nav_desk", icon: "catalog", permission: "services.create", resellerOnly: true },
      { to: "/sub-resellers", key: "nav_sub_resellers", icon: "resellers", permission: "resellers.sub", resellerOnly: true },
      { to: "/my-bot", key: "nav_my_bot", icon: "channels", permission: "resellers.bot", resellerOnly: true },
      { to: "/reports", key: "nav_reports", icon: "reports", permission: "reports.read" },
    ],
  },
  {
    key: "nav_group_system",
    items: [
      { to: "/settings", key: "nav_settings", icon: "settings", permission: "settings.read" },
      { to: "/texts", key: "nav_texts", icon: "texts", permission: "texts.write" },
      { to: "/bot-menu", key: "nav_bot_menu", icon: "texts", permission: "settings.read" },
      { to: "/channels", key: "nav_channels", icon: "channels", permission: "settings.read" },
      { to: "/staff", key: "nav_staff", icon: "staff", permission: "staff.manage" },
      { to: "/audit", key: "nav_audit", icon: "audit", permission: "audit.read" },
      { to: "/jobs", key: "nav_jobs", icon: "jobs", permission: "settings.write" },
      { to: "/health", key: "nav_health", icon: "jobs", permission: "settings.write" },
      { to: "/backups", key: "nav_backups", icon: "backup", permission: "backups.manage" },
      { to: "/migration", key: "nav_migration", icon: "backup", permission: "backups.manage" },
      { to: "/updates", key: "nav_updates", icon: "refresh", permission: "backups.manage" },
      { to: "/help", key: "nav_help", icon: "help" },
    ],
  },
];

const MOBILE_TABS = ["/", "/customers", "/services", "/payments"];

function useVisible(): (item: NavItem) => boolean {
  const { can, me } = useSession();
  return (item) => {
    if (item.permission && !can(item.permission)) return false;
    if (item.shopWide && me?.role === "reseller") return false;
    if (item.resellerOnly && me?.role !== "reseller") return false;
    return true;
  };
}

function NavEntry({ item, onNavigate }: { item: NavItem; onNavigate?: () => void }) {
  const { t } = useSession();
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      onClick={onNavigate}
      className={({ isActive }) =>
        `flex items-center gap-3 rounded-xl px-3 py-2 text-[0.88rem] font-semibold transition-colors ${
          isActive
            ? "bg-firouzeh-600 text-white"
            : "text-[var(--hint)] hover:bg-[var(--line)] hover:text-[var(--text)]"
        }`
      }
    >
      <Icon name={item.icon} size={19} />
      {t(item.key)}
    </NavLink>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useSession();
  const visible = useVisible();
  return (
    <nav className="flex flex-col gap-5">
      {NAV_GROUPS.map((group) => {
        const items = group.items.filter(visible);
        if (items.length === 0) return null;
        return (
          <div key={group.key} className="flex flex-col gap-0.5">
            <p className="px-3 pb-1 text-[0.75rem] font-semibold text-[var(--hint)]">{t(group.key)}</p>
            {items.map((item) => (
              <NavEntry key={item.to} item={item} onNavigate={onNavigate} />
            ))}
          </div>
        );
      })}
    </nav>
  );
}

function AccountBlock({ onNavigate }: { onNavigate?: () => void }) {
  const { me, t, logout, locale, setLocale } = useSession();
  return (
    <div className="flex flex-col gap-2 border-t border-[var(--line)] pt-4">
      <NavLink
        to="/account"
        onClick={onNavigate}
        className="flex items-center gap-3 rounded-xl px-3 py-2 hover:bg-[var(--line)]"
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-firouzeh-100 font-extrabold text-firouzeh-700">
          {(me?.display_name || me?.username || "?").slice(0, 1).toUpperCase()}
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-[0.88rem] font-bold">{me?.display_name || me?.username}</span>
          <span className="text-[0.75rem] text-[var(--hint)]">{t(`role_${me?.role ?? "admin"}`)}</span>
        </span>
      </NavLink>
      <div className="flex gap-2 px-1">
        <Button small tone="ghost" onClick={() => setLocale(locale === "fa" ? "en" : "fa")}>
          {locale === "fa" ? "English" : "فارسی"}
        </Button>
        <Button small tone="ghost" icon="logout" onClick={() => void logout()}>
          {t("logout")}
        </Button>
      </div>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { t } = useSession();
  const visible = useVisible();
  const location = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);
  const allItems = NAV_GROUPS.flatMap((group) => group.items).filter(visible);
  const tabs = MOBILE_TABS.map((to) => allItems.find((item) => item.to === to)).filter(
    (item): item is NavItem => Boolean(item),
  );
  const current = allItems.find((item) =>
    item.to === "/" ? location.pathname === "/" : location.pathname.startsWith(item.to),
  );

  return (
    <div className="min-h-dvh md:flex">
      <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col gap-6 overflow-y-auto border-e border-[var(--line)] bg-[var(--surface)] px-3 py-5 md:flex">
        <p className="px-3 text-xl font-extrabold tracking-tight text-firouzeh-700">{t("brand")}</p>
        <div className="flex-1">
          <NavList />
        </div>
        <AccountBlock />
      </aside>

      <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-[var(--line)] bg-[var(--bg)]/95 px-4 backdrop-blur md:hidden">
        <p className="font-extrabold text-firouzeh-700">{t("brand")}</p>
        <p className="text-[0.85rem] font-semibold text-[var(--hint)]">{current ? t(current.key) : ""}</p>
      </header>

      <main className="min-w-0 flex-1">{children}</main>

      <nav
        className="fixed inset-x-0 bottom-0 z-20 border-t border-[var(--line)] bg-[var(--bg)]/95 backdrop-blur md:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <ul className="flex">
          {tabs.map((item) => (
            <li key={item.to} className="flex-1">
              <NavLink
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `flex flex-col items-center gap-0.5 py-2 text-[0.7rem] font-semibold ${
                    isActive ? "text-firouzeh-600" : "text-[var(--hint)]"
                  }`
                }
              >
                <Icon name={item.icon} size={22} />
                {t(item.key)}
              </NavLink>
            </li>
          ))}
          <li className="flex-1">
            <button
              type="button"
              onClick={() => setMoreOpen(true)}
              className="flex w-full flex-col items-center gap-0.5 py-2 text-[0.7rem] font-semibold text-[var(--hint)]"
            >
              <Icon name="more" size={22} />
              {t("nav_more")}
            </button>
          </li>
        </ul>
      </nav>

      <Sheet open={moreOpen} onClose={() => setMoreOpen(false)} title={t("nav_more")}>
        <div className="flex flex-col gap-6 pb-4">
          <NavList onNavigate={() => setMoreOpen(false)} />
          <AccountBlock onNavigate={() => setMoreOpen(false)} />
        </div>
      </Sheet>
    </div>
  );
}
