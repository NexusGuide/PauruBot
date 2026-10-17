import { useEffect, useRef, useState, type ButtonHTMLAttributes, type CSSProperties, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { parentForPath, tabForPath } from "./navigation";
import { errorText, useApp } from "./state";

export function Screen({
  title,
  subtitle,
  children,
  action,
}: {
  title?: string;
  subtitle?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  const { t } = useApp();
  const navigate = useNavigate();
  const parent = parentForPath(useLocation().pathname);
  return (
    <main id="app-content" tabIndex={-1} className="app-screen">
      {(title || parent) && (
        <header className="app-heading">
          {parent && <button type="button" className="app-back" aria-label={t("ui_back")} onClick={() => navigate(parent)}>
            <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m14 6-6 6 6 6" /></svg>
          </button>}
          <div className="app-heading-grow">
            {title && <h1>{title}</h1>}
            {subtitle && <p className="app-sub">{subtitle}</p>}
          </div>
          {action && <div className="app-heading-action">{action}</div>}
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
  disabled,
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
      {...rest}
      type={rest.type ?? "button"}
      aria-busy={busy || undefined}
      data-tone={tone}
      className={`app-button app-rip-host flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl px-4 text-[0.95rem] transition-opacity active:opacity-80 disabled:opacity-50 ${tones[tone]} ${className}`}
      disabled={busy || disabled}
    >
      {busy && <Spinner small decorative />}<span>{children}</span>
    </button>
  );
}

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`app-panel rounded-3xl bg-[var(--surface)] p-4 ${className}`}>{children}</section>;
}

/** Bottom action bar: one summary line and the single next step of the flow. */
export function ActionBar({
  summary,
  price,
  action,
  onClick,
  submit,
  busy,
  disabled,
}: {
  summary?: string;
  price?: string;
  action: string;
  onClick?: () => void;
  submit?: boolean;
  busy?: boolean;
  disabled?: boolean;
}) {
  return (
    <div className="app-bar">
      <div className="app-bar-info">
        {summary && <small>{summary}</small>}
        {price && <b className="num">{price}</b>}
      </div>
      <Button tone="primary" type={submit ? "submit" : "button"} onClick={onClick} busy={busy} disabled={disabled}>
        {action}
      </Button>
    </div>
  );
}

/** Chevron that follows the reading direction (points left in RTL, right in LTR). */
export function Chevron() {
  return (
    <svg className="app-chev" aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m15 6-6 6 6 6" /></svg>
  );
}

/** Standard tappable row: leading icon, title, optional hint, optional badge, chevron. */
export function Item({
  icon,
  tone,
  title,
  sub,
  badge,
  to,
  onClick,
  disabled,
  chevron = true,
}: {
  icon: ReactNode;
  tone?: "green" | "orange" | "red" | "gray";
  title: ReactNode;
  sub?: ReactNode;
  badge?: ReactNode;
  to?: string;
  onClick?: () => void;
  disabled?: boolean;
  chevron?: boolean;
}) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (to) navigate(to);
        onClick?.();
      }}
      className="app-row app-rip-host flex w-full items-center gap-3 px-4 text-start"
    >
      <span className="app-ico" data-tone={tone}>{icon}</span>
      <span className="app-grow">
        <b className="block text-[0.95rem] font-bold leading-7">{title}</b>
        {sub && <small className="mt-0.5 block text-[0.8rem] font-medium leading-6 text-[var(--hint)]">{sub}</small>}
      </span>
      {badge}
      {chevron && <Chevron />}
    </button>
  );
}

/** Three-step progress bars for the buy flow. */
export function Steps({ n, note }: { n: number; note: string }) {
  return (
    <>
      <div className="app-steps" aria-hidden="true">
        {[1, 2, 3].map((step) => <i key={step} className={step <= n ? "on" : ""} />)}
      </div>
      <p className="app-steps-note">{note}</p>
    </>
  );
}

/** Numbered trail (amount → transfer → receipt) for card payments. */
export function Trail({ items, active }: { items: string[]; active: number }) {
  const { locale } = useApp();
  return (
    <ol className="app-trail">
      {items.map((label, index) => (
        <li key={label} data-state={index < active ? "done" : index === active ? "now" : "next"}>
          <span className="app-trail-dot">
            {index < active ? (
              <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12l5 5 9-10" /></svg>
            ) : (
              (index + 1).toLocaleString(locale)
            )}
          </span>
          <small>{label}</small>
        </li>
      ))}
    </ol>
  );
}

