import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { digits } from "@shared/format";
import type { Dashboard } from "./api";
import { call } from "./api";
import { useSession } from "./session";
import { useTheme } from "./theme";
import { Icon } from "./ui";
import {
  groupFor,
  hubCount,
  hubFor,
  hubLabel,
  hubTarget,
  matches,
  MOBILE_TABS,
  nextOpen,
  visibleFor,
  visibleGroups,
  type Hub,
  type NavGroup,
} from "./nav";
import { activeSettings, visibleSettings } from "./settings-nav";

export { NAV_GROUPS, type NavItem } from "./nav";

export function useGroups(): NavGroup[] {
  const { can, me } = useSession();
  return visibleGroups(visibleFor(can, me?.role));
}

type Counts = Partial<Record<"pending_receipts" | "open_tickets", number>>;

let lastCounts: { at: number; value: Counts } | null = null;

/** What waits in the receipts and tickets queues, for the badges beside their names (fresh each minute). */
export function useCounts(): Counts {
  const { can } = useSession();
  const { pathname } = useLocation();
  const [counts, setCounts] = useState<Counts>(lastCounts?.value ?? {});
  const allowed = can("dashboard.view");
  useEffect(() => {
    if (!allowed) return;
    if (lastCounts && Date.now() - lastCounts.at < 60_000) {
      setCounts(lastCounts.value);
      return;
    }
    let alive = true;
    call<Dashboard>("/api/admin/dashboard")
      .then((data) => {
        const value = { pending_receipts: data.pending_receipts ?? 0, open_tickets: data.open_tickets ?? 0 };
        lastCounts = { at: Date.now(), value };
        if (alive) setCounts(value);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [allowed, pathname]);
  return counts;
}

function CountPill({ n, tone = "wait" }: { n: number; tone?: "wait" | "brand" }) {
  const { locale } = useSession();
  if (n <= 0) return null;
  return (
    <span
      className={`num inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-[0.7rem] font-extrabold ${
        tone === "wait" ? "bg-saffron-500 text-[#1a1406]" : "bg-[var(--accent)] text-[var(--accent-ink)]"
      }`}
    >
      {digits(n, locale)}
    </span>
  );
}

// ---------------------------------------------------------------- desktop sidebar

const RAIL_KEY = "admin.nav.rail";

function useRail(): [boolean, (value: boolean) => void] {
  const [rail, setRail] = useState(() => {
    try {
      return localStorage.getItem(RAIL_KEY) === "1";
    } catch {
      return false;
    }
  });
  const set = (value: boolean) => {
    setRail(value);
    try {
      localStorage.setItem(RAIL_KEY, value ? "1" : "0");
    } catch {
      // private window: not remembered
    }
  };
  return [rail, set];
}

function SideEntry({
  hub,
  active,
  count,
  rail,
  nested,
}: {
  hub: Hub;
  active: boolean;
  count: number;
  rail: boolean;
  nested?: boolean;
}) {
  const { t } = useSession();
  const label = t(hubLabel(hub));
  return (
    <Link
      to={hubTarget(hub)}
      aria-current={active ? "page" : undefined}
      aria-label={rail ? label : undefined}
      title={rail ? label : undefined}
      className={`group relative flex min-h-10 items-center gap-3 rounded-xl text-[0.86rem] transition-colors ${
        rail ? "justify-center px-0" : nested ? "ps-9 pe-3" : "px-3"
      } ${
        active
          ? "bg-firouzeh-100 font-bold text-firouzeh-700"
          : "font-medium text-[var(--hint)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
      }`}
    >
      {(!nested || rail) && <Icon name={hub.icon} size={19} />}
      {!rail && <span className="min-w-0 flex-1 truncate">{label}</span>}
      {!rail && <CountPill n={count} />}
      {rail && count > 0 && <span className="absolute end-3 top-2 h-2 w-2 rounded-full bg-saffron-500" />}
    </Link>
  );
}

function Sidebar() {
  const { t } = useSession();
  const groups = useGroups();
  const counts = useCounts();
  const { pathname } = useLocation();
  const current = hubFor(groups, pathname);
  const here = groupFor(groups, pathname);
  const [rail, setRail] = useRail();
  const [open, setOpen] = useState<string | null>(here?.key ?? null);
  useEffect(() => {
    if (here) setOpen(here.key);
  }, [here?.key, pathname]);

  return (
    <aside
      className={`sticky top-0 hidden h-dvh shrink-0 flex-col gap-4 overflow-y-auto border-e border-[var(--line)] bg-[var(--side)] py-4 transition-[width] md:flex ${
        rail ? "w-[76px] px-2" : "w-[264px] px-3"
      }`}
    >
      <div className={`flex items-center gap-2.5 ${rail ? "flex-col" : "px-1.5"}`}>
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[var(--accent)] text-lg font-extrabold text-[var(--accent-ink)]">
          {t("brand").slice(0, 1)}
        </span>
        {!rail && (
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[0.95rem] font-extrabold">{t("brand")}</span>
            <span className="block text-[0.72rem] text-[var(--hint)]">{t("admin_panel")}</span>
          </span>
        )}
        <button
          type="button"
          onClick={() => setRail(!rail)}
          aria-label={rail ? t("nav_expand") : t("nav_collapse")}
          title={rail ? t("nav_expand") : t("nav_collapse")}
          className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-[var(--hint)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
        >
          <Icon name="sidebar" size={19} className={rail ? "" : "rtl:-scale-x-100"} />
        </button>
      </div>

      <nav aria-label={t("nav_main")} className="flex flex-1 flex-col gap-1">
        {groups.map((group) => {
          if (group.hubs.length === 1) {
            const hub = group.hubs[0]!;
            return (
              <SideEntry key={group.key} hub={hub} rail={rail} active={hub === current} count={hubCount(hub, counts)} />
            );
          }
          const expanded = !rail && open === group.key;
          const isHere = group === here;
          const waiting = group.hubs.reduce((sum, hub) => sum + hubCount(hub, counts), 0);
          const id = `nav-${group.key}`;
          return (
            <div key={group.key} className="flex flex-col gap-0.5">
              <button
                type="button"
                aria-expanded={expanded}
                aria-controls={id}
                aria-label={rail ? t(group.key) : undefined}
                title={rail ? t(group.key) : undefined}
                onClick={() => {
                  if (rail) {
                    setRail(false);
                    setOpen(group.key);
                  } else setOpen((before) => nextOpen(before, group.key));
                }}
                className={`relative flex min-h-10 w-full items-center gap-3 rounded-xl text-[0.86rem] font-semibold transition-colors ${
                  rail ? "justify-center" : "px-3"
                } ${
                  isHere && (rail || !expanded)
                    ? "bg-firouzeh-100 text-firouzeh-700"
                    : "text-[var(--text)] hover:bg-[var(--surface-2)]"
                }`}
              >
                <Icon name={group.icon} size={19} className={isHere ? "" : "opacity-80"} />
                {!rail && <span className="min-w-0 flex-1 truncate text-start">{t(group.key)}</span>}
                {!rail && !expanded && <CountPill n={waiting} />}
                {!rail && (
                  <Icon
                    name="down"
                    size={16}
                    className={`text-[var(--hint)] transition-transform ${expanded ? "rotate-180" : ""}`}
                  />
                )}
                {rail && waiting > 0 && <span className="absolute end-3 top-2 h-2 w-2 rounded-full bg-saffron-500" />}
              </button>
              {expanded && (
                <div id={id} className="flex flex-col gap-0.5 pb-1">
                  {group.hubs.map((hub) => (
                    <SideEntry
                      key={hub.key}
                      hub={hub}
                      nested
                      rail={false}
                      active={hub === current}
                      count={hubCount(hub, counts)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <AccountBlock rail={rail} />
    </aside>
  );
}

export function AccountBlock({ rail, onNavigate }: { rail?: boolean; onNavigate?: () => void }) {
  const { me, t, logout, locale, setLocale } = useSession();
  const [theme, toggleTheme] = useTheme();
  const name = me?.display_name || me?.username || "?";
  const quiet =
    "grid h-9 w-9 place-items-center rounded-lg text-[var(--hint)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]";
  return (
    <div className={`flex gap-2 border-t border-[var(--line)] pt-3 ${rail ? "flex-col items-center" : "flex-col"}`}>
      <NavLink
        to="/account"
        onClick={onNavigate}
        title={rail ? name : undefined}
        aria-label={rail ? name : undefined}
        className={`flex items-center gap-3 rounded-xl hover:bg-[var(--surface-2)] ${rail ? "p-1" : "px-2 py-1.5"}`}
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#2a2344] font-extrabold text-[#cdbbff]">
          {name.slice(0, 1).toUpperCase()}
        </span>
        {!rail && (
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-[0.88rem] font-bold">{name}</span>
            <span className="text-[0.75rem] text-[var(--hint)]">{t(`role_${me?.role ?? "admin"}`)}</span>
          </span>
        )}
      </NavLink>
      <div className={`flex gap-1 ${rail ? "flex-col" : "px-1"}`}>
        <button
          type="button"
          className={quiet}
          onClick={toggleTheme}
          aria-label={theme === "dark" ? t("theme_light") : t("theme_dark")}
          title={theme === "dark" ? t("theme_light") : t("theme_dark")}
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} size={18} />
        </button>
        <button
          type="button"
          className={`${quiet} w-auto px-2 text-[0.78rem] font-bold`}
          onClick={() => setLocale(locale === "fa" ? "en" : "fa")}
          aria-label={locale === "fa" ? "English" : "فارسی"}
          title={locale === "fa" ? "English" : "فارسی"}
        >
          {locale === "fa" ? "EN" : "فا"}
        </button>
        <button
          type="button"
          className={quiet}
          onClick={() => void logout()}
          aria-label={t("logout")}
          title={t("logout")}
        >
          <Icon name="logout" size={18} />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- tabs above a page

const tabClass = (active: boolean) =>
  `flex min-h-10 shrink-0 items-center gap-2 whitespace-nowrap rounded-xl px-3.5 text-[0.86rem] font-semibold transition-colors ${
    active
      ? "bg-[var(--surface-2)] text-[var(--text)] ring-1 ring-[var(--line)]"
      : "text-[var(--hint)] hover:text-[var(--text)]"
  }`;

/** The bot's settings as two levels: a few groups on top, the group's pages below. */
function SettingsNav() {
  const { t, can } = useSession();
  const { pathname, search } = useLocation();
  const groups = visibleSettings(can);
  const here = activeSettings(pathname, search);
  const current = groups.find((g) => g.key === here?.group.key) ?? groups[0];
  if (!current) return null;
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-2 px-4 pt-4 md:px-8 md:pt-6">
      <nav aria-label={t("nav_hub_bot")} className="no-scrollbar flex gap-2 overflow-x-auto">
        {groups.map((g) => (
          <Link
            key={g.key}
            to={g.tabs[0]!.to}
            aria-current={g === current ? "true" : undefined}
            className={`flex min-h-10 shrink-0 items-center whitespace-nowrap rounded-full px-4 text-[0.86rem] font-bold transition-colors ${
              g === current
                ? "bg-[var(--accent)] text-[var(--accent-ink)]"
                : "bg-[var(--surface)] text-[var(--text)] ring-1 ring-[var(--line)] hover:bg-[var(--surface-2)]"
            }`}
          >
            {t(`settings_nav_${g.key}`)}
          </Link>
        ))}
      </nav>
      {current.tabs.length > 1 && (
        <nav
          aria-label={t(`settings_nav_${current.key}`)}
          className="no-scrollbar flex gap-1 overflow-x-auto rounded-2xl bg-[var(--surface)] p-1 ring-1 ring-[var(--line)]"
        >
          {current.tabs.map((tab) => {
            const active = here?.tab.key === tab.key;
            return (
              <Link key={tab.key} to={tab.to} aria-current={active ? "page" : undefined} className={tabClass(active)}>
                {t(`settings_tab_${tab.key}`)}
              </Link>
            );
          })}
        </nav>
      )}
    </div>
  );
}

/** The pages of the current hub, as tabs above the page. */
function HubTabs({ hub }: { hub: Hub }) {
  const { t } = useSession();
  const { pathname } = useLocation();
  const counts = useCounts();
  return (
    <div className="mx-auto w-full max-w-7xl px-4 pt-4 md:px-8 md:pt-6">
      <nav
        aria-label={t(hub.key)}
        className="no-scrollbar flex w-fit max-w-full gap-1 overflow-x-auto rounded-2xl bg-[var(--surface)] p-1 ring-1 ring-[var(--line)]"
      >
        {hub.tabs.map((item) => {
          const active = matches(item, pathname);
          const field = item.to === "/payments" ? "pending_receipts" : item.to === "/tickets" ? "open_tickets" : null;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              aria-current={active ? "page" : undefined}
              className={tabClass(active)}
            >
              {t(item.key)}
              {field && <CountPill n={counts[field] ?? 0} />}
            </NavLink>
          );
        })}
      </nav>
    </div>
  );
}

// ---------------------------------------------------------------- phone bars

function MobileHeader({ title }: { title: string }) {
  const { t, me } = useSession();
  const name = me?.display_name || me?.username || "?";
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-[var(--line)] bg-[var(--bg)]/90 px-4 backdrop-blur md:hidden">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] text-[0.95rem] font-extrabold text-[var(--accent-ink)]">
        {t("brand").slice(0, 1)}
      </span>
      <p className="min-w-0 flex-1 truncate text-[0.95rem] font-bold">{title}</p>
      <Link
        to="/account"
        aria-label={name}
        className="flex h-9 w-9 items-center justify-center rounded-full bg-[#2a2344] text-[0.85rem] font-extrabold text-[#cdbbff]"
      >
        {name.slice(0, 1).toUpperCase()}
      </Link>
    </header>
  );
}

function BottomBar({ current }: { current: Hub | undefined }) {
  const { t } = useSession();
  const groups = useGroups();
  const counts = useCounts();
  const { pathname } = useLocation();
  const hubs = groups.flatMap((group) => group.hubs);
  const tabs = MOBILE_TABS.flatMap((tab) => {
    const hub = hubs.find((h) => h.key === tab.hub || hubLabel(h) === tab.hub);
    return hub ? [{ ...tab, target: hub }] : [];
  });
  const onMore = pathname === "/more" || !tabs.some((tab) => tab.target.key === current?.key);
  const item = (active: boolean) =>
    `flex flex-col items-center gap-1 pb-1 pt-2 text-[0.7rem] whitespace-nowrap ${
      active ? "font-bold text-[var(--accent)]" : "font-semibold text-[var(--hint)]"
    }`;
  const pill = (active: boolean) =>
    `relative flex h-8 w-14 items-center justify-center rounded-full ${active ? "bg-firouzeh-100" : ""}`;
  return (
    <nav
      aria-label={t("nav_main")}
      className="fixed inset-x-0 bottom-0 z-20 border-t border-[var(--line)] bg-[var(--side)]/95 backdrop-blur md:hidden"
      style={{ paddingBottom: "max(0.4rem, env(safe-area-inset-bottom))" }}
    >
      <ul className="grid grid-cols-5">
        {tabs.map((tab) => {
          const active = tab.target.key === current?.key && pathname !== "/more";
          const n = hubCount(tab.target, counts);
          return (
            <li key={tab.hub}>
              <Link to={hubTarget(tab.target)} aria-current={active ? "page" : undefined} className={item(active)}>
                <span className={pill(active)}>
                  <Icon name={tab.icon} size={21} />
                  {n > 0 && (
                    <span className="absolute -top-0.5 end-2">
                      <CountPill n={n} />
                    </span>
                  )}
                </span>
                {t(tab.label)}
              </Link>
            </li>
          );
        })}
        <li>
          <Link to="/more" aria-current={onMore ? "page" : undefined} className={item(onMore)}>
            <span className={pill(onMore)}>
              <Icon name="grid" size={21} />
            </span>
            {t("nav_more")}
          </Link>
        </li>
      </ul>
    </nav>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { t } = useSession();
  const groups = useGroups();
  const location = useLocation();
  const current = hubFor(groups, location.pathname);
  const title =
    location.pathname === "/more" ? t("nav_more") : location.pathname === "/account" ? t("account") : current ? t(hubLabel(current)) : "";

  return (
    <div className="min-h-dvh md:flex">
      <Sidebar />
      <MobileHeader title={title} />
      <main className="min-w-0 flex-1">
        {current?.key === "nav_hub_bot" ? <SettingsNav /> : current && current.tabs.length > 1 && <HubTabs hub={current} />}
        {children}
      </main>
      <BottomBar current={current} />
    </div>
  );
}
