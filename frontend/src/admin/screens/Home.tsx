import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { money } from "@shared/format";
import { call, type Dashboard, type InboxItem } from "../api";
import { Columns } from "../chart";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { InboxResetButton } from "./ClockTools";
import { Async, Badge, Button, Empty, Field, Money, Page, Section, Sheet, Textarea, When } from "../ui";

export function compact(value: number, locale: string): string {
  return new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function Figure({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 px-4 py-3 first:ps-0">
      <span className="text-[0.78rem] text-[var(--hint)]">{label}</span>
      <span className="num text-xl font-extrabold leading-tight">{value}</span>
      {hint && <span className="text-[0.75rem] text-[var(--hint)]">{hint}</span>}
    </div>
  );
}

function Overview({ data }: { data: Dashboard }) {
  const { t, locale, can } = useSession();
  const n = (value: number | undefined) => money(value ?? 0, locale);
  if (data.revenue_today === undefined) {
    // A reseller's own figures.
    const r = data.reseller;
    return (
      <div className="flex flex-col gap-4">
        {r?.suspended && (
          <div role="alert" className="rounded-2xl bg-anar-100 px-4 py-3 text-[0.9rem] font-semibold text-anar-700">
            {t("reseller_suspended_banner", { debt: n(r.debt) })}
          </div>
        )}
        <Section>
          <div className="grid grid-cols-2 divide-x divide-[var(--line)] rtl:divide-x-reverse sm:grid-cols-3">
            <Figure label={t("dash_active_services")} value={n(data.active_services)} />
            <Figure label={t("dash_orders_month")} value={n(data.orders_month)} />
            <Figure label={t("dash_spent_month")} value={<Money amount={data.spent_month ?? 0} />} />
          </div>
        </Section>
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
            <div className="grid grid-cols-2 divide-x divide-[var(--line)] rtl:divide-x-reverse sm:grid-cols-4">
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
  const series = (data.series ?? []).map((point) => ({
    key: point.day,
    label: new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", {
      day: "numeric",
      month: "short",
    }).format(new Date(point.day)),
    value: point.revenue,
    detail: t("dash_orders_n", { n: money(point.orders, locale) }),
  }));
  return (
    <Section>
      <div className="grid grid-cols-2 gap-y-2 sm:grid-cols-3 lg:grid-cols-5 lg:divide-x lg:divide-[var(--line)] lg:rtl:divide-x-reverse">
        <Figure label={t("dash_revenue_today")} value={<Money amount={data.revenue_today ?? 0} />} />
        <Figure
          label={t("dash_revenue_month")}
          value={<Money amount={data.revenue_month ?? 0} />}
          hint={t("dash_orders_n", { n: n(data.orders_month) })}
        />
        <Figure label={t("dash_active_services")} value={n(data.active_services)} />
        <Figure
          label={t("dash_customers")}
          value={n(data.customers)}
          hint={t("dash_new_today", { n: n(data.customers_today) })}
        />
        <Figure label={t("dash_revenue_week")} value={<Money amount={data.revenue_week ?? 0} />} />
      </div>
      {series.length > 0 && (
        <div className="mt-4 border-t border-[var(--line)] pt-4">
          <Columns
            title={t("dash_chart_title")}
            data={series}
            format={(value) => `${money(value, locale)} ${t("currency")}`}
            axisFormat={(value) => compact(value, locale)}
          />
        </div>
      )}
    </Section>
  );
}

function PanelHealth({ data }: { data: Dashboard }) {
  const { t } = useSession();
  if (!data.panels || data.panels.length === 0) return null;
  return (
    <Section title={t("dash_panels")} actions={<Link className="text-[0.82rem] font-semibold text-firouzeh-700" to="/panels">{t("manage")}</Link>}>
      <ul className="flex flex-wrap gap-2">
        {data.panels.map((panel) => (
          <li key={panel.id}>
            <Link
              to="/panels"
              className="flex items-center gap-2 rounded-xl px-3 py-2 ring-1 ring-[var(--line)] hover:bg-[var(--bg)]"
            >
              <span
                className={`h-2.5 w-2.5 rounded-full ${
                  !panel.active ? "bg-[var(--hint)]" : panel.healthy ? "bg-pista-500" : "bg-anar-500"
                }`}
              />
              <span className="text-[0.86rem] font-semibold">{panel.name}</span>
              <span className="text-[0.75rem] text-[var(--hint)]">
                {!panel.active ? t("panel_off") : panel.healthy ? t("panel_ok") : t("panel_down")}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

const TYPE_TONE = { payment: "wait", ticket: "brand", order: "loss", job: "loss", application: "brand" } as const;

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
  const edge = {
    payment: "border-s-saffron-500",
    ticket: "border-s-firouzeh-500",
    order: "border-s-anar-500",
    job: "border-s-anar-500",
    application: "border-s-firouzeh-500",
  }[item.type];

  return (
    <li
      className={`border-s-4 ${edge} ${active ? "bg-[var(--bg)]" : ""}`}
      onClick={onFocus}
      onFocus={onFocus}
    >
      <div className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={TYPE_TONE[item.type]}>{t(`inbox_${item.type}`)}</Badge>
            <span className="text-[0.75rem] text-[var(--hint)]">
              <When iso={item.at} time />
            </span>
            {item.duplicate_of && <Badge tone="loss">{t("inbox_duplicate", { id: item.duplicate_of })}</Badge>}
          </div>
          <p className="text-[0.92rem] font-semibold">
            {item.type === "payment" && (
              <>
                <Money amount={item.amount ?? 0} /> <span className="text-[var(--hint)]">{item.customer}</span>
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
        <div className="flex flex-wrap items-center gap-2">
          {item.type === "payment" && (
            <>
              <Button small tone="ghost" onClick={() => setShowReceipt((v) => !v)}>
                {showReceipt ? t("hide_receipt") : t("show_receipt")}
              </Button>
              <Button small tone="danger" busy={busy} onClick={onReject}>
                {t("reject")}
              </Button>
              <Button small tone="approve" icon="check" busy={busy} onClick={onApprove}>
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
          <Button small tone="ghost" onClick={onDismiss} aria-label={t("dismiss")}>
            {t("dismiss")}
          </Button>
        </div>
      </div>
      {item.type === "payment" && showReceipt && (
        <div className="px-4 pb-4">
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
  const { t, can } = useSession();
  const { data, error, reload } = useLoad<Dashboard>("/api/admin/dashboard");
  return (
    <Page title={t("nav_inbox")} wide>
      {can("payments.read") || can("tickets.read") || can("orders.read") ? <Inbox /> : null}
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <Overview data={value} />
            <PanelHealth data={value} />
          </>
        )}
      </Async>
    </Page>
  );
}
