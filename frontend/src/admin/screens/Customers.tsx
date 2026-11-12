import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { money } from "@shared/format";
import { call, type AccountPlan, type CatalogData, type Customer, type CustomerDetail } from "../api";
import { PAGE_SIZE, useAction, useList, useLoad, useSelection } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Avatar,
  Badge,
  BulkBar,
  Button,
  Chips,
  Field,
  Facts,
  Icon,
  Input,
  Money,
  NumberInput,
  Page,
  Pager,
  SearchBox,
  Section,
  Segmented,
  Select,
  Sheet,
  Status,
  Table,
  Textarea,
  Toggle,
  When,
  type Column,
} from "../ui";
import { OwnBotSection } from "./OwnBot";
import { GiftAllButton } from "./Tools";
import { BulkMessageButton, DeleteCustomerButton, NewCustomerButton } from "./ClockTools";
import { CustomerPhoto, QuickRenewButton, TestCustomerToggle } from "./ClockTools2";
import { HiddenPanels } from "./People";
import { ServiceCards } from "./Services";

const RISKS = ["none", "low", "medium", "high"];
const COLORS = ["red", "orange", "yellow", "green", "blue", "purple"];
const COLOR_CLASS: Record<string, string> = {
  red: "bg-anar-500",
  orange: "bg-[#e07b2d]",
  yellow: "bg-saffron-500",
  green: "bg-pista-500",
  blue: "bg-firouzeh-500",
  purple: "bg-[#7c5cc4]",
};

