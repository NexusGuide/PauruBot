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
import { date, dateTime, money, size } from "@shared/format";
import { useSession } from "./session";

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

export function Page({
  title,
  actions,
  children,
  wide,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={`mx-auto w-full px-4 pb-28 pt-5 md:px-8 md:pb-12 md:pt-8 ${wide ? "max-w-7xl" : "max-w-5xl"}`}>
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-extrabold tracking-tight md:text-[1.75rem]">{title}</h1>
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
    <section className="rounded-2xl bg-[var(--surface)] ring-1 ring-[var(--line)]">
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--line)] px-4 py-3">
          {title && <h2 className="text-[0.95rem] font-bold">{title}</h2>}
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={flush ? "" : "p-4"}>{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------- controls

type ButtonTone = "primary" | "quiet" | "danger" | "ghost" | "approve";

const BUTTON_TONES: Record<ButtonTone, string> = {
  primary: "bg-firouzeh-600 text-white hover:bg-firouzeh-700",
  quiet: "bg-[var(--surface)] text-[var(--text)] ring-1 ring-[var(--line)] hover:bg-[var(--bg)]",
  danger: "bg-anar-100 text-anar-700 hover:bg-anar-500 hover:text-white",
  ghost: "text-[var(--hint)] hover:bg-[var(--line)] hover:text-[var(--text)]",
  approve: "bg-pista-500 text-white hover:bg-pista-700",
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
  const sizing = small ? "min-h-8 px-2.5 text-[0.8rem] rounded-lg" : "min-h-10 px-3.5 text-[0.88rem] rounded-xl";
  return (
    <button
      type="button"
      className={`inline-flex items-center justify-center gap-1.5 font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${sizing} ${BUTTON_TONES[tone]} ${className}`}
      disabled={busy || rest.disabled}
      {...rest}
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
  "rounded-xl bg-[var(--bg)] px-3 py-2 text-[0.9rem] ring-1 ring-[var(--line)] outline-none transition-shadow focus:ring-2 focus:ring-firouzeh-500 disabled:opacity-60";

/** Controls fill their container unless the caller gives them a width. */
function width(className: string): string {
  return /(^|\s)(w-|min-w-|max-w-|flex-)/.test(className) ? "" : "w-full";
}

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${CONTROL} ${width(className)} min-h-10 ${className}`} {...rest} />;
}

export function Textarea({ className = "", ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`${CONTROL} ${width(className)} min-h-24 leading-relaxed ${className}`} {...rest} />;
}

export function Select({ className = "", children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`${CONTROL} ${width(className)} min-h-10 ${className}`} {...rest}>
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
        className={`relative mt-0.5 inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
          checked ? "bg-firouzeh-600" : "bg-[var(--line)]"
        }`}
      >
        <span
          className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${
            checked ? "translate-x-[1.375rem] rtl:-translate-x-[1.375rem]" : "translate-x-0.5 rtl:-translate-x-0.5"
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
            item.tone === "ok" ? "bg-ink text-white" : "bg-anar-500 text-white"
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
  neutral: "bg-[var(--line)] text-[var(--text)]",
  brand: "bg-firouzeh-100 text-firouzeh-700",
  wait: "bg-saffron-100 text-saffron-700",
  loss: "bg-anar-100 text-anar-700",
  health: "bg-pista-100 text-pista-700",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[0.75rem] font-bold ${BADGE_TONES[tone]}`}>
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
  limited: "wait",
  created: "neutral",
  closed: "neutral",
  disabled: "neutral",
  canceled: "neutral",
  expired: "loss",
  failed: "loss",
  rejected: "loss",
  refunded: "loss",
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
  const colour = ratio >= 1 ? "bg-anar-500" : ratio >= 0.85 ? "bg-saffron-500" : "bg-firouzeh-500";
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
        <span className="h-1 w-full overflow-hidden rounded-full bg-[var(--line)]">
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
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("overflow-hidden");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("overflow-hidden");
      previous?.focus();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center md:items-stretch md:justify-end">
      <button
        type="button"
        aria-label={t("close")}
        className="absolute inset-0 bg-ink/40"
        onClick={onClose}
        tabIndex={-1}
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={`relative flex max-h-[92dvh] w-full flex-col rounded-t-3xl bg-[var(--surface)] shadow-2xl outline-none md:max-h-none md:rounded-none ${
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

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useSession();
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const ask = useCallback(
    (req: Omit<ConfirmRequest, "resolve">) =>
      new Promise<boolean>((resolve) => setRequest({ ...req, resolve })),
    [],
  );
  const finish = (ok: boolean) => {
    request?.resolve(ok);
    setRequest(null);
  };
  return (
    <ConfirmCtx.Provider value={ask}>
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
    </ConfirmCtx.Provider>
  );
}

export function useConfirm() {
  return useContext(ConfirmCtx);
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
            <tr className="border-b border-[var(--line)] text-start text-[0.78rem] text-[var(--hint)]">
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
                  onRow ? "cursor-pointer hover:bg-[var(--bg)]" : ""
                } ${selection?.selected.has(row.id) ? "bg-firouzeh-50/60 dark:bg-firouzeh-700/10" : ""}`}
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
                  <td key={column.key} className={`px-3 py-2.5 align-middle ${column.className ?? ""}`}>
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
          <li key={row.id} className="flex items-start gap-3 px-4 py-3">
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
    <div className="sticky top-14 z-10 flex flex-wrap items-center gap-2 border-b border-[var(--line)] bg-firouzeh-50 px-4 py-2.5 text-[0.85rem] dark:bg-firouzeh-700/20 md:top-0">
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
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
      {items.map(([label, value]) => (
        <div key={label} className="flex flex-col gap-0.5">
          <dt className="text-[0.78rem] text-[var(--hint)]">{label}</dt>
          <dd className="text-[0.9rem] font-semibold">{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
