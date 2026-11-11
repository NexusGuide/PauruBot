import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { money } from "@shared/format";
import { call, type Dashboard, type InboxItem } from "../api";
import { Columns } from "../chart";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { InboxResetButton } from "./ClockTools";
import { Async, Badge, Button, Empty, Field, Icon, Money, Page, Section, Sheet, Textarea, When } from "../ui";

export function compact(value: number, locale: string): string {
  return new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function Figure({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[0.78rem] text-[var(--hint)]">{label}</span>
      <span className="num text-xl font-extrabold leading-tight">{value}</span>
      {hint && <span className="text-[0.75rem] text-[var(--hint)]">{hint}</span>}
    </div>
  );
}

const TILE_TONES = {
  brand: "bg-firouzeh-100 text-firouzeh-700",
  health: "bg-pista-100 text-pista-700",
  violet: "bg-[#2a2344] text-[#cdbbff]",
  wait: "bg-saffron-100 text-saffron-700",
} as const;

/** One figure as a card: an icon, the number, what it counts; a link when it has a page. */
function Tile({
  icon,
  tone,
  value,
  label,
  to,
}: {
  icon: string;
  tone: keyof typeof TILE_TONES;
  value: ReactNode;
  label: string;
  to?: string;
}) {
  const body = (
    <>
      <span className={`grid h-9 w-9 place-items-center rounded-[11px] ${TILE_TONES[tone]}`}>
        <Icon name={icon} size={18} />
      </span>
      <span className="num mt-1 text-[1.45rem] font-extrabold leading-none">{value}</span>
      <span className="text-[0.78rem] text-[var(--hint)]">{label}</span>
    </>
  );
  const box = "flex flex-col gap-1.5 rounded-[18px] bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]";
  return to ? (
    <Link to={to} className={`${box} transition-colors hover:bg-[var(--surface-2)]`}>
      {body}
    </Link>
  ) : (
    <div className={box}>{body}</div>
  );
}

function ResellerOverview({ data }: { data: Dashboard }) {
  const { t, locale, can } = useSession();
  const n = (value: number | undefined) => money(value ?? 0, locale);
  const r = data.reseller;
  return (
    <div className="flex flex-col gap-4">
      {r?.suspended && (
        <div role="alert" className="rounded-2xl bg-anar-100 px-4 py-3 text-[0.9rem] font-semibold text-anar-700">
          {t("reseller_suspended_banner", { debt: n(r.debt) })}
        </div>
      )}
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        <Tile icon="services" tone="violet" value={n(data.active_services)} label={t("dash_active_services")} to="/services" />
        <Tile icon="orders" tone="brand" value={n(data.orders_month)} label={t("dash_orders_month")} />
        <Tile icon="wallet" tone="wait" value={<Money amount={data.spent_month ?? 0} />} label={t("dash_spent_month")} />
      </div>
      {r && (
        <Section
          title={t("my_reseller_account")}
          actions={
            <div className="flex gap-3">
              {r.billing_mode !== "prepaid" && (
                <Link className="text-[0.85rem] font-semibold text-firouzeh-700" to="/billing">
                  {t("billing_history")}
                </Link>
              )}
              {r.level === 1 && can("resellers.sub") && (
                <Link className="text-[0.85rem] font-semibold text-firouzeh-700" to="/sub-resellers">
                  {t("nav_sub_resellers")}
                </Link>
              )}
            </div>
          }
        >
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Figure label={t("balance")} value={<Money amount={r.balance} />} />
            <Figure label={t("discount")} value={`${n(r.discount_percent)}٪`} />
            <Figure
              label={t("billing_mode")}
              value={<span className="text-base">{t(`billing_mode_${r.billing_mode}`)}</span>}
              hint={r.debt > 0 ? t("debt_hint", { debt: n(r.debt) }) : undefined}
            />
            <Figure
              label={t("reseller_expires")}
              value={<span className="text-base">{r.expires_at ? <When iso={r.expires_at} /> : t("never")}</span>}
              hint={r.level === 1 && r.sub_resellers > 0 ? t("sub_count", { n: n(r.sub_resellers) }) : undefined}
            />
          </div>
        </Section>
      )}
    </div>
  );
}

/** Today's takings with the last seven days as small columns, today's in the accent. */
function TodayCard({ data }: { data: Dashboard }) {
  const { t, locale } = useSession();
  const week = (data.series ?? []).slice(-7);
  const top = Math.max(1, ...week.map((point) => point.revenue));
  const weekday = new Intl.DateTimeFormat(locale === "fa" ? "fa-IR" : "en-GB", { weekday: "narrow" });
  return (
    <section className="flex flex-col gap-4 rounded-[22px] bg-[var(--surface)] p-5 ring-1 ring-[var(--line)]">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-[0.82rem] text-[var(--hint)]">{t("dash_revenue_today")}</span>
          <span className="num text-[1.9rem] font-extrabold leading-tight tracking-tight">
            <Money amount={data.revenue_today ?? 0} />
          </span>
        </div>
        <Link to="/reports" className="rounded-full px-3 py-1.5 text-[0.8rem] font-semibold text-firouzeh-700 hover:bg-firouzeh-50">
          {t("nav_reports")}
        </Link>
      </div>
      {week.length > 0 && (
        <div className="flex h-24 items-end gap-2" aria-hidden="true">
          {week.map((point, index) => {
            const last = index === week.length - 1;
            return (
              <div key={point.day} className="flex flex-1 flex-col items-center gap-1.5">
                <div
                  className={`w-full rounded-[7px] ${last ? "bg-[var(--accent)]" : "bg-[var(--surface-2)]"}`}
                  style={{ height: `${Math.max(6, (point.revenue / top) * 72)}px` }}
                />
                <span className={`text-[0.7rem] ${last ? "font-bold text-[var(--text)]" : "text-[var(--hint)]"}`}>
                  {last ? t("today") : weekday.format(new Date(point.day))}
                </span>
              </div>
            );
          })}
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 border-t border-[var(--line)] pt-3">
        <Figure label={t("dash_revenue_week")} value={<Money amount={data.revenue_week ?? 0} />} />
        <Figure label={t("dash_revenue_month")} value={<Money amount={data.revenue_month ?? 0} />} />
      </div>
    </section>
  );
}

function Tiles({ data }: { data: Dashboard }) {
  const { t, locale } = useSession();
  const n = (value: number | undefined) => money(value ?? 0, locale);
  return (
    <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
      <Tile icon="orders" tone="brand" value={n(data.orders_month)} label={t("dash_orders_month")} to="/orders" />
      <Tile
        icon="users"
        tone="health"
        value={n(data.customers_today)}
        label={t("dash_customers_new", { total: n(data.customers) })}
        to="/customers"
      />
      <Tile icon="services" tone="violet" value={n(data.active_services)} label={t("dash_active_services")} to="/services" />
      <Tile icon="tickets" tone="wait" value={n(data.open_tickets)} label={t("dash_open_tickets")} to="/tickets" />
    </div>
  );
}

function MonthChart({ data }: { data: Dashboard }) {
  const { t, locale } = useSession();
  const series = (data.series ?? []).map((point) => ({
    key: point.day,
    label: new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", {
      day: "numeric",
      month: "short",
    }).format(new Date(point.day)),
    value: point.revenue,
    detail: t("dash_orders_n", { n: money(point.orders, locale) }),
  }));
  if (series.length === 0) return null;
  return (
    <section className="hidden rounded-[22px] bg-[var(--surface)] p-5 ring-1 ring-[var(--line)] md:block">
      <Columns
        title={t("dash_chart_title")}
        data={series}
        format={(value) => `${money(value, locale)} ${t("currency")}`}
        axisFormat={(value) => compact(value, locale)}
      />
    </section>
  );
}

function PanelHealth({ data }: { data: Dashboard }) {
  const { t } = useSession();
  if (!data.panels || data.panels.length === 0) return null;
  return (
    <Section
      title={t("dash_panels")}
      actions={
        <Link className="text-[0.82rem] font-semibold text-firouzeh-700" to="/panels">
          {t("manage")}
        </Link>
      }
    >
      <ul className="flex flex-col gap-1">
        {data.panels.map((panel) => (
          <li key={panel.id}>
            <Link to="/panels" className="flex min-h-11 items-center gap-3 rounded-xl px-2 hover:bg-[var(--surface-2)]">
              <span
                className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                  !panel.active ? "bg-[var(--hint)]" : panel.healthy ? "bg-pista-700" : "bg-anar-500"
                }`}
              />
              <span className="min-w-0 flex-1 truncate text-[0.9rem] font-semibold">{panel.name}</span>
              <Badge tone={!panel.active ? "neutral" : panel.healthy ? "health" : "loss"}>
                {!panel.active ? t("panel_off") : panel.healthy ? t("panel_ok") : t("panel_down")}
              </Badge>
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

const TYPE_LOOK = {
  payment: { icon: "payments", tint: "bg-saffron-100 text-saffron-700" },
  ticket: { icon: "tickets", tint: "bg-firouzeh-100 text-firouzeh-700" },
  order: { icon: "alert", tint: "bg-anar-100 text-anar-700" },
  job: { icon: "alert", tint: "bg-anar-100 text-anar-700" },
  application: { icon: "resellers", tint: "bg-[#2a2344] text-[#cdbbff]" },
} as const;

function InboxRow({
  item,
  active,
  onFocus,
  onApprove,
  onReject,
  onRetry,
  onDismiss,
  busy,
}: {
  item: InboxItem;
  active: boolean;
  onFocus: () => void;
  onApprove: () => void;
  onReject: () => void;
  onRetry: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const { t } = useSession();
  const [showReceipt, setShowReceipt] = useState(false);
  const look = TYPE_LOOK[item.type];

  return (
    <li
      className={`transition-colors ${active ? "bg-[var(--surface-2)]" : ""}`}
      onClick={onFocus}
      onFocus={onFocus}
    >
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center md:px-5">
        <div className="flex min-w-0 flex-1 items-start gap-3">
        <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${look.tint}`}>
          <Icon name={look.icon} size={19} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[0.78rem] font-bold text-[var(--hint)]">{t(`inbox_${item.type}`)}</span>
            <span className="text-[0.75rem] text-[var(--hint)]">
              <When iso={item.at} time />
            </span>
            {item.duplicate_of && <Badge tone="loss">{t("inbox_duplicate", { id: item.duplicate_of })}</Badge>}
          </div>
          <p className="text-[0.92rem] font-semibold">
            {item.type === "payment" && (
              <>
                <Money amount={item.amount ?? 0} /> <bdi className="text-[var(--hint)]">{item.customer}</bdi>
              </>
            )}
            {item.type === "ticket" && item.subject}
            {item.type === "order" && (
              <>
                {t("inbox_order_title", { id: item.id })} <Money amount={item.amount ?? 0} />
              </>
            )}
            {item.type === "job" && <span dir="ltr">{item.kind}</span>}
            {item.type === "application" && t("inbox_application_title", { id: item.id })}
          </p>
          {item.error && <p className="line-clamp-2 text-[0.8rem] text-anar-700" dir="auto">{item.error}</p>}
        </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 ps-[3.25rem] sm:ps-0">
          {item.type === "payment" && (
            <>
              <Button small tone="ghost" onClick={() => setShowReceipt((v) => !v)}>
                {showReceipt ? t("hide_receipt") : t("show_receipt")}
              </Button>
              <Button small tone="danger" busy={busy} onClick={onReject}>
                {t("reject")}
              </Button>
              <Button small tone="approve" icon="check" busy={busy} onClick={onApprove} className="min-w-24">
                {t("approve")}
              </Button>
            </>
          )}
          {item.type === "ticket" && (
            <Link className="text-[0.85rem] font-semibold text-firouzeh-700" to={`/tickets/${item.id}`}>
              {t("open")}
            </Link>
          )}
          {item.type === "order" && (
            <Link className="text-[0.85rem] font-semibold text-firouzeh-700" to={`/orders?id=${item.id}`}>
              {t("open")}
            </Link>
          )}
          {item.type === "application" && (
            <Link className="text-[0.85rem] font-semibold text-firouzeh-700" to="/resellers">
              {t("review")}
            </Link>
          )}
          {item.type === "job" && (
            <Button small busy={busy} icon="refresh" onClick={onRetry}>
              {t("retry")}
            </Button>
          )}
          <Button small tone="ghost" icon="close" onClick={onDismiss} aria-label={t("dismiss")} title={t("dismiss")} />
        </div>
      </div>
      {item.type === "payment" && showReceipt && (
        <div className="px-4 pb-4 md:px-5">
          <img
            src={`/api/admin/payments/${item.id}/receipt`}
            alt={t("receipt_alt", { id: item.id })}
            className="max-h-[28rem] rounded-xl ring-1 ring-[var(--line)]"
            loading="lazy"
          />
        </div>
      )}
    </li>
  );
}

function Inbox() {
  const { t, toast, errorText } = useSession();
  const { data, error, reload, setData } = useLoad<{ items: InboxItem[] }>("/api/admin/inbox");
  const [busy, run] = useAction();
  const [cursor, setCursor] = useState(0);
  const [rejecting, setRejecting] = useState<InboxItem | null>(null);
  const [reason, setReason] = useState("");

  const items = data?.items ?? [];
  const drop = useCallback(
    (item: InboxItem) => {
      setData((prev) =>
        prev ? { items: prev.items.filter((x) => !(x.type === item.type && x.id === item.id)) } : prev,
      );
    },
    [setData],
  );

  const approve = useCallback(
    async (item: InboxItem) => {
      try {
        const result = await run(() =>
          call<{ order_settled: boolean }>(`/api/admin/payments/${item.id}/approve`, { method: "POST" }),
        );
        drop(item);
        toast(result?.order_settled ? t("approved_and_delivered") : t("approved"));
      } catch (err) {
        toast(errorText(err), "error");
      }
    },
    [run, drop, toast, t, errorText],
  );

  const confirmReject = async () => {
    if (!rejecting) return;
    try {
      await run(() =>
        call(`/api/admin/payments/${rejecting.id}/reject`, { method: "POST", body: { reason: reason.trim() } }),
      );
      drop(rejecting);
      toast(t("rejected"));
      setRejecting(null);
      setReason("");
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const retry = async (item: InboxItem) => {
    try {
      await run(() => call(`/api/admin/jobs/${item.id}/retry`, { method: "POST" }));
      drop(item);
      toast(t("job_requeued"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const dismiss = useCallback(
    async (item: InboxItem) => {
      drop(item);
      try {
        await call("/api/admin/inbox/dismiss", { method: "POST", body: { type: item.type, id: item.id } });
      } catch (err) {
        toast(errorText(err), "error");
        void reload();
      }
    },
    [drop, toast, errorText, reload],
  );

  // Keyboard review: J/K move, A approves, R rejects, D dismisses.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, [role=dialog]") || event.metaKey || event.ctrlKey) return;
      const item = items[cursor];
      const key = event.key.toLowerCase();
      if (key === "j") setCursor((c) => Math.min(items.length - 1, c + 1));
      else if (key === "k") setCursor((c) => Math.max(0, c - 1));
      else if (!item) return;
      else if (key === "a" && item.type === "payment") void approve(item);
      else if (key === "r" && item.type === "payment") setRejecting(item);
      else if (key === "d") void dismiss(item);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [items, cursor, approve, dismiss]);

  useEffect(() => {
    if (cursor >= items.length) setCursor(Math.max(0, items.length - 1));
  }, [items.length, cursor]);

  return (
    <Section
      flush
      title={t("inbox_title")}
      actions={
        <>
          <InboxResetButton onDone={() => void reload()} />
          <Button small tone="ghost" icon="refresh" onClick={() => void reload()}>
            {t("refresh")}
          </Button>
        </>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {() =>
          items.length === 0 ? (
            <Empty title={t("inbox_empty")} hint={t("inbox_empty_hint")} />
          ) : (
            <>
              <ul className="divide-y divide-[var(--line)]">
                {items.map((item, index) => (
                  <InboxRow
                    key={`${item.type}:${item.id}`}
                    item={item}
                    active={index === cursor}
                    onFocus={() => setCursor(index)}
                    onApprove={() => void approve(item)}
                    onReject={() => setRejecting(item)}
                    onRetry={() => void retry(item)}
                    onDismiss={() => void dismiss(item)}
                    busy={busy}
                  />
                ))}
              </ul>
              <p className="hidden border-t border-[var(--line)] px-4 py-2 text-[0.75rem] text-[var(--hint)] md:block">
                {t("inbox_keys")}
              </p>
            </>
          )
        }
      </Async>
      <Sheet
        open={rejecting !== null}
        onClose={() => setRejecting(null)}
        title={t("reject_title")}
        footer={
          <>
            <Button onClick={() => setRejecting(null)}>{t("cancel")}</Button>
            <Button tone="danger" busy={busy} onClick={() => void confirmReject()}>
              {t("reject")}
            </Button>
          </>
        }
      >
        <Field label={t("reject_reason")} hint={t("reject_reason_hint")}>
          {(id) => (
            <Textarea id={id} value={reason} maxLength={200} onChange={(event) => setReason(event.target.value)} />
          )}
        </Field>
      </Sheet>
    </Section>
  );
}

export function Home() {
  const { t, can, me, locale } = useSession();
  const { data, error, reload } = useLoad<Dashboard>("/api/admin/dashboard");
  const name = me?.display_name || me?.username || "";
  const today = new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date());
  const inbox = can("payments.read") || can("tickets.read") || can("orders.read");
  return (
    <Page title={t("home_hello", { name })} wide>
      <p className="-mt-4 text-[0.85rem] text-[var(--hint)] md:-mt-5">{today}</p>
      <Async data={data} error={error} reload={reload}>
        {(value) =>
          value.revenue_today === undefined ? (
            <>
              <ResellerOverview data={value} />
              {inbox && <Inbox />}
            </>
          ) : (
            <>
              <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
                <TodayCard data={value} />
                <div className="flex flex-col gap-4">
                  <Tiles data={value} />
                  <PanelHealth data={value} />
                </div>
              </div>
              {inbox && <Inbox />}
              <MonthChart data={value} />
            </>
          )
        }
      </Async>
    </Page>
  );
}
