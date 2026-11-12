import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Link, useLocation } from "react-router-dom";
import { date, dateTime, money, size } from "@shared/format";
import { call } from "./api";
import { useSession } from "./session";
import {
  confirmationFields, confirmationSpec, needsTyping, validateChallenge,
  type ConfirmationChallenge, type ConfirmationTarget, type ConfirmFields,
} from "./typed-confirm";

// ---------------------------------------------------------------- icons

const ICONS: Record<string, string> = {
  inbox: "M4 13h4l1.5 3h5L16 13h4M4 13l2.5-8h11L20 13v6H4z",
  home: "M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z",
  users: "M16 19v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 17.5V19M10 10.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7M20 19v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 3.65a3.5 3.5 0 0 1 0 6.7",
  services: "M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 11a1 1 0 1 0 1 1",
  orders: "M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6",
  payments: "M3 6h18v12H3zM3 10h18M7 15h3",
  catalog: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  panels: "M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01",
  discounts: "M20 12 12 20l-8-8V4h8zM8.5 8.5h.01",
  broadcast: "M4 10v4h3l5 4V6L7 10zM16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11",
  resellers: "M12 3 4 7v5c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V7z",
  staff: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M5 21a7 7 0 0 1 14 0",
  tickets: "M4 5h16v11H8l-4 4z",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1",
  texts: "M4 6h16M4 12h10M4 18h7",
  channels: "M21 5 3 11l7 2 2 7z",
  audit: "M9 4h6v3H9zM6 6h12v15H6zM9 12h6M9 16h4",
  jobs: "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18",
  reports: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  backup: "M4 7h16v13H4zM3 4h18v3H3zM10 11h4",
  account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M4 21a8 8 0 0 1 16 0",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  close: "M6 6l12 12M18 6 6 18",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14M20 20l-4-4",
  check: "M5 12.5 10 17l9-10",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  plus: "M12 5v14M5 12h14",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
  sort: "M8 6v12M5 9l3-3 3 3M16 18V6M13 15l3 3 3-3",
  up: "M6 15l6-6 6 6",
  down: "M6 9l6 6 6-6",
  copy: "M8 8h11v11H8zM5 16V5h11",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  bot: "M5 9h14v10H5zM12 5v4M9 13h.01M15 13h.01M9.5 16.5h5M3 13v2M21 13v2M12 4h.01",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  variable: "M8 4c-2 0-3 1-3 3v2c0 1.5-1 3-2 3 1 0 2 1.5 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1.5 1 3 2 3-1 0-2 1.5-2 3v2c0 2-1 3-3 3",
  chevron: "M9 6l6 6-6 6",
  sidebar: "M4 5h16v14H4zM9.5 5v14M6.5 9h.01",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5",
  grid: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  wallet: "M3 6h18v13H3zM3 10h18M16 14.5h.01",
  send: "M21 3 10 14M21 3l-7 18-4-7-7-4z",
  alert: "M12 3 2 20h20zM12 10v4M12 17h.01",
  trend: "M3 17l6-6 4 4 8-8M15 7h6v6",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 7v5l3 2",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01",
};

export function Icon({ name, size: px = 20, className = "" }: { name: string; size?: number; className?: string }) {
  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={`shrink-0 ${className}`}
    >
      <path d={ICONS[name] ?? ICONS.more} />
    </svg>
  );
}

// ---------------------------------------------------------------- layout blocks

