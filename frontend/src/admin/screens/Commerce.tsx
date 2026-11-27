import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, call, type Order, type OrderDetails, type Payment } from "../api";
import { PAGE_SIZE, useAction, useList } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Avatar,
  Badge,
  Button,
  Chips,
  Facts,
  Field,
  Money,
  Page,
  Pager,
  Section,
  Select,
  Sheet,
  Status,
  Table,
  Textarea,
  Toggle,
  When,
  useTypedConfirm,
  type Column,
} from "../ui";

// The same statuses as filters, the ones staff look for most first.
const PAYMENT_CHIPS = ["verifying", "paid", "pending", "created", "failed", "expired", "refunded", "disputed", "voided"];
const ORDER_CHIPS = ["completed", "provisioning", "paid", "awaiting_payment", "pending", "failed", "refunded", "canceled"];
const VOID_REASONS = ["fake_receipt", "wrong_amount", "duplicate", "mistake", "other"];
const PROVIDERS = ["card", "zarinpal", "stars", "usdt_trc20", "trx", "ton", "nowpayments", "plisio", "aqayepardakht", "tetra98", "variza", "abangateway", "blupal", "tronado", "cubepay", "atlaspay"];

export function formatOrderSpecs(
  details: OrderDetails | undefined,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (!details) return "";
  const parts: string[] = [];
  if (details.days) parts.push(t("order_spec_days", { n: details.days }));
  if (details.gb) parts.push(t("order_spec_gb", { n: details.gb }));
  if (details.users) parts.push(t("order_spec_users", { n: details.users }));
  return parts.join(" · ");
}