function ColorDot({ color }: { color: string }) {
  const { t } = useSession();
  return <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${COLOR_CLASS[color] ?? "bg-[var(--line)]"}`} title={t(`color_${color}`)} aria-label={t(`color_${color}`)} role="img" />;
}

export function customerName(c: Pick<Customer, "username" | "first_name" | "telegram_id">): string {
  if (c.username) return `@${c.username}`;
  return c.first_name || String(c.telegram_id);
}

/** The name a person goes by: their first name, else their username, else their Telegram id. */
export function displayName(c: Pick<Customer, "username" | "first_name" | "telegram_id">): string {
  return c.first_name?.trim() || (c.username ? `@${c.username}` : String(c.telegram_id));
}

/** Whole days until an end date, never below zero. */
export function daysLeft(iso: string, now = Date.now()): number {
  return Math.max(0, Math.ceil((new Date(iso).getTime() - now) / 86_400_000));
}

type Segment = "all" | "active" | "ending" | "buyers" | "none" | "banned";

function ServicesCell({ c }: { c: Customer }) {
  const { t, locale } = useSession();
  const n = c.active_services ?? 0;
  if (n === 0) return <span className="text-[0.82rem] font-semibold text-[var(--hint)]">{t("cust_no_service")}</span>;
  const left = c.next_expiry ? daysLeft(c.next_expiry) : null;
  const soon = left !== null && left <= 3;
  return (
    <span className="flex flex-col items-end gap-0.5 md:items-start">
      <span className={`text-[0.84rem] font-bold ${soon ? "text-saffron-700" : "text-pista-700"}`}>
        {soon ? t("cust_days_left", { n: money(left, locale) }) : t("cust_services_n", { n: money(n, locale) })}
      </span>
      {soon && n > 1 && <span className="text-[0.74rem] text-[var(--hint)]">{t("cust_services_n", { n: money(n, locale) })}</span>}
    </span>
  );
}

export function Customers() {
  const { t, can, toast, errorText, locale } = useSession();
  const navigate = useNavigate();
  const list = useList<Customer>("/api/admin/customers", ["q", "banned", "tag", "risk", "color", "segment"]);
  const selection = useSelection(`${list.page}:${list.params.toString()}:${list.data?.total ?? 0}`);
  const [busy, run] = useAction();
  const [tagging, setTagging] = useState<"tag" | "untag" | null>(null);
  const [tag, setTag] = useState("");
  const [moreFilters, setMoreFilters] = useState(Boolean(list.get("risk") || list.get("color")));
  const segment: Segment = list.get("banned") === "true" ? "banned" : ((list.get("segment") || "all") as Segment);

  const bulk = async (action: "ban" | "unban" | "tag" | "untag", tagValue?: string) => {
    try {
      const result = await run(() =>
        call<{ updated: number }>("/api/admin/customers/bulk", {
          method: "POST",
          body: { action, ids: [...selection.selected], tag: tagValue },
        }),
      );
      toast(t("bulk_done", { n: result?.updated ?? 0 }));
      selection.clear();
      setTagging(null);
      setTag("");
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const columns: Column<Customer>[] = [
    {
      key: "name",
      label: t("col_customer"),
      mobile: "title",
      render: (c) => (
        <span className="flex min-w-0 items-center gap-3">
          <Avatar id={c.id} name={displayName(c)} />
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="flex min-w-0 flex-wrap items-center gap-1.5">
              {c.color && <ColorDot color={c.color} />}
              <span className="truncate text-[0.92rem] font-bold">{displayName(c)}</span>
              {c.is_banned && <Badge tone="loss">{t("banned")}</Badge>}
              {c.risk !== "none" && <Badge tone={c.risk === "low" ? "wait" : "loss"}>{t(`risk_${c.risk}`)}</Badge>}
              {c.blocked_bot_at && <Badge>{t("blocked_bot")}</Badge>}
              {c.tags.map((tagName) => (
                <Badge key={tagName} tone="brand">
                  {tagName}
                </Badge>
              ))}
            </span>
            <span className="truncate text-start text-[0.78rem] font-normal text-[var(--hint)]" dir="ltr">
              {c.username ? `@${c.username}` : c.telegram_id}
            </span>
          </span>
        </span>
      ),
    },
    { key: "services", label: t("col_services"), mobile: "end", render: (c) => <ServicesCell c={c} /> },
    {
      key: "wallet",
      label: t("col_wallet"),
      mobile: "end",
      render: (c) => (
        <span className="text-[0.8rem] text-[var(--hint)] md:text-[0.86rem] md:text-[var(--text)]">
          <Money amount={c.balance ?? 0} />
        </span>
      ),
    },
    { key: "phone", label: t("col_phone"), mobile: "hide", render: (c) => <span dir="ltr">{c.phone ?? "—"}</span> },
    { key: "joined", label: t("col_joined"), sort: "created_at", mobile: "hide", render: (c) => <When iso={c.created_at} /> },
    {
      key: "seen",
      label: t("col_last_seen"),
      sort: "last_seen_at",
      mobile: "hide",
      render: (c) => <When iso={c.last_seen_at} time />,
    },
  ];

  const segments: { value: Segment; label: string }[] = [
    { value: "all", label: t("seg_all") },
    { value: "active", label: t("seg_active") },
    { value: "ending", label: t("seg_ending") },
    { value: "buyers", label: t("seg_buyers") },
    { value: "none", label: t("seg_none") },
    { value: "banned", label: t("seg_banned") },
  ];
  const pick = (value: Segment) =>
    list.set(
      value === "banned"
        ? { banned: "true", segment: null }
        : { banned: null, segment: value === "all" ? null : value },
    );

  return (
    <Page
      title={t("nav_customers")}
      wide
      actions={
        <>
          <NewCustomerButton />
          <GiftAllButton />
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <SearchBox value={list.get("q")} onChange={(q) => list.set({ q })} placeholder={t("search_customers")} />
          <Button
            icon="sort"
            aria-expanded={moreFilters}
            onClick={() => setMoreFilters((v) => !v)}
            tone={list.get("risk") || list.get("color") ? "primary" : "quiet"}
          >
            {t("filters_more")}
          </Button>
        </div>
        <Chips label={t("filter_status")} options={segments} value={segment} onChange={pick} />
        {moreFilters && (
          <div className="flex flex-wrap gap-2">
            <Select className="w-auto" aria-label={t("filter_risk")} value={list.get("risk")} onChange={(event) => list.set({ risk: event.target.value })}>
              <option value="">{t("filter_any_risk")}</option>
              {RISKS.map((risk) => (
                <option key={risk} value={risk}>
                  {t(`risk_${risk}`)}
                </option>
              ))}
            </Select>
            <Select className="w-auto" aria-label={t("filter_color")} value={list.get("color")} onChange={(event) => list.set({ color: event.target.value })}>
              <option value="">{t("filter_any_color")}</option>
              {COLORS.map((color) => (
                <option key={color} value={color}>
                  {t(`color_${color}`)}
                </option>
              ))}
            </Select>
          </div>
        )}
        {list.get("tag") && (
          <div>
            <Button small tone="quiet" icon="close" onClick={() => list.set({ tag: null })}>
              {t("tag_filter", { tag: list.get("tag") })}
            </Button>
          </div>
        )}
      </div>
      <Section flush>
        {can("customers.ban") && (
          <BulkBar count={selection.selected.size} onClear={selection.clear}>
            <Button small busy={busy} onClick={() => void bulk("ban")}>
              {t("ban")}
            </Button>
            <Button small busy={busy} onClick={() => void bulk("unban")}>
              {t("unban")}
            </Button>
            <BulkMessageButton ids={[...selection.selected]} onDone={selection.clear} />
            {can("customers.write") && (
              <>
                <Button small onClick={() => setTagging("tag")}>
                  {t("add_tag")}
                </Button>
                <Button small onClick={() => setTagging("untag")}>
                  {t("remove_tag")}
                </Button>
              </>
            )}
          </BulkBar>
        )}
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <p className="border-b border-[var(--line)] px-4 py-2.5 text-[0.8rem] text-[var(--hint)] md:px-5">
                {t("cust_count", { n: money(data.total, locale) })}
              </p>
              <Table
                columns={columns}
                rows={data.items}
                sort={list.sort}
                onSort={list.toggleSort}
                onRow={(c) => navigate(`/customers/${c.id}`)}
                selection={can("customers.ban") ? selection : undefined}
              />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      <Sheet
        open={tagging !== null}
        onClose={() => setTagging(null)}
        title={tagging === "untag" ? t("remove_tag") : t("add_tag")}
        footer={
          <Button tone="primary" busy={busy} disabled={!tag.trim()} onClick={() => tagging && void bulk(tagging, tag.trim())}>
            {t("apply_to_n", { n: selection.selected.size })}
          </Button>
        }
      >
        <Field label={t("tag")} hint={t("tag_hint")}>
          {(id) => <Input id={id} value={tag} maxLength={24} onChange={(event) => setTag(event.target.value)} />}
        </Field>
      </Sheet>
    </Page>
  );
}

function AdjustSheet({
  customerId,
  open,
  onClose,
  onDone,
}: {
  customerId: number;
  open: boolean;
  onClose: () => void;
  onDone: (balance: number) => void;
}) {
  const { t, toast, errorText } = useSession();
  const [direction, setDirection] = useState<"add" | "take">("add");
  const [amount, setAmount] = useState<number | null>(null);
  const [memo, setMemo] = useState("");
  const [notify, setNotify] = useState(true);
  const [busy, run] = useAction();
  const [requestId] = useState(() => crypto.randomUUID().replace(/-/g, ""));

  const submit = async () => {
    if (!amount || amount <= 0) return;
    try {
      const result = await run(() =>
        call<{ balance: number }>(`/api/admin/customers/${customerId}/adjust`, {
          method: "POST",
          body: { delta: direction === "add" ? amount : -amount, memo: memo.trim(), notify, request_id: requestId },
        }),
      );
      if (result) onDone(result.balance);
      toast(t("balance_updated"));
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={t("adjust_balance")}
      footer={
        <Button tone="primary" busy={busy} disabled={!amount || memo.trim().length < 2} onClick={() => void submit()}>
          {direction === "add" ? t("add_to_wallet") : t("take_from_wallet")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-2">
          <Button tone={direction === "add" ? "primary" : "quiet"} onClick={() => setDirection("add")}>
            {t("add_to_wallet")}
          </Button>
          <Button tone={direction === "take" ? "danger" : "quiet"} onClick={() => setDirection("take")}>
            {t("take_from_wallet")}
          </Button>
        </div>
        <Field label={t("amount_toman")}>{(id) => <NumberInput id={id} value={amount} onChange={setAmount} />}</Field>
        <Field label={t("memo")} hint={t("memo_hint")}>
          {(id) => <Input id={id} value={memo} maxLength={200} onChange={(event) => setMemo(event.target.value)} />}
        </Field>
        <Toggle checked={notify} onChange={setNotify} label={t("notify_customer")} />
      </div>
    </Sheet>
  );
}

function GiveServiceSheet({ customerId, onClose, onDone }: { customerId: number; onClose: () => void; onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const catalog = useLoad<CatalogData>("/api/admin/catalog");
  const [planId, setPlanId] = useState<number | null>(null);
  const [label, setLabel] = useState("");
  const [busy, run] = useAction();
  const give = async () => {
    try {
      await run(() =>
        call("/api/admin/services", {
          method: "POST",
          body: { customer_id: customerId, plan_id: planId, label: label.trim() || null },
        }),
      );
      toast(t("service_given"));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const plans = (catalog.data?.plans ?? []).filter((plan) => plan.is_active);
  return (
    <Sheet
      open
      onClose={onClose}
      title={t("give_service")}
      footer={
        <Button tone="primary" busy={busy} disabled={!planId} onClick={() => void give()}>
          {t("give_service")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("give_service_hint")}</p>
        <Field label={t("col_plan")}>
          {(id) => (
            <Select id={id} value={planId ?? ""} onChange={(e) => setPlanId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">—</option>
              {plans.map((plan) => (
                <option key={plan.id} value={plan.id}>
                  {plan.title}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("service_label")}>
          {(id) => <Input id={id} value={label} maxLength={48} onChange={(e) => setLabel(e.target.value)} />}
        </Field>
      </div>
    </Sheet>
  );
}

function GiveAccountSheet({ customerId, onClose, onDone }: { customerId: number; onClose: () => void; onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const plans = useLoad<{ items: AccountPlan[] }>("/api/admin/account-plans");
  const [planId, setPlanId] = useState<number | null>(null);
  const [busy, run] = useAction();
  const give = async () => {
    try {
      await run(() => call("/api/admin/panel-accounts", { method: "POST", body: { customer_id: customerId, plan_id: planId } }));
      toast(t("account_given"));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={t("give_account")}
      footer={
        <Button tone="primary" busy={busy} disabled={!planId} onClick={() => void give()}>
          {t("give_account")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("give_account_hint")}</p>
        <Field label={t("col_plan")}>
          {(id) => (
            <Select id={id} value={planId ?? ""} onChange={(e) => setPlanId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">—</option>
              {(plans.data?.items ?? [])
                .filter((plan) => plan.is_active)
                .map((plan) => (
                  <option key={plan.id} value={plan.id}>
                    {plan.title}
                  </option>
                ))}
            </Select>
          )}
        </Field>
      </div>
    </Sheet>
  );
}

function ActionTile({ icon, label, onClick }: { icon: string; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[4.5rem] flex-col items-center justify-center gap-1.5 rounded-2xl bg-[var(--surface)] px-1 text-[0.76rem] font-semibold ring-1 ring-[var(--line)] transition-colors hover:bg-[var(--surface-2)]"
    >
      <Icon name={icon} size={20} className="text-[var(--accent)]" />
      {label}
    </button>
  );
}

export function CustomerPage() {
  const { id } = useParams();
  const { t, can, me, locale, toast, errorText } = useSession();
  const navigate = useNavigate();
  const { data, error, reload, setData } = useLoad<CustomerDetail>(`/api/admin/customers/${id}`);
  const [adjusting, setAdjusting] = useState(false);
  const [messaging, setMessaging] = useState(false);
  const [message, setMessage] = useState("");
  const [banOpen, setBanOpen] = useState(false);
  const [banReason, setBanReason] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [giving, setGiving] = useState(false);
  const [givingAccount, setGivingAccount] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [tab, setTab] = useState<"services" | "orders" | "wallet" | "profile">("services");
  const [busy, run] = useAction();

  const ban = async (banned: boolean) => {
    try {
      const result = await run(() =>
        call<{ customer: Customer }>(`/api/admin/customers/${id}/ban`, {
          method: "POST",
          body: { banned, reason: banned ? banReason.trim() || null : null },
        }),
      );
      if (result) setData((prev) => (prev ? { ...prev, customer: result.customer } : prev));
      setBanOpen(false);
      toast(banned ? t("customer_banned") : t("customer_unbanned"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const send = async () => {
    try {
      await run(() => call(`/api/admin/customers/${id}/message`, { method: "POST", body: { text: message } }));
      setMessaging(false);
      setMessage("");
      toast(t("message_sent"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const trialAgain = async () => {
    try {
      await run(() => call(`/api/admin/customers/${id}/trial-again`, { method: "POST" }));
      toast(t("trial_again_done"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const saveMarkers = async (patch: {
    color?: string | null;
    risk?: string;
    card_trust?: string;
    hidden_panel_ids?: number[];
  }) => {
    try {
      const result = await run(() =>
        call<{ customer: Customer }>(`/api/admin/customers/${id}`, { method: "PATCH", body: patch }),
      );
      if (result) setData((prev) => (prev ? { ...prev, customer: result.customer } : prev));
      toast(t("saved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const saveNote = async (value: string) => {
    try {
      const result = await run(() =>
        call<{ customer: Customer }>(`/api/admin/customers/${id}`, { method: "PATCH", body: { note: value } }),
      );
      if (result) setData((prev) => (prev ? { ...prev, customer: result.customer } : prev));
      setNote(null);
      toast(t("saved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async data={data} error={error} reload={reload}>
      {(detail) => {
        const c = detail.customer;
        return (
          <Page title={t("customer_title")}>
            <section className="flex items-center gap-4">
              <CustomerPhoto customerId={c.id} name={displayName(c)} />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <h2 className="truncate text-[1.25rem] font-extrabold">{displayName(c)}</h2>
                <p className="flex flex-wrap items-center gap-x-2 text-[0.82rem] text-[var(--hint)]">
                  {c.username && <span dir="ltr">@{c.username}</span>}
                  <span className="num" dir="ltr">
                    {c.telegram_id}
                  </span>
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {c.is_banned && <Badge tone="loss">{t("banned")}</Badge>}
                  {c.is_test && <Badge tone="wait">{t("cust_test")}</Badge>}
                  {c.phone && <Badge tone="health">{t("cust_phone_ok")}</Badge>}
                  {detail.reseller && <Badge tone="brand">{t("reseller")}</Badge>}
                  <Badge>
                    {t("cust_member_since")}&nbsp;<When iso={c.created_at} />
                  </Badge>
                </div>
              </div>
            </section>

            <section className="grid grid-cols-3 rounded-[18px] bg-[var(--surface)] ring-1 ring-[var(--line)] [&>div+div]:border-s [&>div+div]:border-[var(--line)]">
              <div className="flex flex-col items-center gap-0.5 px-2 py-3">
                <span className="text-[1rem] font-extrabold">
                  <Money amount={detail.balance} />
                </span>
                <span className="text-[0.75rem] text-[var(--hint)]">{t("wallet_balance")}</span>
              </div>
              <div className="flex flex-col items-center gap-0.5 px-2 py-3">
                <span className="num text-[1rem] font-extrabold">
                  {money(detail.services.filter((svc) => svc.status === "active").length, locale)}
                </span>
                <span className="text-[0.75rem] text-[var(--hint)]">{t("dash_active_services")}</span>
              </div>
              <div className="flex flex-col items-center gap-0.5 px-2 py-3">
                <span className="num text-[1rem] font-extrabold">{money(detail.referrals, locale)}</span>
                <span className="text-[0.75rem] text-[var(--hint)]">{t("referrals")}</span>
              </div>
            </section>

            <section aria-label={t("cust_actions")} className="grid grid-cols-4 gap-2 md:max-w-xl">
              {can("wallet.adjust") && <ActionTile icon="wallet" label={t("cust_wallet")} onClick={() => setAdjusting(true)} />}
              {can("customers.write") && <ActionTile icon="send" label={t("cust_message")} onClick={() => setMessaging(true)} />}
              {can("services.create") && can("catalog.read") && (
                <ActionTile icon="plus" label={t("cust_new_service")} onClick={() => setGiving(true)} />
              )}
              <ActionTile icon="more" label={t("cust_more_actions")} onClick={() => setMoreOpen(true)} />
            </section>

            <Segmented
              label={t("customer_title")}
              value={tab}
              onChange={setTab}
              options={[
                { value: "services", label: t("nav_services") },
                { value: "orders", label: t("nav_orders") },
                { value: "wallet", label: t("cust_wallet") },
                { value: "profile", label: t("cust_profile") },
              ]}
            />

            {tab === "services" && <ServiceCards rows={detail.services} />}

            {tab === "orders" && (
              <Section flush>
                <ul className="divide-y divide-[var(--line)]">
                  {detail.orders.length === 0 && <li className="px-4 py-6 text-center text-[var(--hint)]">{t("empty_list")}</li>}
                  {detail.orders.map((o) => (
                    <li key={o.id} className="flex items-center justify-between gap-3 px-4 py-3 text-[0.88rem] md:px-5">
                      <span className="flex flex-col gap-0.5">
                        <Link className="font-semibold text-[var(--text)] hover:text-firouzeh-700" to={`/orders?id=${o.id}`}>
                          {t(`order_kind_${o.kind}`)}
                        </Link>
                        <span className="text-[0.78rem] text-[var(--hint)]">
                          #{money(o.id, locale)} · <When iso={o.created_at} time />
                        </span>
                      </span>
                      <span className="flex flex-col items-end gap-1">
                        <span className="font-bold">
                          <Money amount={o.amount} />
                        </span>
                        <Status value={o.status} />
                      </span>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {tab === "wallet" && (
              <Section flush>
                <ul className="divide-y divide-[var(--line)]">
                  {detail.ledger.length === 0 && <li className="px-4 py-6 text-center text-[var(--hint)]">{t("empty_list")}</li>}
                  {detail.ledger.map((entry) => (
                    <li key={entry.id} className="flex items-center justify-between gap-3 px-4 py-3 text-[0.88rem] md:px-5">
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="font-semibold">{t(`journal_${entry.kind}`)}</span>
                        <span className="truncate text-[0.78rem] text-[var(--hint)]">
                          {entry.memo ? `${entry.memo} — ` : ""}
                          <When iso={entry.at} time />
                        </span>
                      </span>
                      <span className="flex flex-col items-end">
                        <span className={`num font-bold ${entry.amount < 0 ? "text-[var(--text)]" : "text-pista-700"}`} dir="ltr">
                          {entry.amount > 0 ? "+" : ""}
                          {money(entry.amount, locale)}
                        </span>
                        <span className="num text-[0.75rem] text-[var(--hint)]">{money(entry.balance_after, locale)}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {tab === "profile" && (
              <>
            <Section title={t("cust_info")}>
              <Facts
                items={[
                  [t("wallet_balance"), <Money key="b" amount={detail.balance} />],
                  [t("col_telegram_id"), <span key="tg" className="num" dir="ltr">{c.telegram_id}</span>],
                  [t("col_phone"), c.phone ? <span dir="ltr">{c.phone}</span> : null],
                  [t("col_joined"), <When key="j" iso={c.created_at} time />],
                  [t("col_last_seen"), <When key="s" iso={c.last_seen_at} time />],
                  [t("referrals"), money(detail.referrals, locale)],
                  [t("language"), c.locale],
                  [
                    t("reseller"),
                    detail.reseller ? (detail.reseller.active ? t("reseller_active") : t("reseller_inactive")) : t("no"),
                  ],
                  ...(c.is_banned ? ([[t("ban_reason"), c.ban_reason || "—"]] as [string, string][]) : []),
                ]}
              />
            </Section>
              {can("customers.write") && (
              <Section title={t("cust_markers")}>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label={t("customer_color")} hint={t("customer_color_hint")}>
                    {(fid) => (
                      <Select id={fid} value={c.color ?? ""} onChange={(e) => void saveMarkers({ color: e.target.value || null })}>
                        <option value="">—</option>
                        {COLORS.map((color) => (
                          <option key={color} value={color}>
                            {t(`color_${color}`)}
                          </option>
                        ))}
                      </Select>
                    )}
                  </Field>
                  <Field label={t("customer_risk")} hint={t("customer_risk_hint")}>
                    {(fid) => (
                      <Select id={fid} value={c.risk} onChange={(e) => void saveMarkers({ risk: e.target.value })}>
                        {RISKS.map((risk) => (
                          <option key={risk} value={risk}>
                            {t(`risk_${risk}`)}
                          </option>
                        ))}
                      </Select>
                    )}
                  </Field>
                  <Field label={t("customer_card_trust")} hint={t("customer_card_trust_hint")}>
                    {(fid) => (
                      <Select id={fid} value={c.card_trust ?? "normal"} onChange={(e) => void saveMarkers({ card_trust: e.target.value })}>
                        {["normal", "trusted", "blocked"].map((level) => (
                          <option key={level} value={level}>
                            {t(`card_trust_${level}`)}
                          </option>
                        ))}
                      </Select>
                    )}
                  </Field>
                  <div className="sm:col-span-2">
                    <HiddenPanels
                      value={c.hidden_panel_ids ?? []}
                      hint="customer_hidden_panels_hint"
                      onChange={(ids) => void saveMarkers({ hidden_panel_ids: ids })}
                    />
                  </div>
                </div>
              </Section>
              )}
              <Section title={t("staff_note")}>
                {note === null ? (
                  <button
                    type="button"
                    className="w-full rounded-xl px-1 py-1 text-start text-[0.9rem] leading-relaxed hover:bg-[var(--bg)]"
                    onClick={() => setNote(c.note ?? "")}
                    disabled={!can("customers.write")}
                  >
                    {c.note || <span className="text-[var(--hint)]">{t("staff_note_empty")}</span>}
                  </button>
                ) : (
                  <div className="flex flex-col gap-2">
                    <Textarea value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} />
                    <div className="flex gap-2">
                      <Button small tone="primary" busy={busy} onClick={() => void saveNote(note)}>
                        {t("save")}
                      </Button>
                      <Button small onClick={() => setNote(null)}>
                        {t("cancel")}
                      </Button>
                    </div>
                  </div>
                )}
            </Section>

            {can("resellers.manage") && me?.role !== "reseller" && <OwnBotSection customerId={c.id} />}
              </>
            )}

            <Sheet open={moreOpen} onClose={() => setMoreOpen(false)} title={t("cust_more_actions")}>
              <div className="flex flex-col items-stretch gap-2 [&>button]:w-full [&>button]:justify-start">
                <QuickRenewButton customerId={c.id} onDone={() => void reload()} />
                {can("resellers.manage") && me?.role !== "reseller" && (
                  <Button
                    icon="panels"
                    onClick={() => {
                      setMoreOpen(false);
                      setGivingAccount(true);
                    }}
                  >
                    {t("give_account")}
                  </Button>
                )}
                {can("customers.write") && (
                  <Button icon="refresh" busy={busy} onClick={() => void trialAgain()}>
                    {t("trial_again")}
                  </Button>
                )}
                <TestCustomerToggle customerId={c.id} value={Boolean(c.is_test)} onDone={() => void reload()} />
                {can("customers.ban") &&
                  (c.is_banned ? (
                    <Button busy={busy} onClick={() => void ban(false)}>
                      {t("unban")}
                    </Button>
                  ) : (
                    <Button
                      tone="danger"
                      onClick={() => {
                        setMoreOpen(false);
                        setBanOpen(true);
                      }}
                    >
                      {t("ban")}
                    </Button>
                  ))}
                <DeleteCustomerButton customerId={c.id} onDeleted={() => navigate("/customers")} />
              </div>
            </Sheet>

            {givingAccount && (
              <GiveAccountSheet
                customerId={c.id}
                onClose={() => setGivingAccount(false)}
                onDone={() => setGivingAccount(false)}
              />
            )}
            {giving && (
              <GiveServiceSheet
                customerId={c.id}
                onClose={() => setGiving(false)}
                onDone={() => {
                  setGiving(false);
                  window.setTimeout(() => void reload(), 2500);
                }}
              />
            )}
            <AdjustSheet
              customerId={c.id}
              open={adjusting}
              onClose={() => setAdjusting(false)}
              onDone={(balance) => {
                setData((prev) => (prev ? { ...prev, balance } : prev));
                void reload();
              }}
            />
            <Sheet
              open={messaging}
              onClose={() => setMessaging(false)}
              title={t("send_message")}
              footer={
                <Button tone="primary" busy={busy} disabled={!message.trim()} onClick={() => void send()}>
                  {t("send")}
                </Button>
              }
            >
              <Field label={t("message_text")} hint={t("message_text_hint")}>
                {(fieldId) => (
                  <Textarea id={fieldId} value={message} maxLength={4000} onChange={(event) => setMessage(event.target.value)} />
                )}
              </Field>
            </Sheet>
            <Sheet
              open={banOpen}
              onClose={() => setBanOpen(false)}
              title={t("ban_title")}
              footer={
                <Button tone="danger" busy={busy} onClick={() => void ban(true)}>
                  {t("ban")}
                </Button>
              }
            >
              <Field label={t("ban_reason")} hint={t("ban_reason_hint")}>
                {(fieldId) => (
                  <Input id={fieldId} value={banReason} maxLength={255} onChange={(event) => setBanReason(event.target.value)} />
                )}
              </Field>
            </Sheet>
          </Page>
        );
      }}
    </Async>
  );
}
