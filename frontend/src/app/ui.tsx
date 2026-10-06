import type { ButtonHTMLAttributes, ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useApp } from "./state";

export function Screen({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-md px-4 pb-28 pt-5">
      {title && (
        <header className="mb-5 flex items-center justify-between gap-3">
          <h1 className="text-[1.65rem] font-extrabold leading-tight tracking-tight">{title}</h1>
          {action}
        </header>
      )}
      {children}
    </main>
  );
}

type Tone = "primary" | "quiet" | "danger";

export function Button({
  tone = "primary",
  busy,
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone; busy?: boolean }) {
  const tones: Record<Tone, string> = {
    primary: "bg-[var(--accent)] text-[var(--accent-ink)] font-bold",
    quiet: "bg-[var(--surface)] text-[var(--text)] font-semibold ring-1 ring-[var(--line)]",
    danger: "bg-anar-100 text-anar-700 font-semibold",
  };
  return (
    <button
      type="button"
      className={`flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl px-4 text-[0.95rem] transition-opacity active:opacity-80 disabled:opacity-50 ${tones[tone]} ${className}`}
      disabled={busy || rest.disabled}
      {...rest}
    >
      {busy ? <Spinner small /> : children}
    </button>
  );
}

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-3xl bg-[var(--surface)] p-4 ${className}`}>{children}</section>;
}

export function Spinner({ small }: { small?: boolean }) {
  const size = small ? "h-4 w-4" : "h-7 w-7";
  return (
    <span
      role="status"
      aria-label="loading"
      className={`inline-block ${size} animate-spin rounded-full border-2 border-current border-t-transparent opacity-70`}
    />
  );
}

export function Loading() {
  return (
    <div className="flex min-h-[50dvh] items-center justify-center text-[var(--hint)]">
      <Spinner />
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 py-14 text-center">
      <Ring size={72} outer={0} inner={0} />
      <p className="text-lg font-bold">{title}</p>
      {hint && <p className="max-w-xs text-sm text-[var(--hint)]">{hint}</p>}
      {action && <div className="mt-2 w-full max-w-xs">{action}</div>}
    </div>
  );
}

/**
 * The service gauge: the outer ring is data left, the inner ring is time left.
 * Turquoise while healthy, saffron when one of them runs low, pomegranate when gone.
 */
export function Ring({
  size,
  outer,
  inner,
  children,
}: {
  size: number;
  outer: number | null;
  inner: number | null;
  children?: ReactNode;
}) {
  const stroke = Math.max(5, size * 0.085);
  const r1 = size / 2 - stroke / 2;
  const r2 = r1 - stroke * 1.55;
  const c1 = 2 * Math.PI * r1;
  const c2 = 2 * Math.PI * r2;
  const colour = (v: number | null) =>
    v === null ? "#0e9aa7" : v <= 0 ? "#c8344c" : v < 0.15 ? "#e8a33d" : "#0e9aa7";
  const dash = (v: number | null, c: number) => `${(v === null ? 1 : Math.max(0, Math.min(1, v))) * c} ${c}`;
  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r1} fill="none" stroke="var(--line)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r2} fill="none" stroke="var(--line)" strokeWidth={stroke * 0.7} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r1}
          fill="none"
          stroke={colour(outer)}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={dash(outer, c1)}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r2}
          fill="none"
          stroke={colour(inner)}
          strokeOpacity={0.75}
          strokeWidth={stroke * 0.7}
          strokeLinecap="round"
          strokeDasharray={dash(inner, c2)}
        />
      </svg>
      {children && <div className="absolute inset-0 flex flex-col items-center justify-center">{children}</div>}
    </div>
  );
}

export function StatusDot({ status }: { status: string }) {
  const map: Record<string, string> = {
    active: "bg-pista-500",
    on_hold: "bg-saffron-500",
    limited: "bg-saffron-500",
    expired: "bg-anar-500",
    disabled: "bg-[var(--hint)]",
    pending: "bg-firouzeh-500",
  };
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${map[status] ?? "bg-[var(--hint)]"}`} />;
}

const tabs = [
  { to: "/", key: "tab_home", icon: "M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z" },
  { to: "/buy", key: "tab_buy", icon: "M5 7h14l-1.5 11h-11zM9 7a3 3 0 0 1 6 0" },
  { to: "/services", key: "tab_services", icon: "M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5" },
  { to: "/wallet", key: "tab_wallet", icon: "M3 7h16v12H3zM3 7l12-3v3M15 13h4" },
  { to: "/more", key: "tab_more", icon: "M5 12h.01M12 12h.01M19 12h.01" },
];

export function TabBar() {
  const { t } = useApp();
  return (
    <nav
      className="fixed inset-x-0 bottom-0 z-20 border-t border-[var(--line)] bg-[var(--bg)]/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <ul className="mx-auto flex max-w-md justify-between px-2">
        {tabs.map((tab) => (
          <li key={tab.to} className="flex-1">
            <NavLink
              to={tab.to}
              end={tab.to === "/"}
              className={({ isActive }) =>
                `flex flex-col items-center gap-0.5 py-2 text-[0.7rem] font-semibold ${
                  isActive ? "text-[var(--accent)]" : "text-[var(--hint)]"
                }`
              }
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={tab.icon} />
              </svg>
              {t(tab.key)}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function Toast() {
  const { toastMessage } = useApp();
  if (!toastMessage) return null;
  const tone = toastMessage.tone === "ok" ? "bg-ink text-white" : "bg-anar-500 text-white";
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-24 z-30 flex justify-center px-4" aria-live="polite">
      <p className={`rounded-full px-4 py-2 text-sm font-semibold shadow-lg ${tone}`}>{toastMessage.text}</p>
    </div>
  );
}

export function Row({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-3 px-4 py-3.5 text-start transition-colors active:bg-[var(--line)]"
    >
      {children}
    </button>
  );
}