/** Segmented control with a sliding thumb. */
export function Seg({
  options,
  value,
  onChange,
  disabled,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ left: number; width: number } | null>(null);
  useEffect(() => {
    const element = holder.current;
    if (!element) return;
    const measure = () => {
      const active = element.querySelector<HTMLButtonElement>("button.on");
      if (active) setThumb({ left: active.offsetLeft, width: active.offsetWidth });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [value, options.length]);
  return (
    <div className="app-seg" ref={holder} role="group">
      {thumb && <span className="app-seg-th" style={{ left: thumb.left, width: thumb.width }} aria-hidden="true" />}
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "on" : ""}
          aria-pressed={option.value === value}
          disabled={disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
  busy,
}: {
  checked: boolean;
  onChange: () => void;
  label: string;
  disabled?: boolean;
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="app-sw"
      disabled={disabled || busy}
      onClick={onChange}
    />
  );
}

/** Counts a number up once, then follows real updates. */
export function CountUp({ value, format, className }: { value: number; format: (value: number) => string; className?: string }) {
  const [display, setDisplay] = useState(0);
  const shown = useRef(0);
  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
    if (reduced || !Number.isFinite(value) || value === shown.current) {
      shown.current = value;
      setDisplay(value);
      return;
    }
    const origin = shown.current;
    const delta = value - origin;
    const start = performance.now();
    let frame = requestAnimationFrame(function step(now: number) {
      const progress = Math.min(1, (now - start) / 750);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = Math.round(origin + delta * eased);
      shown.current = current;
      setDisplay(current);
      if (progress < 1) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <span className={className}>{format(display)}</span>;
}

/** Full-screen success overlay with a drawn check. */
export function Overlay({ title, text }: { title: string; text: string }) {
  return (
    <div className="app-ov" role="status" aria-live="polite">
      <div className="app-check">
        <svg viewBox="0 0 120 120" aria-hidden="true"><circle cx="60" cy="60" r="48" /><path d="M38 62l16 16 28-32" /></svg>
      </div>
      <h1>{title}</h1>
      <p>{text}</p>
    </div>
  );
}

export function Spinner({ small, decorative = false }: { small?: boolean; decorative?: boolean }) {
  const { t } = useApp();
  const size = small ? "h-4 w-4" : "h-7 w-7";
  return (
    <span
      role={decorative ? undefined : "status"}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : t("ui_loading")}
      className={`inline-block ${size} animate-spin rounded-full border-2 border-current border-t-transparent opacity-70`}
    />
  );
}

export function Loading() {
  const { t } = useApp();
  return (
    <div className="app-loading" role="status" aria-label={t("ui_loading")}>
      <span className="app-skeleton app-skeleton-title" />
      <span className="app-skeleton app-skeleton-card" />
      <span className="app-skeleton app-skeleton-card" />
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
 * Blue while healthy, saffron when one of them runs low, pomegranate when gone.
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
    v === null ? "var(--accent)" : v <= 0 ? "var(--color-anar-500)" : v < 0.15 ? "var(--color-saffron-500)" : "var(--accent)";
  const dash = (v: number | null, c: number) =>
    `${(v === null ? 1 : Math.max(0, Math.min(1, v))) * c} ${c}`;
  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r1} fill="none" stroke="var(--line)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r2} fill="none" stroke="var(--line)" strokeWidth={stroke * 0.7} />
        <circle
          className="app-ring-arc"
          cx={size / 2}
          cy={size / 2}
          r={r1}
          fill="none"
          stroke={colour(outer)}
          strokeWidth={stroke}
          strokeLinecap="round"
          style={{ strokeDasharray: dash(outer, c1), "--ring-c": c1 } as CSSProperties}
        />
        <circle
          className="app-ring-arc"
          cx={size / 2}
          cy={size / 2}
          r={r2}
          fill="none"
          stroke={colour(inner)}
          strokeOpacity={0.75}
          strokeWidth={stroke * 0.7}
          strokeLinecap="round"
          style={{ strokeDasharray: dash(inner, c2), "--ring-c": c2 } as CSSProperties}
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
  const live = status === "active" || status === "pending";
  return <span className={`app-dot ${live ? "app-dot-live" : ""} inline-block h-2.5 w-2.5 rounded-full ${map[status] ?? "bg-[var(--hint)]"}`} />;
}

const tabs: { to: string; key: string; glyph: string; fab?: boolean }[] = [
  { to: "/", key: "tab_home", glyph: '<path d="M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>' },
  { to: "/services", key: "tab_services", glyph: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>' },
  { to: "/buy", key: "tab_buy", glyph: '<path d="M5 8h14l-1 12H6zM9 8a3 3 0 0 1 6 0"/>', fab: true },
  { to: "/wallet", key: "tab_wallet", glyph: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18M16 15h2"/>' },
  { to: "/more", key: "tab_more", glyph: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/>' },
];

function tapFeedback() {
  const feedback = window.Telegram?.WebApp?.HapticFeedback;
  if (feedback) feedback.selectionChanged();
}

export function TabBar() {
  const { t } = useApp();
  const path = useLocation().pathname;
  const selected = tabForPath(path);
  return (
    <nav aria-label={t("ui_navigation")} className="app-tabbar">
      <ul className="app-tabs">
        {tabs.map((tab) => (
          <li key={tab.to}>
            <Link to={tab.to} onClick={tapFeedback}
              aria-current={selected === tab.key.slice(4) ? (path === tab.to ? "page" : "location") : undefined}
              className={`app-tab ${selected === tab.key.slice(4) ? "app-tab-active" : ""} ${tab.fab ? "app-tab-fab" : ""}`}>
              <span className="app-tab-p">
                <svg width={tab.fab ? 28 : 22} height={tab.fab ? 28 : 22} viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
                  dangerouslySetInnerHTML={{ __html: tab.glyph }} />
              </span>
              <b>{t(tab.key)}</b>
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function Toast() {
  const { toastMessage } = useApp();
  return <div className="app-toast" role={toastMessage?.tone === "error" ? "alert" : "status"}
    aria-live={toastMessage?.tone === "error" ? "assertive" : "polite"} aria-atomic="true">
    {toastMessage && <p data-tone={toastMessage.tone}>{toastMessage.text}</p>}
  </div>;
}

export function Row({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="app-row app-rip-host flex w-full items-center gap-3 px-4 text-start transition-colors active:bg-[var(--line)]"
    >
      {children}
    </button>
  );
}

export function ReadFailure({ error, retry }: { error: unknown; retry: () => void }) {
  const { t } = useApp();
  return <Panel className="my-4"><div role="alert"><p className="mb-3">{errorText(t, error)}</p>
    <Button tone="quiet" onClick={retry}>{t("ui_retry")}</Button></div></Panel>;
}