export function Orders() {
  const { t, can, toast, errorText } = useSession();
  const typedConfirm = useTypedConfirm();
  const list = useList<Order>("/api/admin/orders", ["status", "customer_id", "id", "group"]);
  const [open, setOpen] = useState<Order | null>(null);
  const [busy, run] = useAction();

  const refund = async (order: Order) => {
    try {
      const result = await run(async () => {
        const fields = await typedConfirm({
          title: t("refund_title"),
          body: t("refund_body", { id: order.id }),
          confirm: t("refund"),
          target: { action: "order.refund", id: order.id },
        });
        if (fields === null) return null;
        return call<{ order: Order }>(`/api/admin/orders/${order.id}/refund`, { method: "POST", body: fields });
      });
      if (!result) return;
      if (result) {
        const updated = { ...order, ...result.order };
        setOpen(updated);
        list.setData((prev) =>
          prev ? { ...prev, items: prev.items.map((row) => (row.id === order.id ? updated : row)) } : prev,
        );
      }
      toast(t("refunded_toast"));
    } catch (err) {
      toast(err instanceof ApiError && err.status === 428 ? t("err_confirmation_required") : errorText(err), "error");
    }
  };

  const columns: Column<Order>[] = [
    {
      key: "id",
      label: t("col_order"),
      sort: "id",
      mobile: "title",
      render: (o) => (
        <span className="flex min-w-0 items-center gap-3">
          <Avatar id={o.customer_id} name={o.customer} size={38} />
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="truncate font-bold">
              <bdi>{o.customer}</bdi> · {t(`order_kind_${o.kind}`)}
            </span>
            <span className="truncate text-[0.76rem] font-normal text-[var(--hint)]">
              #{o.id} · {o.plan ?? "—"}
              {o.account_username ? ` · ${o.account_username}` : o.service_username ? ` · ${o.service_username}` : ""}
              {formatOrderSpecs(o.details, t) ? ` · ${formatOrderSpecs(o.details, t)}` : ""}
            </span>
          </span>
        </span>
      ),
    },
    { key: "amount", label: t("col_amount"), sort: "amount", mobile: "end", render: (o) => <Money amount={o.amount} /> },
    { key: "status", label: t("col_status"), sort: "status", mobile: "end", render: (o) => <Status value={o.status} /> },
    { key: "source", label: t("col_source"), mobile: "hide", render: (o) => (o.source ? t(`source_${o.source}`) : "—") },
    { key: "created", label: t("col_created"), sort: "created_at", mobile: "hide", render: (o) => <When iso={o.created_at} time /> },
  ];

  return (
    <Page title={t("nav_orders")} wide>
      <div className="flex flex-col gap-3">
        <Chips
          label={t("orders_group_filter")}
          value={list.get("group") || "all"}
          onChange={(group) => list.set({ group: group === "all" ? null : group })}
          options={[
            { value: "all", label: t("seg_all") },
            { value: "services", label: t("orders_group_services") },
            { value: "accounts", label: t("orders_group_accounts") },
          ]}
        />
        <Chips
          label={t("filter_status")}
          value={list.get("status") || "all"}
          onChange={(status) => list.set({ status: status === "all" ? null : status })}
          options={[{ value: "all", label: t("seg_all") }, ...ORDER_CHIPS.map((status) => ({ value: status, label: t(`status_${status}`) }))]}
        />
        {(list.get("customer_id") || list.get("id") || list.get("group")) && (
          <div>
            <Button small icon="close" onClick={() => list.set({ customer_id: null, id: null, group: null })}>
              {t("clear_filters")}
            </Button>
          </div>
        )}
      </div>
      <Section flush>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} sort={list.sort} onSort={list.toggleSort} onRow={setOpen} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      {open && (
        <Sheet open onClose={() => setOpen(null)} title={`${t(`order_kind_${open.kind}`)} #${open.id}`}>
          <div className="flex flex-col gap-5">
            <Facts
              items={[
                [t("col_status"), <Status key="s" value={open.status} />],
                [t("col_customer"), <Link key="c" className="text-firouzeh-700" to={`/customers/${open.customer_id}`}>{open.customer}</Link>],
                [t("col_plan"), open.plan],
                ...(open.panel ? ([[t("col_panel"), open.panel]] as [string, string][]) : []),
                ...(open.account_username
                  ? ([[t("nav_admin_accounts"), <span key="au" className="num font-semibold" dir="ltr">{open.account_username}</span>]] as [string, ReactNode][])
                  : []),
                ...(open.service_username
                  ? ([[t("nav_services"), <span key="su" className="num font-semibold" dir="ltr">{open.service_username}</span>]] as [string, ReactNode][])
                  : []),
                ...(formatOrderSpecs(open.details, t)
                  ? ([[t("order_specs"), formatOrderSpecs(open.details, t)]] as [string, string][])
                  : []),
                [t("price_base"), <Money key="b" amount={open.base_price} />],
                [t("discount_reseller"), <Money key="r" amount={open.reseller_discount} />],
                [t("discount_code"), <Money key="d" amount={open.code_discount} />],
                [t("col_amount"), <Money key="a" amount={open.amount} />],
                [t("col_source"), open.source ? t(`source_${open.source}`) : null],
                [t("col_created"), <When key="cr" iso={open.created_at} time />],
                [t("paid_at"), <When key="p" iso={open.paid_at} time />],
                [t("completed_at"), <When key="co" iso={open.completed_at} time />],
              ]}
            />
            {open.error && (
              <p className="rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] text-anar-700" dir="auto">
                {open.error}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {open.service_id && (
                <Link className="text-[0.88rem] font-semibold text-firouzeh-700" to={`/services?customer_id=${open.customer_id}`}>
                  {t("open_service")}
                </Link>
              )}
              {open.account_username && can("resellers.manage") && (
                <Link
                  className="text-[0.88rem] font-semibold text-firouzeh-700"
                  to={`/admin-accounts?q=${encodeURIComponent(open.account_username)}`}
                >
                  {t("open_admin_account")}
                </Link>
              )}
              {can("payments.refund") && ["completed", "failed"].includes(open.status) && open.amount > 0 && (
                <Button tone="danger" busy={busy} onClick={() => void refund(open)}>
                  {t("refund_to_wallet")}
                </Button>
              )}
            </div>
          </div>
        </Sheet>
      )}
    </Page>
  );
}

export function PaymentSheet({
  payment,
  onClose,
  onChange,
}: {
  payment: Payment;
  onClose: () => void;
  onChange: (payment: Payment) => void;
}) {
  const { t, can, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const reviewable = can("payments.review") && payment.reviewable;
  const typedConfirm = useTypedConfirm();
  const [voiding, setVoiding] = useState(false);
  const [voidReason, setVoidReason] = useState(VOID_REASONS[0]);
  const [voidNote, setVoidNote] = useState("");
  const [blockCard, setBlockCard] = useState(false);
  const voidable = can("payments.refund") && payment.voidable;

  const voidPayment = async () => {
    try {
      const result = await run(async () => {
        const fields = await typedConfirm({
          title: t("void_title", { id: payment.id }),
          body: t("void_confirm_body"),
          confirm: t("void"),
          target: { action: "payment.void", id: payment.id },
        });
        if (fields === null) return null;
        return call<{ payment: Payment; kept: string[] }>(`/api/admin/payments/${payment.id}/void`, {
          method: "POST",
          body: { ...fields, reason: voidReason, note: voidNote.trim(), block_card: blockCard },
        });
      });
      if (!result) return;
      onChange({ ...payment, ...result.payment });
      setVoiding(false);
      toast(result.kept.length ? t("voided_toast_kept") : t("voided_toast"), result.kept.length ? "error" : "ok");
    } catch (err) {
      toast(err instanceof ApiError && err.status === 428 ? t("err_confirmation_required") : errorText(err), "error");
    }
  };

  const approve = async () => {
    try {
      const result = await run(() =>
        call<{ payment: Payment; order_settled: boolean }>(`/api/admin/payments/${payment.id}/approve`, { method: "POST" }),
      );
      if (result) onChange({ ...payment, ...result.payment });
      toast(result?.order_settled ? t("approved_and_delivered") : t("approved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const reject = async () => {
    try {
      const result = await run(() =>
        call<{ payment: Payment }>(`/api/admin/payments/${payment.id}/reject`, {
          method: "POST",
          body: { reason: reason.trim() },
        }),
      );
      if (result) onChange({ ...payment, ...result.payment });
      setRejecting(false);
      toast(t("rejected"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Sheet
      open
      onClose={onClose}
      title={t("payment_n", { id: payment.id })}
      wide
      footer={
        reviewable ? (
          rejecting ? (
            <>
              <Button onClick={() => setRejecting(false)}>{t("cancel")}</Button>
              <Button tone="danger" busy={busy} onClick={() => void reject()}>
                {t("reject")}
              </Button>
            </>
          ) : (
            <>
              <Button tone="danger" onClick={() => setRejecting(true)}>
                {t("reject")}
              </Button>
              <Button tone="approve" icon="check" busy={busy} onClick={() => void approve()}>
                {t("approve")}
              </Button>
            </>
          )
        ) : voidable ? (
          voiding ? (
            <>
              <Button onClick={() => setVoiding(false)}>{t("cancel")}</Button>
              <Button tone="danger" busy={busy} onClick={() => void voidPayment()}>
                {t("void")}
              </Button>
            </>
          ) : (
            <Button tone="danger" onClick={() => setVoiding(true)}>
              {t("void")}
            </Button>
          )
        ) : undefined
      }
    >
      <div className="flex flex-col gap-5">
        {payment.review_amount !== null && (
          <p className="rounded-xl bg-saffron-100 px-3 py-2 text-[0.85rem] font-semibold text-saffron-700">
            {t("payment_amount_differs", { amount: payment.review_amount.toLocaleString() })}
          </p>
        )}
        {payment.duplicate_of_id && (
          <p className="rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] font-semibold text-anar-700">
            {t("duplicate_warning", { id: payment.duplicate_of_id })}
          </p>
        )}
        <Facts
          items={[
            [t("col_status"), <Status key="s" value={payment.status} />],
            [t("col_amount"), <Money key="a" amount={payment.amount} />],
            [t("col_method"), t(`provider_${payment.provider}`)],
            [t("col_purpose"), t(`purpose_${payment.purpose}`)],
            [t("col_customer"), <Link key="c" className="text-firouzeh-700" to={`/customers/${payment.customer_id}`}>{payment.customer}</Link>],
            [t("col_order"), payment.order_id ? <Link key="o" className="text-firouzeh-700" to={`/orders?id=${payment.order_id}`}>#{payment.order_id}</Link> : null],
            [t("provider_ref"), payment.provider_tx_id ? <span key="r" dir="ltr">{payment.provider_tx_id}</span> : null],
            [t("col_created"), <When key="cr" iso={payment.created_at} time />],
            [t("paid_at"), <When key="p" iso={payment.paid_at} time />],
            [t("reviewed_at"), <When key="rv" iso={payment.reviewed_at} time />],
          ]}
        />
        {payment.receipt_text && (
          <div>
            <p className="mb-1 text-[0.78rem] text-[var(--hint)]">{t("receipt_text")}</p>
            <p className="whitespace-pre-wrap rounded-xl bg-[var(--bg)] p-3 text-[0.9rem]" dir="auto">
              {payment.receipt_text}
            </p>
          </div>
        )}
        {payment.has_receipt && (
          <img
            src={`/api/admin/payments/${payment.id}/receipt`}
            alt={t("receipt_alt", { id: payment.id })}
            className="max-h-[32rem] self-start rounded-xl ring-1 ring-[var(--line)]"
          />
        )}
        {(payment.review_note || payment.failure_reason) && (
          <p className="text-[0.85rem] text-[var(--hint)]" dir="auto">
            {payment.review_note || payment.failure_reason}
          </p>
        )}
        {rejecting && (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-1.5">
              {(["reject_preset_1", "reject_preset_2", "reject_preset_3", "reject_preset_4"] as const).map((key) => {
                const label = t(key);
                return (
                  <Button key={key} small tone="ghost" onClick={() => setReason(label)}>
                    {label}
                  </Button>
                );
              })}
            </div>
            <Field label={t("reject_reason")} hint={t("reject_reason_hint")}>
              {(id) => <Textarea id={id} value={reason} maxLength={200} onChange={(event) => setReason(event.target.value)} />}
            </Field>
          </div>
        )}
        {payment.void && (
          <div className="flex flex-col gap-1 rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] text-anar-700">
            <p className="font-semibold">
              {t("void_done_line", { reason: t(`void_reason_${payment.void.reason}`) })} · <When iso={payment.void.at} time />
            </p>
            {payment.void.note && <p dir="auto">{payment.void.note}</p>}
            {payment.void.order_id && <p>{t("void_done_order", { id: payment.void.order_id })}</p>}
            {payment.void.service_id && <p>{t("void_done_service", { id: payment.void.service_id })}</p>}
            {payment.void.kept.length > 0 && (
              <p>{t("void_done_kept", { items: payment.void.kept.map((item) => t(`void_kept_${item}`)).join("، ") })}</p>
            )}
            {payment.void.block_card && <p>{t("void_done_blocked")}</p>}
          </div>
        )}
        {voiding && (
          <div className="flex flex-col gap-3 rounded-xl ring-1 ring-anar-200 p-3">
            <p className="text-[0.85rem] leading-relaxed">{t("void_explain")}</p>
            <Field label={t("void_reason")}>
              {(id) => (
                <Select id={id} value={voidReason} onChange={(event) => setVoidReason(event.target.value)}>
                  {VOID_REASONS.map((value) => (
                    <option key={value} value={value}>
                      {t(`void_reason_${value}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("void_note")}>
              {(id) => <Textarea id={id} value={voidNote} maxLength={200} onChange={(event) => setVoidNote(event.target.value)} />}
            </Field>
            <Toggle checked={blockCard} onChange={setBlockCard} label={t("void_block_card")} hint={t("void_block_card_hint")} />
          </div>
        )}
      </div>
    </Sheet>
  );
}

export function Payments() {
  const { t } = useSession();
  const list = useList<Payment>("/api/admin/payments", ["status", "provider", "customer_id", "id"]);
  const [open, setOpen] = useState<Payment | null>(null);

  const columns: Column<Payment>[] = [
    {
      key: "id",
      label: t("col_payment"),
      sort: "id",
      mobile: "title",
      render: (p) => (
        <span className="flex min-w-0 items-center gap-3">
          <Avatar id={p.customer_id} name={p.customer} size={38} />
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="flex min-w-0 items-center gap-1.5">
              <bdi className="truncate font-bold">{p.customer}</bdi>
              {p.duplicate_of_id && <Badge tone="loss">{t("duplicate")}</Badge>}
            </span>
            <span className="truncate text-[0.76rem] font-normal text-[var(--hint)]">
              #{p.id} · {t(`provider_${p.provider}`)}
            </span>
          </span>
        </span>
      ),
    },
    { key: "purpose", label: t("col_purpose"), mobile: "hide", render: (p) => t(`purpose_${p.purpose}`) },
    { key: "amount", label: t("col_amount"), sort: "amount", mobile: "end", render: (p) => <Money amount={p.amount} /> },
    { key: "status", label: t("col_status"), sort: "status", mobile: "end", render: (p) => <Status value={p.status} /> },
    { key: "created", label: t("col_created"), sort: "created_at", mobile: "meta", render: (p) => <When iso={p.created_at} time /> },
  ];

  return (
    <Page title={t("nav_payments")} wide>
      <div className="flex flex-col gap-3">
        <Chips
          label={t("filter_status")}
          value={list.get("status") || "all"}
          onChange={(status) => list.set({ status: status === "all" ? null : status })}
          options={[{ value: "all", label: t("seg_all") }, ...PAYMENT_CHIPS.map((status) => ({ value: status, label: t(`status_${status}`) }))]}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Select
            className="w-auto"
            aria-label={t("col_method")}
            value={list.get("provider")}
            onChange={(event) => list.set({ provider: event.target.value })}
          >
            <option value="">{t("filter_all_methods")}</option>
            {PROVIDERS.map((provider) => (
              <option key={provider} value={provider}>
                {t(`provider_${provider}`)}
              </option>
            ))}
          </Select>
          {(list.get("customer_id") || list.get("id")) && (
            <Button small icon="close" onClick={() => list.set({ customer_id: null, id: null })}>
              {t("clear_filters")}
            </Button>
          )}
        </div>
      </div>
      <Section flush>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} sort={list.sort} onSort={list.toggleSort} onRow={setOpen} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      {open && (
        <PaymentSheet
          payment={open}
          onClose={() => setOpen(null)}
          onChange={(payment) => {
            setOpen(payment);
            list.setData((prev) =>
              prev ? { ...prev, items: prev.items.map((row) => (row.id === payment.id ? payment : row)) } : prev,
            );
          }}
        />
      )}
    </Page>
  );
}
