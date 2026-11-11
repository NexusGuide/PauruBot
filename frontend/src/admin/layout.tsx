import { useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { digits } from "@shared/format";
import { useSession } from "./session";
import { Button, Icon, Sheet } from "./ui";
import {
  groupFor,
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
import { activeSettings, visibleSettings } from "./settings-nav";

export { NAV_GROUPS, type NavItem } from "./nav";

function useGroups(): NavGroup[] {
  const { can, me } = useSession();
  return visibleGroups(visibleFor(can, me?.role));
}

// The sidebar is dark in both themes (as in the approved design); the sheet on phones is not.
const SIDE = {
  dark: {
    entry: "text-white/70 hover:bg-white/10 hover:text-white",
    active: "bg-firouzeh-600 text-white",
    group: "text-white/90 hover:bg-white/5",
    openGroup: "bg-white/[0.04]",
    badge: "bg-white/10 text-white/70",
  },
  light: {
    entry: "text-[var(--hint)] hover:bg-[var(--line)] hover:text-[var(--text)]",
    active: "bg-firouzeh-600 text-white",
    group: "text-[var(--text)] hover:bg-[var(--line)]",
    openGroup: "",
    badge: "bg-[var(--line)] text-[var(--hint)]",
  },
};
type Tone = keyof typeof SIDE;

function HubEntry({ hub, active, tone, onNavigate }: { hub: Hub; active: boolean; tone: Tone; onNavigate?: () => void }) {
  const { t } = useSession();
  return (
    // A plain link: the hub is active on any of its pages, not only the one it opens (NavLink
    // would mark aria-current only on that one).
    <Link
      to={hubTarget(hub)}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-3 rounded-xl px-3 py-2 text-[0.86rem] font-semibold transition-colors ${
        active ? SIDE[tone].active : SIDE[tone].entry
      }`}
    >
      <Icon name={hub.icon} size={18} />
      <span className="min-w-0 flex-1 truncate">{t(hubLabel(hub))}</span>
    </Link>
  );
}

const OPEN_KEY = "admin.nav.open";

function readOpen(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(OPEN_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((key) => typeof key === "string") : [];
  } catch {
    return [];
  }
}

/** The groups the person opened by hand, remembered on this browser; the current page's is always open. */
function useOpenGroups(): [Set<string>, (key: string) => void] {
  const [open, setOpen] = useState<Set<string>>(() => new Set(readOpen()));
  const toggle = (key: string) =>
    setOpen((before) => {
      const next = new Set(before);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      try {
        localStorage.setItem(OPEN_KEY, JSON.stringify([...next]));
      } catch {
        // private window: the choice just isn't remembered
      }
      return next;
    });
  return [open, toggle];
}

function NavList({ onNavigate, tone = "dark" }: { onNavigate?: () => void; tone?: Tone }) {
  const { t, locale } = useSession();
  const groups = useGroups();
  const { pathname } = useLocation();
  const current = hubFor(groups, pathname);
  const here = groupFor(groups, pathname);
  const [open, toggle] = useOpenGroups();
  return (
    <nav className="flex flex-col gap-1">
      {groups.map((group) => {
        // A group with one entry is that entry, not a folder.
        if (group.hubs.length === 1) {
          const hub = group.hubs[0]!;
          return <HubEntry key={group.key} hub={hub} tone={tone} active={hub === current} onNavigate={onNavigate} />;
        }
        const isHere = group === here;
        const expanded = isHere || open.has(group.key);
        const id = `nav-${group.key}`;
        return (
          <div key={group.key} className={`rounded-2xl ${expanded ? SIDE[tone].openGroup : ""}`}>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={id}
              disabled={isHere}
              onClick={() => toggle(group.key)}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-[0.86rem] font-bold transition-colors disabled:cursor-default ${SIDE[tone].group}`}
            >
              <Icon name={group.icon} size={18} />
              <span className="min-w-0 flex-1 truncate text-start">{t(group.key)}</span>
              {!expanded && (
                <span className={`rounded-full px-2 text-[0.7rem] font-semibold ${SIDE[tone].badge}`}>
                  {digits(group.hubs.length, locale)}
                </span>
              )}
              <Icon
                name="chevron"
                size={15}
                className={`opacity-60 transition-transform ${expanded ? "rotate-90" : "rtl:rotate-180"}`}
              />
            </button>
            {expanded && (
              <div id={id} className="flex flex-col gap-0.5 px-1 pb-2 ps-4">
                {group.hubs.map((hub) => (
                  <HubEntry key={hub.key} hub={hub} tone={tone} active={hub === current} onNavigate={onNavigate} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}

/** The bot's settings as two levels (groups, then the group's tabs), as in the approved design. */
function SettingsNav() {
  const { t, can } = useSession();
  const { pathname, search } = useLocation();
  const groups = visibleSettings(can);
  const here = activeSettings(pathname, search);
  const current = groups.find((g) => g.key === here?.group.key) ?? groups[0];
  if (!current) return null;
  const pill = (active: boolean) =>
    `flex min-h-10 shrink-0 items-center whitespace-nowrap rounded-full px-4 text-[0.88rem] font-bold transition-colors ${
      active ? "bg-firouzeh-600 text-white" : "bg-[var(--surface)] text-[var(--text)] ring-1 ring-[var(--line)] hover:bg-[var(--bg)]"
    }`;
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-3 px-4 pt-4 md:px-8 md:pt-6">
      <nav aria-label={t("nav_hub_bot")} className="flex gap-2 overflow-x-auto pb-1">
        {groups.map((g) => (
          <Link key={g.key} to={g.tabs[0]!.to} aria-current={g === current ? "true" : undefined} className={pill(g === current)}>
            {t(`settings_nav_${g.key}`)}
          </Link>
        ))}
      </nav>
      {current.tabs.length > 1 && (
        <nav aria-label={t(`settings_nav_${current.key}`)} className="flex gap-1 overflow-x-auto border-b border-[var(--line)]">
          {current.tabs.map((tab) => {
            const active = here?.tab.key === tab.key;
            return (
              <Link
                key={tab.key}
                to={tab.to}
                aria-current={active ? "page" : undefined}
                className={`-mb-px flex min-h-11 shrink-0 items-center whitespace-nowrap border-b-[3px] px-4 text-[0.9rem] font-semibold transition-colors ${
                  active ? "border-firouzeh-600 text-firouzeh-700" : "border-transparent text-[var(--hint)] hover:text-[var(--text)]"
                }`}
              >
                {t(`settings_tab_${tab.key}`)}
              </Link>
            );
          })}
        </nav>
      )}
    </div>
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

function AccountBlock({ onNavigate, tone = "dark" }: { onNavigate?: () => void; tone?: Tone }) {
  const { me, t, logout, locale, setLocale } = useSession();
  const dark = tone === "dark";
  return (
    <div className={`flex flex-col gap-2 border-t pt-4 ${dark ? "border-white/10 text-white" : "border-[var(--line)]"}`}>
      <NavLink
        to="/account"
        onClick={onNavigate}
        className={`flex items-center gap-3 rounded-xl px-3 py-2 ${dark ? "hover:bg-white/10" : "hover:bg-[var(--line)]"}`}
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-firouzeh-100 font-extrabold text-firouzeh-700">
          {(me?.display_name || me?.username || "?").slice(0, 1).toUpperCase()}
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-[0.88rem] font-bold">{me?.display_name || me?.username}</span>
          <span className={`text-[0.75rem] ${dark ? "text-white/60" : "text-[var(--hint)]"}`}>{t(`role_${me?.role ?? "admin"}`)}</span>
        </span>
      </NavLink>
      <div className={`flex gap-2 px-1 ${dark ? "[&_button]:text-white/80 [&_button:hover]:bg-white/10" : ""}`}>
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
      <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col gap-5 overflow-y-auto bg-[#11152A] px-3 py-5 md:flex">
        <p className="flex items-center gap-2 px-3 text-xl font-extrabold tracking-tight text-white">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-firouzeh-600 text-[0.95rem]">
            {t("brand").slice(0, 1)}
          </span>
          {t("brand")}
        </p>
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
        {current?.key === "nav_hub_bot" ? <SettingsNav /> : current && current.tabs.length > 1 && <HubTabs hub={current} />}
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
          <NavList tone="light" onNavigate={() => setMoreOpen(false)} />
          <AccountBlock tone="light" onNavigate={() => setMoreOpen(false)} />
        </div>
      </Sheet>
    </div>
  );
}
