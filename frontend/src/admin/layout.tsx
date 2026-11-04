import { useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useSession } from "./session";
import { Button, Icon, Sheet } from "./ui";
import {
  hubFor,
  hubLabel,
  hubTarget,
  matches,
  MOBILE_HUBS,
  visibleFor,
  visibleGroups,
  type Hub,
  type NavGroup,
} from "./nav";

export { NAV_GROUPS, type NavItem } from "./nav";

function useGroups(): NavGroup[] {
  const { can, me } = useSession();
  return visibleGroups(visibleFor(can, me?.role));
}

function HubEntry({ hub, active, onNavigate }: { hub: Hub; active: boolean; onNavigate?: () => void }) {
  const { t } = useSession();
  return (
    // A plain link: the hub is active on any of its pages, not only the one it opens (NavLink
    // would mark aria-current only on that one).
    <Link
      to={hubTarget(hub)}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-3 rounded-xl px-3 py-2 text-[0.88rem] font-semibold transition-colors ${
        active
          ? "bg-firouzeh-600 text-white"
          : "text-[var(--hint)] hover:bg-[var(--line)] hover:text-[var(--text)]"
      }`}
    >
      <Icon name={hub.icon} size={19} />
      {t(hubLabel(hub))}
    </Link>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useSession();
  const groups = useGroups();
  const { pathname } = useLocation();
  const current = hubFor(groups, pathname);
  return (
    <nav className="flex flex-col gap-5">
      {groups.map((group) => (
        <div key={group.key} className="flex flex-col gap-0.5">
          <p className="px-3 pb-1 text-[0.75rem] font-semibold text-[var(--hint)]">{t(group.key)}</p>
          {group.hubs.map((hub) => (
            <HubEntry key={hub.key} hub={hub} active={hub === current} onNavigate={onNavigate} />
          ))}
        </div>
      ))}
    </nav>
  );
}

/** The pages of the current hub, as tabs above the page (APP-ADMIN regroup). */
function HubTabs({ hub }: { hub: Hub }) {
  const { t } = useSession();
  const { pathname } = useLocation();
  return (
    <div className="mx-auto w-full max-w-7xl px-4 pt-4 md:px-8 md:pt-6">
      <nav aria-label={t(hub.key)} className="flex gap-1 overflow-x-auto border-b border-[var(--line)]">
        {hub.tabs.map((item) => {
          const active = matches(item, pathname);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              aria-current={active ? "page" : undefined}
              className={`-mb-px flex min-h-11 shrink-0 items-center gap-2 whitespace-nowrap border-b-[3px] px-4 text-[0.9rem] font-semibold transition-colors ${
                active
                  ? "border-firouzeh-600 text-firouzeh-700"
                  : "border-transparent text-[var(--hint)] hover:text-[var(--text)]"
              }`}
            >
              <Icon name={item.icon} size={17} />
              {t(item.key)}
            </NavLink>
          );
        })}
      </nav>
    </div>
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
  const groups = useGroups();
  const location = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);
  const hubs = groups.flatMap((group) => group.hubs);
  const tabs = MOBILE_HUBS.map((key) => hubs.find((hub) => hub.key === key || hubLabel(hub) === key)).filter(
    (hub): hub is Hub => Boolean(hub),
  );
  const current = hubFor(groups, location.pathname);
  const label = (hub: Hub) => t(hubLabel(hub));

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
        <p className="text-[0.85rem] font-semibold text-[var(--hint)]">{current ? label(current) : ""}</p>
      </header>

      <main className="min-w-0 flex-1">
        {current && current.tabs.length > 1 && <HubTabs hub={current} />}
        {children}
      </main>

      <nav
        className="fixed inset-x-0 bottom-0 z-20 border-t border-[var(--line)] bg-[var(--bg)]/95 backdrop-blur md:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <ul className="flex">
          {tabs.map((hub) => (
            <li key={hub.key} className="flex-1">
              <Link
                to={hubTarget(hub)}
                aria-current={hub === current ? "page" : undefined}
                className={`flex flex-col items-center gap-0.5 py-2 text-[0.7rem] font-semibold ${
                  hub === current ? "text-firouzeh-600" : "text-[var(--hint)]"
                }`}
              >
                <Icon name={hub.icon} size={22} />
                {label(hub)}
              </Link>
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