/** The help article for a screen; the help bundle loads in the background so pages never wait on it. */
function useHelpFor(locale: string, pathname: string): { slug: string; title: string } | undefined {
  const [help, setHelp] = useState<{ slug: string; title: string } | undefined>();
  useEffect(() => {
    let alive = true;
    if (pathname.startsWith("/help")) {
      setHelp(undefined);
      return;
    }
    void import("./help")
      .then(({ articleForPath }) => {
        const found = articleForPath(locale, pathname);
        if (alive) setHelp(found ? { slug: found.slug, title: found.title } : undefined);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [locale, pathname]);
  return help;
}

export function Page({
  title,
  actions,
  children,
  wide,
  icon,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  wide?: boolean;
  /** Shown before the title, e.g. a customer's photo. */
  icon?: ReactNode;
}) {
  const { t, locale } = useSession();
  const { pathname } = useLocation();
  const help = useHelpFor(locale, pathname);
  return (
    <div className={`mx-auto w-full px-4 pb-28 pt-4 md:px-8 md:pb-12 md:pt-7 ${wide ? "max-w-7xl" : "max-w-5xl"}`}>
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3 md:mb-6">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <h1 className="text-[1.35rem] font-extrabold tracking-tight md:text-[1.6rem]">{title}</h1>
          {help && (
            <Link
              to={`/help/${help.slug}`}
              aria-label={t("help_about", { title: help.title })}
              title={help.title}
              className="grid size-9 shrink-0 place-items-center rounded-full text-[var(--hint)] hover:bg-[var(--surface)] hover:text-firouzeh-700"
            >
              <Icon name="help" size={20} />
            </Link>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      <div className="flex flex-col gap-6">{children}</div>
    </div>
  );
}

export function Section({
  title,
  actions,
  children,
  flush,
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="rounded-[20px] bg-[var(--surface)] ring-1 ring-[var(--line)]">
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--line)] px-4 py-3 md:px-5">
          {title && <h2 className="text-[0.98rem] font-bold">{title}</h2>}
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={flush ? "" : "p-4 md:p-5"}>{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------- controls

type ButtonTone = "primary" | "quiet" | "danger" | "ghost" | "approve";

const BUTTON_TONES: Record<ButtonTone, string> = {
  primary: "bg-[var(--accent)] text-[var(--accent-ink)] font-bold hover:brightness-110",
  quiet: "bg-[var(--surface-2)] text-[var(--text)] ring-1 ring-[var(--line)] hover:brightness-125",
  danger: "bg-anar-100 text-anar-700 hover:bg-anar-500 hover:text-white",
  ghost: "text-[var(--hint)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]",
  approve: "bg-pista-500 text-white font-bold hover:brightness-110",
};

export function Button({
  tone = "quiet",
  busy,
  icon,
  small,
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone;
  busy?: boolean;
  icon?: string;
  small?: boolean;
}) {
  const sizing = small ? "min-h-9 px-3 text-[0.8rem] rounded-[10px]" : "min-h-11 px-4 text-[0.88rem] rounded-xl md:min-h-10";
  return (
    <button
      type="button"
      className={`inline-flex items-center justify-center gap-1.5 font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${sizing} ${BUTTON_TONES[tone]} ${className}`}
      {...rest}
      disabled={busy || rest.disabled}
    >
      {busy ? <Spinner small /> : icon ? <Icon name={icon} size={small ? 16 : 18} /> : null}
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: ReactNode;
  children: (id: string) => ReactNode;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <label htmlFor={id} className="text-[0.82rem] font-semibold">
        {label}
      </label>
      {children(id)}
      {hint && <p className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{hint}</p>}
    </div>
  );
}

const CONTROL =
  "rounded-xl bg-[var(--bg)] px-3.5 py-2 text-[0.92rem] ring-1 ring-[var(--line)] outline-none transition-shadow focus:ring-2 focus:ring-firouzeh-500 disabled:opacity-60";

/** Controls fill their container unless the caller gives them a width. */
function width(className: string): string {
  return /(^|\s)(w-|min-w-|max-w-|flex-)/.test(className) ? "" : "w-full";
}

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${CONTROL} ${width(className)} min-h-11 ${className}`} {...rest} />;
}

export function Textarea({ className = "", ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`${CONTROL} ${width(className)} min-h-24 leading-relaxed ${className}`} {...rest} />;
}

export function Select({ className = "", children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`${CONTROL} ${width(className)} min-h-11 ${className}`} {...rest}>
      {children}
    </select>
  );
}

const DIGITS = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩";

/** Accepts Persian/Arabic digits and thousands separators; reports a number or null. */
export function parseNumber(text: string): number | null {
  let normalized = "";
  for (const ch of text) {
    const index = DIGITS.indexOf(ch);
    normalized += index >= 0 ? String(index % 10) : ch;
  }
  normalized = normalized.replace(/[,٬\s]/g, "");
  if (normalized === "" || !/^-?\d+$/.test(normalized)) return null;
  return Number(normalized);
}

export function NumberInput({
  value,
  onChange,
  allowEmpty,
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange"> & {
  value: number | null;
  onChange: (value: number | null) => void;
  allowEmpty?: boolean;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  useEffect(() => {
    // Follow outside changes only; the text the person is typing stays as typed.
    setText((current) => (parseNumber(current) === value ? current : value === null ? "" : String(value)));
  }, [value]);
  return (
    <Input
      inputMode="numeric"
      dir="ltr"
      className="num text-start"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        const parsed = parseNumber(event.target.value);
        if (parsed !== null) onChange(parsed);
        else if (event.target.value.trim() === "" && allowEmpty) onChange(null);
      }}
      {...rest}
    />
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <label htmlFor={id} className="flex flex-col gap-0.5">
        <span className="text-[0.88rem] font-semibold">{label}</span>
        {hint && <span className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{hint}</span>}
      </label>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative mt-0.5 inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
          checked ? "bg-[var(--accent)]" : "bg-[var(--surface-2)] ring-1 ring-[var(--line)]"
        }`}
      >
        <span
          className={`inline-block h-5 w-5 rounded-full shadow transition-transform ${checked ? "bg-[var(--accent-ink)]" : "bg-[var(--hint)]"} ${
            checked ? "translate-x-6 rtl:-translate-x-6" : "translate-x-1 rtl:-translate-x-1"
          }`}
        />
      </button>
    </div>
  );
}

export function Checkbox({
  checked,
  onChange,
  label,
  indeterminate,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  indeterminate?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = Boolean(indeterminate);
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={checked}
      onChange={(event) => onChange(event.target.checked)}
      onClick={(event) => event.stopPropagation()}
      className="h-4 w-4 cursor-pointer accent-firouzeh-600"
    />
  );
}

// ---------------------------------------------------------------- feedback

export function Spinner({ small }: { small?: boolean }) {
  return (
    <span
      role="status"
      aria-label="loading"
      className={`inline-block ${small ? "h-4 w-4" : "h-6 w-6"} animate-spin rounded-full border-2 border-current border-t-transparent opacity-70`}
    />
  );
}

export function Loading() {
  return (
    <div className="flex min-h-40 items-center justify-center text-[var(--hint)]">
      <Spinner />
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <p className="font-bold">{title}</p>
      {hint && <p className="max-w-sm text-[0.85rem] leading-relaxed text-[var(--hint)]">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const { t, errorText } = useSession();
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-10 text-center">
      <p className="font-bold text-anar-700">{errorText(error)}</p>
      {retry && (
        <Button icon="refresh" onClick={retry}>
          {t("retry")}
        </Button>
      )}
    </div>
  );
}

/** Loading, error and data states for one fetch, in one place. */
export function Async<T>({
  data,
  error,
  reload,
  children,
}: {
  data: T | null;
  error: unknown;
  reload: () => void;
  children: (data: T) => ReactNode;
}) {
  if (data !== null) return <>{children(data)}</>;
  if (error) return <ErrorState error={error} retry={reload} />;
  return <Loading />;
}

export function Toasts() {
  const { toasts } = useSession();
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex flex-col items-center gap-2 px-4 md:bottom-6"
      aria-live="polite"
    >
      {toasts.map((item) => (
        <p
          key={item.id}
          className={`rounded-xl px-4 py-2.5 text-[0.88rem] font-semibold shadow-lg ${
            item.tone === "ok" ? "bg-[var(--text)] text-[var(--bg)]" : "bg-anar-500 text-white"
          }`}
        >
          {item.text}
        </p>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- badges and values

type BadgeTone = "neutral" | "brand" | "wait" | "loss" | "health";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-[var(--surface-2)] text-[var(--text)] ring-1 ring-[var(--line)]",
  brand: "bg-firouzeh-100 text-firouzeh-700",
  wait: "bg-saffron-100 text-saffron-700",
  loss: "bg-anar-100 text-anar-700",
  health: "bg-pista-100 text-pista-700",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[0.73rem] font-bold ${BADGE_TONES[tone]}`}>
      {children}
    </span>
  );
}

const STATUS_TONES: Record<string, BadgeTone> = {
  active: "health",
  completed: "health",
  paid: "health",
  approved: "health",
  done: "health",
  open: "wait",
  pending: "wait",
  verifying: "wait",
  queued: "wait",
  running: "brand",
  processing: "brand",
  on_hold: "wait",
  paused: "wait",
  limited: "wait",
  created: "neutral",
  closed: "neutral",
  disabled: "neutral",
  canceled: "neutral",
  expired: "loss",
  failed: "loss",
  rejected: "loss",
  refunded: "loss",
  voided: "loss",
  dead: "loss",
  deleted: "loss",
  answered: "brand",
};

export function Status({ value }: { value: string }) {
  const { t } = useSession();
  return <Badge tone={STATUS_TONES[value] ?? "neutral"}>{t(`status_${value}`)}</Badge>;
}

export function Money({ amount, className = "" }: { amount: number; className?: string }) {
  const { locale, t } = useSession();
  return (
    <span className={`num whitespace-nowrap ${className}`}>
      {money(amount, locale)} <span className="text-[0.8em] text-[var(--hint)]">{t("currency")}</span>
    </span>
  );
}

export function Bytes({ value }: { value: number }) {
  const { locale, t } = useSession();
  return <span className="num whitespace-nowrap">{size(value, locale, t("size_units").split(","))}</span>;
}

export function When({ iso, time }: { iso: string | null; time?: boolean }) {
  const { locale } = useSession();
  return (
    <time className="num whitespace-nowrap" dateTime={iso ?? undefined}>
      {time ? dateTime(iso, locale) : date(iso, locale)}
    </time>
  );
}

export function Usage({ used, limit }: { used: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  const colour = ratio >= 1 ? "bg-anar-500" : ratio >= 0.85 ? "bg-saffron-500" : "bg-[var(--accent)]";
  return (
    <div className="flex min-w-28 flex-col gap-1">
      <span className="text-[0.8rem]">
        <Bytes value={used} />
        {limit > 0 && (
          <>
            <span className="text-[var(--hint)]"> / </span>
            <Bytes value={limit} />
          </>
        )}
      </span>
      {limit > 0 && (
        <span className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
          <span className={`block h-full ${colour}`} style={{ width: `${ratio * 100}%` }} />
        </span>
      )}
    </div>
  );
}

export function CopyText({ text, short }: { text: string; short?: boolean }) {
  const { t, toast } = useSession();
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => toast(t("copied")),
          () => toast(t("copy_failed"), "error"),
        );
      }}
      className="inline-flex max-w-full items-center gap-1 rounded-md px-1 text-start font-mono text-[0.78rem] text-firouzeh-700 hover:bg-firouzeh-50"
      dir="ltr"
      title={t("copy")}
    >
      <span className={short ? "truncate" : "break-all"}>{text}</span>
      <Icon name="copy" size={14} />
    </button>
  );
}

// ---------------------------------------------------------------- sheet and confirm

const openSheets: string[] = [];
let previousScrollLock = false;

export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const { t } = useSession();
  const panel = useRef<HTMLDivElement>(null);
  const sheetId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    if (openSheets.length === 0) previousScrollLock = document.body.classList.contains("overflow-hidden");
    openSheets.push(sheetId);
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (openSheets[openSheets.length - 1] !== sheetId) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
      }
      if (event.key === "Tab" && panel.current) {
        const controls = [...panel.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
        )].filter((el) => el.getClientRects().length > 0);
        const first = controls[0], last = controls[controls.length - 1];
        if (!first) { event.preventDefault(); panel.current.focus(); }
        else if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
          event.preventDefault(); first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("overflow-hidden");
    return () => {
      document.removeEventListener("keydown", onKey);
      const index = openSheets.indexOf(sheetId);
      if (index >= 0) openSheets.splice(index, 1);
      if (!openSheets.length && !previousScrollLock) document.body.classList.remove("overflow-hidden");
      if (previous?.isConnected) previous.focus();
    };
  }, [open, sheetId]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center md:items-stretch md:justify-end">
      <button
        type="button"
        aria-label={t("close")}
        className="absolute inset-0 bg-black/55 backdrop-blur-[2px]"
        onClick={onClose}
        tabIndex={-1}
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={`relative flex max-h-[92dvh] w-full flex-col rounded-t-3xl bg-[var(--surface)] shadow-2xl ring-1 ring-[var(--line)] outline-none md:max-h-none md:rounded-none ${
          wide ? "md:max-w-3xl" : "md:max-w-lg"
        }`}
      >
        <header className="flex items-center justify-between gap-3 border-b border-[var(--line)] px-5 py-4">
          <h2 className="text-lg font-extrabold">{title}</h2>
          <Button tone="ghost" small icon="close" aria-label={t("close")} onClick={onClose} />
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer
            className="flex flex-wrap justify-end gap-2 border-t border-[var(--line)] px-5 py-3"
            style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
          >
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

interface ConfirmRequest {
  title: string;
  body?: string;
  confirm: string;
  danger?: boolean;
  resolve: (ok: boolean) => void;
}

const ConfirmCtx = createContext<(req: Omit<ConfirmRequest, "resolve">) => Promise<boolean>>(async () => false);

interface TypedConfirmRequest {
  title: string;
  body?: string;
  confirm: string;
  target: ConfirmationTarget;
}

type PendingConfirm =
  | { kind: "simple"; owner: string; request: ConfirmRequest }
  | { kind: "typed"; id: number; owner: string; request: TypedConfirmRequest; resolve: (fields: ConfirmFields | null) => void };

const TypedConfirmCtx = createContext<(req: TypedConfirmRequest) => Promise<ConfirmFields | null>>(async () => null);

function cancelConfirmation(pending: PendingConfirm | null) {
  if (pending?.kind === "simple") pending.request.resolve(false);
  else pending?.resolve(null);
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t, me } = useSession();
  const location = useLocation();
  const owner = `${me?.id}:${me?.shop_id}:${location.key}`;
  const ownerRef = useRef(owner);
  ownerRef.current = owner;
  const active = useRef<PendingConfirm | null>(null);
  const nextId = useRef(0);
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const replace = useCallback((next: PendingConfirm) => {
    cancelConfirmation(active.current);
    active.current = next;
    setPending(next);
  }, []);
  const ask = useCallback(
    (req: Omit<ConfirmRequest, "resolve">) =>
      new Promise<boolean>((resolve) => replace({
        kind: "simple", owner: ownerRef.current, request: { ...req, resolve },
      })),
    [replace],
  );
  const askTyped = useCallback(
    (req: TypedConfirmRequest) => new Promise<ConfirmFields | null>((resolve) => {
      // Clone the target before awaiting network/user input.
      const target = JSON.parse(JSON.stringify(req.target)) as ConfirmationTarget;
      replace({ kind: "typed", id: ++nextId.current, owner: ownerRef.current, request: { ...req, target }, resolve });
    }),
    [replace],
  );
  useEffect(() => {
    if (active.current && active.current.owner !== owner) {
      cancelConfirmation(active.current);
      active.current = null;
      setPending(null);
    }
  }, [owner]);
  useEffect(() => () => {
    cancelConfirmation(active.current);
    active.current = null;
  }, []);
  const request = pending?.kind === "simple" ? pending.request : null;
  const finish = (ok: boolean) => {
    if (active.current?.kind !== "simple") return;
    active.current.request.resolve(ok && active.current.owner === ownerRef.current);
    active.current = null;
    setPending(null);
  };
  const finishTyped = (fields: ConfirmFields | null) => {
    if (!pending || pending.kind !== "typed" || active.current !== pending) return;
    pending.resolve(pending.owner === ownerRef.current ? fields : null);
    active.current = null;
    setPending(null);
  };
  return (
    <ConfirmCtx.Provider value={ask}>
      <TypedConfirmCtx.Provider value={askTyped}>
      {children}
      <Sheet
        open={request !== null}
        onClose={() => finish(false)}
        title={request?.title ?? ""}
        footer={
          <>
            <Button onClick={() => finish(false)}>{t("cancel")}</Button>
            <Button tone={request?.danger ? "danger" : "primary"} onClick={() => finish(true)} autoFocus>
              {request?.confirm}
            </Button>
          </>
        }
      >
        {request?.body && <p className="leading-relaxed">{request.body}</p>}
      </Sheet>
      {pending?.kind === "typed" && (
        <TypedConfirmDialog key={pending.id} request={pending.request} finish={finishTyped} />
      )}
      </TypedConfirmCtx.Provider>
    </ConfirmCtx.Provider>
  );
}

export function useConfirm() {
  return useContext(ConfirmCtx);
}

export function useTypedConfirm() {
  return useContext(TypedConfirmCtx);
}

function TypedConfirmDialog({
  request, finish,
}: { request: TypedConfirmRequest; finish: (fields: ConfirmFields | null) => void }) {
  const { t, errorText } = useSession();
  const [challenge, setChallenge] = useState<ConfirmationChallenge | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const [deadline, setDeadline] = useState(0);
  const [now, setNow] = useState(Date.now());
  const settled = useRef(false);
  const formId = useId();
  useEffect(() => {
    const abort = new AbortController();
    let alive = true;
    const issuedAt = Date.now();
    setChallenge(null); setTyped(""); setError(null); setLoading(true);
    void (async () => {
      try {
        const spec = confirmationSpec(request.target);
        const value = await call<unknown>(spec.path, { method: "POST", body: spec.body, signal: abort.signal });
        const result = validateChallenge(value, spec.action);
        if (alive) {
          setChallenge(result);
          setDeadline(issuedAt + result.expires_in * 1000);
          setNow(Date.now());
        }
      } catch (err) {
        if (alive) setError(err);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; abort.abort(); };
  }, [request, generation]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);
  const expired = challenge !== null && now >= deadline;
  const fields = challenge ? confirmationFields(challenge, typed, deadline, now) : null;
  const done = (value: ConfirmFields | null) => {
    if (settled.current) return;
    settled.current = true;
    finish(value);
  };
  const submit = () => {
    const fresh = challenge ? confirmationFields(challenge, typed, deadline) : null;
    if (!loading && fresh) done(fresh);
  };
  return (
    <Sheet open onClose={() => done(null)} title={request.title} footer={
      <>
        <Button onClick={() => done(null)}>{t("cancel")}</Button>
        <Button tone="danger" type="submit" form={formId} disabled={loading || !fields}>
          {request.confirm}
        </Button>
      </>
    }>
      <form id={formId} onSubmit={(event) => { event.preventDefault(); submit(); }} className="flex flex-col gap-4">
        {request.body && <p className="leading-relaxed">{request.body}</p>}
        {loading && <Spinner />}
        {error !== null && <p role="alert" className="text-anar-700">{errorText(error)}</p>}
        {challenge && (
          <>
            <p className="text-[0.85rem]">{t("typed_confirm_server_phrase")}</p>
            <p className="break-all rounded-xl bg-[var(--bg)] p-3 font-mono" dir="auto">{challenge.phrase}</p>
            {challenge.count !== undefined && <p>{t("typed_confirm_server_count", { n: challenge.count })}</p>}
            {needsTyping(challenge) && (
              <Field label={t("typed_confirm_input")} hint={t("typed_confirm_hint")}>
                {(id) => <Input id={id} autoFocus autoComplete="off" spellCheck={false} maxLength={128}
                  value={typed} onChange={(event) => setTyped(event.target.value)} disabled={expired || loading} />}
              </Field>
            )}
            {expired && <p role="alert" className="text-anar-700">{t("typed_confirm_expired")}</p>}
          </>
        )}
        {!loading && (error !== null || expired) && (
          <Button icon="refresh" onClick={() => setGeneration((value) => value + 1)}>{t("typed_confirm_refresh")}</Button>
        )}
      </form>
    </Sheet>
  );
}

// ---------------------------------------------------------------- tables

export interface Column<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  sort?: string;
  className?: string;
  /** "title" leads the mobile row, "meta" shows under it, "hide" is desktop-only. */
  mobile?: "title" | "meta" | "end" | "hide";
}

export function Table<T extends { id: number }>({
  columns,
  rows,
  sort,
  onSort,
  onRow,
  selection,
  empty,
}: {
  columns: Column<T>[];
  rows: T[];
  sort?: string;
  onSort?: (column: string) => void;
  onRow?: (row: T) => void;
  selection?: { selected: Set<number>; toggle: (id: number) => void; setAll: (ids: number[], on: boolean) => void };
  empty?: ReactNode;
}) {
  const { t } = useSession();
  if (rows.length === 0) return <>{empty ?? <Empty title={t("empty_list")} />}</>;
  const ids = rows.map((row) => row.id);
  const allSelected = selection ? ids.every((id) => selection.selected.has(id)) : false;
  const someSelected = selection ? ids.some((id) => selection.selected.has(id)) : false;
  const title = columns.find((c) => c.mobile === "title") ?? columns[0];
  const end = columns.filter((c) => c.mobile === "end");
  const meta = columns.filter((c) => c.mobile === "meta");

  return (
    <>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full border-collapse text-[0.86rem]">
          <thead>
            <tr className="border-b border-[var(--line)] text-start text-[0.76rem] text-[var(--hint)]">
              {selection && (
                <th className="w-10 px-3 py-2.5">
                  <Checkbox
                    label={t("select_all")}
                    checked={allSelected}
                    indeterminate={someSelected && !allSelected}
                    onChange={(on) => selection.setAll(ids, on)}
                  />
                </th>
              )}
              {columns.map((column) => (
                <th key={column.key} className={`px-3 py-2.5 text-start font-semibold ${column.className ?? ""}`}>
                  {column.sort && onSort ? (
                    <button
                      type="button"
                      onClick={() => onSort(column.sort as string)}
                      className="inline-flex items-center gap-1 hover:text-[var(--text)]"
                    >
                      {column.label}
                      <SortMark state={sort === column.sort ? "asc" : sort === `-${column.sort}` ? "desc" : null} />
                    </button>
                  ) : (
                    column.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                onClick={onRow ? () => onRow(row) : undefined}
                className={`border-b border-[var(--line)] last:border-0 ${
                  onRow ? "cursor-pointer hover:bg-[var(--surface-2)]" : ""
                } ${selection?.selected.has(row.id) ? "bg-firouzeh-50/60" : ""}`}
              >
                {selection && (
                  <td className="px-3 py-2.5">
                    <Checkbox
                      label={t("select_row")}
                      checked={selection.selected.has(row.id)}
                      onChange={() => selection.toggle(row.id)}
                    />
                  </td>
                )}
                {columns.map((column) => (
                  <td key={column.key} className={`px-3 py-3 align-middle ${column.className ?? ""}`}>
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="divide-y divide-[var(--line)] md:hidden">
        {rows.map((row) => (
          <li key={row.id} className="flex items-start gap-3 px-4 py-3.5">
            {selection && (
              <span className="pt-1">
                <Checkbox
                  label={t("select_row")}
                  checked={selection.selected.has(row.id)}
                  onChange={() => selection.toggle(row.id)}
                />
              </span>
            )}
            <button
              type="button"
              disabled={!onRow}
              onClick={onRow ? () => onRow(row) : undefined}
              className="flex min-w-0 flex-1 flex-col gap-1 text-start"
            >
              <span className="flex w-full items-start justify-between gap-3">
                <span className="min-w-0 font-semibold">{title?.render(row)}</span>
                {end.length > 0 && (
                  <span className="flex shrink-0 flex-col items-end gap-1">
                    {end.map((column) => (
                      <span key={column.key}>{column.render(row)}</span>
                    ))}
                  </span>
                )}
              </span>
              {meta.length > 0 && (
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.8rem] text-[var(--hint)]">
                  {meta.map((column) => (
                    <span key={column.key}>{column.render(row)}</span>
                  ))}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

function SortMark({ state }: { state: "asc" | "desc" | null }) {
  if (state === "asc") return <Icon name="up" size={14} />;
  if (state === "desc") return <Icon name="down" size={14} />;
  return <Icon name="sort" size={14} className="opacity-40" />;
}

export function Pager({
  page,
  total,
  size: pageSize,
  onPage,
}: {
  page: number;
  total: number;
  size: number;
  onPage: (page: number) => void;
}) {
  const { t, locale } = useSession();
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) {
    return total > 0 ? (
      <p className="px-4 py-3 text-[0.8rem] text-[var(--hint)]">{t("rows_total", { n: money(total, locale) })}</p>
    ) : null;
  }
  return (
    <nav className="flex items-center justify-between gap-3 border-t border-[var(--line)] px-4 py-3 text-[0.82rem]">
      <span className="text-[var(--hint)]">
        {t("page_of", { page: money(page + 1, locale), pages: money(pages, locale), n: money(total, locale) })}
      </span>
      <span className="flex gap-2">
        <Button small disabled={page <= 0} onClick={() => onPage(page - 1)}>
          {t("prev")}
        </Button>
        <Button small disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>
          {t("next")}
        </Button>
      </span>
    </nav>
  );
}

/** A search box that reports after the person pauses typing. */
export function SearchBox({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const timer = window.setTimeout(() => onChange(text), 350);
    return () => window.clearTimeout(timer);
  }, [text, value, onChange]);
  return (
    <div className="relative min-w-48 flex-1">
      <Icon name="search" size={18} className="pointer-events-none absolute start-3 top-1/2 -translate-y-1/2 text-[var(--hint)]" />
      <Input
        type="search"
        value={text}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(event) => setText(event.target.value)}
        className="w-full ps-9"
      />
    </div>
  );
}

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2 border-b border-[var(--line)] p-3">{children}</div>;
}

/** Sticky strip that appears while rows are selected. */
export function BulkBar({ count, children, onClear }: { count: number; children: ReactNode; onClear: () => void }) {
  const { t, locale } = useSession();
  if (count === 0) return null;
  return (
    <div className="sticky top-14 z-10 flex flex-wrap items-center gap-2 border-b border-[var(--line)] bg-firouzeh-50 px-4 py-2.5 text-[0.85rem] md:top-0">
      <span className="font-bold">{t("selected_n", { n: money(count, locale) })}</span>
      <span className="flex flex-1 flex-wrap gap-2">{children}</span>
      <Button small tone="ghost" onClick={onClear}>
        {t("clear_selection")}
      </Button>
    </div>
  );
}

/** Key/value rows for detail views. */
export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 md:gap-x-6">
      {items.map(([label, value]) => (
        <div key={label} className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-[0.78rem] text-[var(--hint)]">{label}</dt>
          <dd className="min-w-0 break-words text-[0.9rem] font-semibold">{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------- redesign building blocks

const AVATAR_TONES = [
  "bg-[#2b3a55] text-[#b7d0ff]",
  "bg-[#16302a] text-[#7fe3b2]",
  "bg-[#2a2344] text-[#cdbbff]",
  "bg-[#3a2a14] text-[#f7ce7a]",
  "bg-[#2e2433] text-[#f0b3d6]",
  "bg-[#1f3138] text-[#8fd8e8]",
];

/** A round initial for a person, its colour fixed by their id so the same person looks the same. */
export function Avatar({ id, name, size = 42 }: { id: number; name: string; size?: number }) {
  const letter = (name.replace(/^@/, "").trim()[0] ?? "?").toUpperCase();
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-full font-extrabold ${AVATAR_TONES[Math.abs(id) % AVATAR_TONES.length]}`}
      style={{ width: size, height: size, fontSize: size * 0.4 }}
    >
      {letter}
    </span>
  );
}

/** One-tap filters in a row that scrolls sideways on a phone. */
export function Chips<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 md:mx-0 md:flex-wrap md:px-0">
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(option.value)}
            className={`h-9 shrink-0 whitespace-nowrap rounded-full px-4 text-[0.82rem] transition-colors ${
              on
                ? "bg-[var(--accent)] font-bold text-[var(--accent-ink)]"
                : "bg-[var(--surface)] font-semibold text-[var(--text)] ring-1 ring-[var(--line)] hover:bg-[var(--surface-2)]"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** A segmented switch between a few views of one page (e.g. the tabs of a customer). */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: ReactNode }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div
      role="tablist"
      aria-label={label}
      className="no-scrollbar flex gap-1 overflow-x-auto rounded-2xl bg-[var(--surface)] p-1 ring-1 ring-[var(--line)]"
    >
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(option.value)}
            className={`flex min-h-10 flex-1 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-xl px-3 text-[0.86rem] transition-colors ${
              on ? "bg-[var(--surface-2)] font-bold text-[var(--text)] ring-1 ring-[var(--line)]" : "font-semibold text-[var(--hint)] hover:text-[var(--text)]"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** A page's opening explanation: two lines on a phone with "more" to read the rest; whole on a desktop. */
export function Intro({ children }: { children: string }) {
  const { t } = useSession();
  const [open, setOpen] = useState(false);
  const long = children.length > 120;
  return (
    <div className="-mt-2 flex flex-col items-start gap-1">
      <p className={`text-[0.86rem] leading-relaxed text-[var(--hint)] ${long && !open ? "line-clamp-2 md:line-clamp-none" : ""}`}>{children}</p>
      {long && (
        <button type="button" onClick={() => setOpen(!open)} className="text-[0.8rem] font-semibold text-firouzeh-700 md:hidden">
          {open ? t("intro_less") : t("intro_more")}
        </button>
      )}
    </div>
  );
}

/** One titled block of a long form, as a card, so a sheet reads in steps. */
export function FormCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-4 rounded-2xl bg-[var(--bg)] p-4 ring-1 ring-[var(--line)]">
      <h3 className="text-[0.92rem] font-bold">{title}</h3>
      {children}
    </section>
  );
}

/** The rarely touched part of a form, folded away under "more settings". */
export function MoreSettings({ children, label }: { children: ReactNode; label?: string }) {
  const { t } = useSession();
  return (
    <details className="group rounded-2xl ring-1 ring-[var(--line)]">
      <summary className="flex min-h-12 cursor-pointer list-none items-center gap-2 px-4 text-[0.9rem] font-bold text-[var(--hint)] hover:text-[var(--text)]">
        <Icon name="settings" size={18} />
        <span className="flex-1">{label ?? t("more_settings")}</span>
        <Icon name="down" size={18} className="transition-transform group-open:rotate-180" />
      </summary>
      <div className="flex flex-col gap-5 border-t border-[var(--line)] p-4">{children}</div>
    </details>
  );
}

/** A small heading inside a folded settings block. */
export function SubHead({ children }: { children: ReactNode }) {
  return <h4 className="-mb-2 text-[0.78rem] font-bold uppercase tracking-wide text-[var(--hint)]">{children}</h4>;
}
