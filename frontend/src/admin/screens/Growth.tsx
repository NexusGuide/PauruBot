import { useState } from "react";
import { money } from "@shared/format";
import { call, type CatalogData, type Discount } from "../api";
import { PAGE_SIZE, useAction, useList, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Button,
  Checkbox,
  Field,
  Input,
  Money,
  NumberInput,
  Page,
  Pager,
  Section,
  Select,
  Sheet,
  Table,
  Toggle,
  When,
  useConfirm,
  type Column,
} from "../ui";

type CodeDraft = Omit<Discount, "id" | "used_count" | "created_at"> & { id?: number };

const EMPTY_CODE: CodeDraft = {
  code: "",
  kind: "percent",
  value: 10,
  max_discount: null,
  min_amount: 0,
  max_uses: null,
  per_customer_limit: 1,
  plan_ids: [],
  first_purchase_only: false,
  sticky: false,
  customer_telegram_id: null,
  starts_at: null,
  expires_at: null,
  is_active: true,
  note: null,
};

/** datetime-local works in the browser's zone; the API speaks ISO-8601 with offset. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

function CodeSheet({ draft, onClose, onSaved }: { draft: CodeDraft; onClose: () => void; onSaved: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const [form, setForm] = useState<CodeDraft>(draft);
  const [busy, run] = useAction();
  const catalog = useLoad<CatalogData>(can("catalog.read") ? "/api/admin/catalog" : null);
  const set = <K extends keyof CodeDraft>(key: K, value: CodeDraft[K]) => setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() => call(id ? `/api/admin/discounts/${id}` : "/api/admin/discounts", { method: id ? "PUT" : "POST", body }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const valid = /^[A-Za-z0-9_-]{2,32}$/.test(form.code) && form.value > 0 && (form.kind !== "percent" || form.value <= 100);

  return (
    <Sheet
      open
      onClose={onClose}
      title={form.id ? t("edit_code") : t("new_code")}
      footer={
        <Button tone="primary" busy={busy} disabled={!valid} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("code")} hint={t("code_hint")}>
          {(id) => (
            <Input id={id} dir="ltr" value={form.code} maxLength={32} onChange={(e) => set("code", e.target.value.replace(/\s/g, ""))} />
          )}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("discount_kind")}>
            {(id) => (
              <Select id={id} value={form.kind} onChange={(e) => set("kind", e.target.value)}>
                <option value="percent">{t("discount_percent")}</option>
                <option value="fixed">{t("discount_fixed")}</option>
              </Select>
            )}
          </Field>
          <Field label={form.kind === "percent" ? t("percent") : t("amount_toman")}>
            {(id) => <NumberInput id={id} value={form.value} onChange={(v) => set("value", v ?? 0)} />}
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          {form.kind === "percent" && (
            <Field label={t("max_discount")} hint={t("empty_no_limit")}>
              {(id) => <NumberInput id={id} value={form.max_discount} onChange={(v) => set("max_discount", v || null)} allowEmpty />}
            </Field>
          )}
          <Field label={t("min_amount")}>
            {(id) => <NumberInput id={id} value={form.min_amount} onChange={(v) => set("min_amount", v ?? 0)} />}
          </Field>
          <Field label={t("max_uses")} hint={t("empty_no_limit")}>
            {(id) => <NumberInput id={id} value={form.max_uses} onChange={(v) => set("max_uses", v || null)} allowEmpty />}
          </Field>
          <Field label={t("per_customer_limit")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.per_customer_limit} onChange={(v) => set("per_customer_limit", v ?? 0)} />}
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("starts_at")}>
            {(id) => (
              <Input id={id} type="datetime-local" value={toLocalInput(form.starts_at)} onChange={(e) => set("starts_at", fromLocalInput(e.target.value))} />
            )}
          </Field>
          <Field label={t("expires_at")}>
            {(id) => (
              <Input id={id} type="datetime-local" value={toLocalInput(form.expires_at)} onChange={(e) => set("expires_at", fromLocalInput(e.target.value))} />
            )}
          </Field>
        </div>
        {catalog.data && catalog.data.plans.length > 0 && (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-[0.82rem] font-semibold">{t("code_plans")}</legend>
            <p className="text-[0.78rem] text-[var(--hint)]">{t("code_plans_hint")}</p>
            {catalog.data.plans.map((plan) => (
              <label key={plan.id} className="flex items-center gap-2 text-[0.88rem]">
                <Checkbox
                  label={plan.title}
                  checked={form.plan_ids.includes(plan.id)}
                  onChange={(on) =>
                    set("plan_ids", on ? [...form.plan_ids, plan.id] : form.plan_ids.filter((pid) => pid !== plan.id))
                  }
                />
                {plan.title}
              </label>
            ))}
          </fieldset>
        )}
        <Field label={t("code_customer")} hint={t("code_customer_hint")}>
          {(id) => <NumberInput id={id} allowEmpty value={form.customer_telegram_id} onChange={(v) => set("customer_telegram_id", v)} />}
        </Field>
        <Toggle checked={form.first_purchase_only} onChange={(v) => set("first_purchase_only", v)} label={t("first_purchase_only")} />
        <Toggle checked={form.sticky} onChange={(v) => set("sticky", v)} label={t("sticky_code")} hint={t("sticky_code_hint")} />
        <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("active")} />
        <Field label={t("staff_note")}>
          {(id) => <Input id={id} value={form.note ?? ""} maxLength={255} onChange={(e) => set("note", e.target.value || null)} />}
        </Field>
      </div>
    </Sheet>
  );
}

interface Gift {
  id: number;
  code: string;
  amount: number;
  max_uses: number | null;
  used_count: number;
  new_customers_only: boolean;
  expires_at: string | null;
  is_active: boolean;
  note: string | null;
  created_at: string;
}

type GiftDraft = Omit<Gift, "id" | "used_count" | "created_at"> & { id?: number };

function randomCode(): string {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(10));
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

function GiftSheet({ draft, onClose, onSaved }: { draft: GiftDraft; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState<GiftDraft>(draft);
  const [busy, run] = useAction();
  const redemptions = useLoad<{ items: { id: number; customer_id: number; customer: string; amount: number; at: string }[] }>(
    draft.id ? `/api/admin/gift-codes/${draft.id}/redemptions` : null,
  );
  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() => call(id ? `/api/admin/gift-codes/${id}` : "/api/admin/gift-codes", { method: id ? "PUT" : "POST", body }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={form.id ? t("edit_gift") : t("new_gift")}
      footer={
        <Button tone="primary" busy={busy} disabled={!/^[A-Za-z0-9_-]{4,32}$/.test(form.code) || form.amount < 1000} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("code")} hint={t("gift_code_hint")}>
          {(id) => (
            <div className="flex gap-2">
              <Input id={id} dir="ltr" value={form.code} maxLength={32} onChange={(e) => setForm({ ...form, code: e.target.value.replace(/\s/g, "") })} />
              {!form.id && (
                <Button small onClick={() => setForm({ ...form, code: randomCode() })}>
                  {t("generate")}
                </Button>
              )}
            </div>
          )}
        </Field>
        <Field label={t("amount_toman")}>{(id) => <NumberInput id={id} value={form.amount} onChange={(v) => setForm({ ...form, amount: v ?? 0 })} />}</Field>
        <Field label={t("max_uses")} hint={t("empty_no_limit")}>
          {(id) => <NumberInput id={id} value={form.max_uses} onChange={(v) => setForm({ ...form, max_uses: v || null })} allowEmpty />}
        </Field>
        <Field label={t("expires_at")}>
          {(id) => (
            <Input id={id} type="datetime-local" value={toLocalInput(form.expires_at)} onChange={(e) => setForm({ ...form, expires_at: fromLocalInput(e.target.value) })} />
          )}
        </Field>
        <Toggle checked={form.new_customers_only} onChange={(v) => setForm({ ...form, new_customers_only: v })} label={t("gift_new_only")} />
        <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} />
        <Field label={t("staff_note")}>
          {(id) => <Input id={id} value={form.note ?? ""} maxLength={255} onChange={(e) => setForm({ ...form, note: e.target.value || null })} />}
        </Field>
        {redemptions.data && redemptions.data.items.length > 0 && (
          <div>
            <p className="mb-2 text-[0.82rem] font-semibold">{t("gift_redemptions")}</p>
            <ul className="divide-y divide-[var(--line)] text-[0.85rem]">
              {redemptions.data.items.map((r) => (
                <li key={r.id} className="flex justify-between py-2">
                  <span>{r.customer}</span>
                  <When iso={r.at} time />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Sheet>
  );
}

function GiftCodes() {
  const { t, can, locale } = useSession();
  const list = useLoad<{ items: Gift[]; total: number }>("/api/admin/gift-codes?limit=200");
  const [editing, setEditing] = useState<GiftDraft | null>(null);
  const columns: Column<Gift>[] = [
    {
      key: "code",
      label: t("code"),
      mobile: "title",
      render: (g) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono font-bold" dir="ltr">{g.code}</span>
          {!g.is_active && <Badge>{t("inactive")}</Badge>}
        </span>
      ),
    },
    { key: "amount", label: t("col_amount"), mobile: "end", render: (g) => <Money amount={g.amount} /> },
    {
      key: "uses",
      label: t("uses"),
      mobile: "meta",
      render: (g) => <span className="num">{money(g.used_count, locale)}{g.max_uses ? ` / ${money(g.max_uses, locale)}` : ""}</span>,
    },
    { key: "expires", label: t("expires_at"), mobile: "meta", render: (g) => (g.expires_at ? <When iso={g.expires_at} time /> : t("never")) },
  ];
  return (
    <Section
      flush
      title={t("gift_codes")}
      actions={
        can("wallet.adjust") && (
          <Button small icon="plus" onClick={() => setEditing({ code: randomCode(), amount: 50_000, max_uses: 1, new_customers_only: false, expires_at: null, is_active: true, note: null })}>
            {t("new_gift")}
          </Button>
        )
      }
    >
      <Async data={list.data} error={list.error} reload={list.reload}>
        {(data) => <Table columns={columns} rows={data.items} onRow={can("wallet.adjust") ? (g) => setEditing({ ...g }) : undefined} />}
      </Async>
      {editing && (
        <GiftSheet
          draft={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void list.reload();
          }}
        />
      )}
    </Section>
  );
}

export function Discounts() {
  const { t, locale } = useSession();
  const list = useList<Discount>("/api/admin/discounts", []);
  const [editing, setEditing] = useState<CodeDraft | null>(null);

  const columns: Column<Discount>[] = [
    {
      key: "code",
      label: t("code"),
      mobile: "title",
      render: (c) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono font-bold" dir="ltr">
            {c.code}
          </span>
          {!c.is_active && <Badge>{t("inactive")}</Badge>}
          {c.sticky && <Badge tone="brand">{t("sticky")}</Badge>}
        </span>
      ),
    },
    {
      key: "value",
      label: t("discount"),
      mobile: "end",
      render: (c) => (c.kind === "percent" ? `${money(c.value, locale)}٪` : <Money amount={c.value} />),
    },
    {
      key: "uses",
      label: t("uses"),
      mobile: "meta",
      render: (c) => (
        <span className="num">
          {money(c.used_count, locale)}
          {c.max_uses ? ` / ${money(c.max_uses, locale)}` : ""}
        </span>
      ),
    },
    { key: "expires", label: t("expires_at"), mobile: "meta", render: (c) => (c.expires_at ? <When iso={c.expires_at} time /> : t("never")) },
  ];

  return (
    <Page
      title={t("nav_discounts")}
      actions={
        <Button tone="primary" icon="plus" onClick={() => setEditing({ ...EMPTY_CODE })}>
          {t("new_code")}
        </Button>
      }
    >
      <Section flush>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} onRow={(c) => setEditing({ ...c })} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      <GiftCodes />
      {editing && (
        <CodeSheet
          draft={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void list.reload();
          }}
        />
      )}
    </Page>
  );
}

export { Broadcasts } from "../broadcasts/Broadcasts";

// ---------------------------------------------------------------- lotteries (GRW-04)

type PrizeKind = "service" | "wallet" | "text";

interface LotteryPrize {
  kind: PrizeKind;
  value: number;
  days?: number;
  panel_id?: number;
  label: string;
}

interface PrizePanel {
  id: number;
  name: string;
}

interface LotteryAward extends LotteryPrize {
  rank: number;
  customer_id: number;
  points: number;
  purchases: number;
  spent: number;
  status: "paid" | "queued" | "applied" | "failed" | "manual";
  service?: string;
  name: string;
  username: string | null;
  telegram_id: number | null;
  phone: string | null;
}

interface LotteryRow {
  id: number;
  title: string;
  prize: string;
  prize_amount: number;
  winners: number;
  mode: "join" | "purchases" | "points";
  pick: "random" | "top";
  points_per: number;
  board: "none" | "self" | "top";
  prizes: LotteryPrize[];
  starts_at: string;
  ends_at: string;
  status: "open" | "drawn" | "canceled";
  entrants: number;
  tickets: number;
  awards: LotteryAward[];
}

interface Standing {
  rank: number;
  customer_id: number;
  name: string;
  username: string | null;
  telegram_id: number;
  phone: string | null;
  points: number;
  purchases: number;
  spent: number;
}

const LOTTERY_TONE = { open: "brand", drawn: "health", canceled: "neutral" } as const;
const PRIZE_KINDS: PrizeKind[] = ["service", "wallet", "text"];
const MEDALS = ["🥇", "🥈", "🥉"];
const EMPTY_LOTTERY = {
  title: "",
  mode: "points" as LotteryRow["mode"],
  pick: "top" as LotteryRow["pick"],
  points_per: 10000,
  board: "self" as LotteryRow["board"],
  ends_at: null as string | null,
};

function medal(rank: number): string {
  return MEDALS[rank - 1] ?? "🏅";
}

function usePrizeText() {
  const { t, locale } = useSession();
  return (prize: LotteryPrize) => {
    const base =
      prize.kind === "wallet"
        ? t("lottery_prize_wallet", { amount: money(prize.value, locale) })
        : prize.kind === "service"
          ? t("lottery_prize_service", { gb: money(prize.value, locale), days: money(prize.days ?? 0, locale) })
          : prize.label;
    return prize.kind !== "text" && prize.label ? `${base} (${prize.label})` : base;
  };
}

function newPrize(panels: PrizePanel[]): LotteryPrize {
  const first = panels[0];
  return first ? { kind: "service", value: 10, days: 30, panel_id: first.id, label: "" } : { kind: "wallet", value: 10000, label: "" };
}

function PrizeEditor({ prizes, panels, onChange }: { prizes: LotteryPrize[]; panels: PrizePanel[]; onChange: (next: LotteryPrize[]) => void }) {
  const { t } = useSession();
  const set = (index: number, patch: Partial<LotteryPrize>) => onChange(prizes.map((p, i) => (i === index ? { ...p, ...patch } : p)));
  return (
    <div className="flex flex-col gap-3">
      {prizes.map((prize, index) => (
        <div key={index} className="flex flex-col gap-2 rounded-xl border border-[var(--line)] p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-bold">
              {medal(index + 1)} {t("lottery_place", { rank: index + 1 })}
            </span>
            <Button small tone="ghost" icon="close" aria-label={t("lottery_prize_remove")} onClick={() => onChange(prizes.filter((_, i) => i !== index))} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Select
              aria-label={t("lottery_prize_kind")}
              value={prize.kind}
              onChange={(e) => {
                const kind = e.target.value as PrizeKind;
                set(index, kind === "service" ? { kind, value: 10, days: 30, panel_id: panels[0]?.id } : { kind, value: kind === "wallet" ? 10000 : 0 });
              }}
            >
              {PRIZE_KINDS.filter((kind) => kind !== "service" || panels.length > 0).map((kind) => (
                <option key={kind} value={kind}>
                  {t(`lottery_kind_${kind}`)}
                </option>
              ))}
            </Select>
            {prize.kind !== "text" && <NumberInput aria-label={t(`lottery_value_${prize.kind}`)} placeholder={t(`lottery_value_${prize.kind}`)} value={prize.value} onChange={(v) => set(index, { value: v ?? 0 })} />}
          </div>
          {prize.kind === "service" && (
            <div className="grid grid-cols-2 gap-2">
              <NumberInput aria-label={t("lottery_value_days")} placeholder={t("lottery_value_days")} value={prize.days ?? 0} onChange={(v) => set(index, { days: v ?? 0 })} />
              <Select aria-label={t("lottery_prize_panel")} value={prize.panel_id ?? ""} onChange={(e) => set(index, { panel_id: Number(e.target.value) })}>
                {panels.map((panel) => (
                  <option key={panel.id} value={panel.id}>
                    {panel.name}
                  </option>
                ))}
              </Select>
            </div>
          )}
          <Input
            aria-label={t("lottery_prize_label")}
            placeholder={prize.kind === "text" ? t("lottery_prize_text_ph") : t("lottery_prize_label")}
            maxLength={64}
            value={prize.label}
            onChange={(e) => set(index, { label: e.target.value })}
          />
        </div>
      ))}
      {prizes.length < 20 && (
        <Button small icon="plus" onClick={() => onChange([...prizes, newPrize(panels)])}>
          {t("lottery_prize_add")}
        </Button>
      )}
    </div>
  );
}

function prizesReady(prizes: LotteryPrize[]): boolean {
  return (
    prizes.length > 0 &&
    prizes.every((p) => (p.kind === "text" ? p.label.trim() !== "" : p.value > 0 && (p.kind !== "service" || ((p.days ?? 0) > 0 && !!p.panel_id))))
  );
}

function StandingsSheet({ lottery, onClose }: { lottery: LotteryRow | null; onClose: () => void }) {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<{ total: number; items: Standing[] }>(lottery ? `/api/admin/lotteries/${lottery.id}/standings?limit=200` : null);
  return (
    <Sheet open={lottery !== null} onClose={onClose} title={lottery ? t("lottery_standings_title", { title: lottery.title }) : ""} wide>
      <Async data={data} error={error} reload={reload}>
        {(value) =>
          value.items.length === 0 ? (
            <p className="text-[var(--hint)]">{t("lottery_standings_empty")}</p>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-[0.8rem] text-[var(--hint)]">{t("lottery_standings_total", { total: money(value.total, locale) })}</p>
              <ol className="divide-y divide-[var(--line)]">
                {value.items.map((row) => (
                  <li key={row.customer_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span className="min-w-0">
                      <span className="font-bold">
                        {row.rank <= 3 ? medal(row.rank) : `${row.rank}.`} {row.name}
                      </span>{" "}
                      <span className="text-[0.8rem] text-[var(--hint)]" dir="ltr">
                        {row.username ? `@${row.username} · ` : ""}
                        {row.telegram_id}
                        {row.phone ? ` · ${row.phone}` : ""}
                      </span>
                    </span>
                    <span className="text-[0.85rem]">
                      {lottery?.mode === "join"
                        ? t("lottery_score_join", { points: money(row.points, locale) })
                        : t("lottery_score", { points: money(row.points, locale), purchases: money(row.purchases, locale) })}{" "}
                      {lottery?.mode !== "join" && <Money amount={row.spent} />}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )
        }
      </Async>
    </Sheet>
  );
}

export function Lotteries() {
  const { t, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const prizeText = usePrizeText();
  const { data, error, reload } = useLoad<{ items: LotteryRow[]; panels: PrizePanel[] }>("/api/admin/lotteries");
  const panels = data?.panels ?? [];
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_LOTTERY);
  const [prizes, setPrizes] = useState<LotteryPrize[]>([]);
  const [board, setBoard] = useState<LotteryRow | null>(null);
  const [editing, setEditing] = useState<LotteryRow | null>(null);
  const [draftPrizes, setDraftPrizes] = useState<LotteryPrize[]>([]);
  const [busy, run] = useAction();

  const create = async () => {
    try {
      await run(() => call("/api/admin/lotteries", { method: "POST", body: { ...form, points_per: form.mode === "points" ? form.points_per : 0, prizes } }));
      toast(t("saved"));
      setOpen(false);
      setForm(EMPTY_LOTTERY);
      setPrizes([]);
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const savePrizes = async () => {
    if (!editing) return;
    try {
      await run(() => call(`/api/admin/lotteries/${editing.id}/prizes`, { method: "PUT", body: { prizes: draftPrizes } }));
      toast(t("saved"));
      setEditing(null);
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const act = async (row: LotteryRow, action: "draw" | "cancel") => {
    const ok = await confirm({
      title: t(`lottery_${action}_title`),
      body: t(`lottery_${action}_body`, { title: row.title }),
      confirm: t(`lottery_${action}`),
      danger: action === "cancel",
    });
    if (!ok) return;
    try {
      await run(() => call(`/api/admin/lotteries/${row.id}/${action}`, { method: "POST" }));
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_lotteries")}
      actions={
        <Button
          tone="primary"
          icon="plus"
          onClick={() => {
            if (prizes.length === 0) setPrizes([newPrize(panels)]);
            setOpen(true);
          }}
        >
          {t("new_lottery")}
        </Button>
      }
    >
      <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("lottery_intro")}</p>
      <Async data={data} error={error} reload={reload}>
        {(value) =>
          value.items.length === 0 ? (
            <Section>
              <p className="text-[var(--hint)]">{t("lottery_empty")}</p>
            </Section>
          ) : (
            <Section flush>
              <ul className="divide-y divide-[var(--line)]">
                {value.items.map((row) => (
                  <li key={row.id} className="flex flex-col gap-2 px-4 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-bold">{row.title}</span>
                      <Badge tone={LOTTERY_TONE[row.status]}>{t(`lottery_status_${row.status}`)}</Badge>
                    </div>
                    <p className="text-[0.8rem] text-[var(--hint)]">
                      {t(`lottery_mode_${row.mode}`, { per: money(row.points_per, locale) })} — {t(`lottery_pick_${row.pick}`)} —{" "}
                      {t("lottery_counts", { entrants: money(row.entrants, locale), tickets: money(row.tickets, locale) })} — <When iso={row.ends_at} time />
                    </p>
                    {row.status !== "drawn" && row.prizes.length > 0 && (
                      <ul className="flex flex-col gap-0.5 text-[0.85rem]">
                        {row.prizes.map((prize, index) => (
                          <li key={index}>
                            {medal(index + 1)} {t("lottery_place", { rank: index + 1 })}: {prizeText(prize)}
                          </li>
                        ))}
                      </ul>
                    )}
                    {row.awards.length > 0 && (
                      <ul className="flex flex-col gap-1.5 text-[0.85rem]">
                        {row.awards.map((award) => (
                          <li key={award.rank} className="rounded-lg bg-[var(--bg)] px-3 py-2">
                            <div className="font-bold">
                              {medal(award.rank)} {t("lottery_place", { rank: award.rank })}: {award.name} — {prizeText(award)}
                            </div>
                            <div className="text-[0.8rem] text-[var(--hint)]" dir="ltr">
                              {award.username ? `@${award.username} · ` : ""}
                              {award.telegram_id ?? ""}
                              {award.phone ? ` · ${award.phone}` : ""}
                            </div>
                            <div className="text-[0.8rem]">{t(`lottery_award_${award.status}`, { service: award.service ?? "" })}</div>
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="flex flex-wrap gap-2">
                      <Button small onClick={() => setBoard(row)}>
                        {t("lottery_standings")}
                      </Button>
                      {row.status === "open" && (
                        <>
                          <Button
                            small
                            onClick={() => {
                              setDraftPrizes(row.prizes.length ? row.prizes : [newPrize(panels)]);
                              setEditing(row);
                            }}
                          >
                            {t("lottery_edit_prizes")}
                          </Button>
                          <Button small busy={busy} disabled={row.prizes.length === 0} onClick={() => void act(row, "draw")}>
                            {t("lottery_draw")}
                          </Button>
                          <Button small tone="ghost" busy={busy} onClick={() => void act(row, "cancel")}>
                            {t("lottery_cancel")}
                          </Button>
                        </>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          )
        }
      </Async>
      <StandingsSheet lottery={board} onClose={() => setBoard(null)} />
      <Sheet
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={t("lottery_edit_prizes")}
        footer={
          <Button tone="primary" busy={busy} disabled={!prizesReady(draftPrizes)} onClick={() => void savePrizes()}>
            {t("save")}
          </Button>
        }
      >
        <PrizeEditor prizes={draftPrizes} panels={panels} onChange={setDraftPrizes} />
      </Sheet>
      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("new_lottery")}
        footer={
          <Button
            tone="primary"
            busy={busy}
            disabled={!form.title.trim() || !form.ends_at || !prizesReady(prizes) || (form.mode === "points" && form.points_per < 1)}
            onClick={() => void create()}
          >
            {t("save")}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <Field label={t("title")}>{(id) => <Input id={id} maxLength={96} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />}</Field>
          <Field label={t("lottery_mode")}>
            {(id) => (
              <Select id={id} value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value as LotteryRow["mode"] })}>
                <option value="points">{t("lottery_mode_points", { per: "…" })}</option>
                <option value="purchases">{t("lottery_mode_purchases")}</option>
                <option value="join">{t("lottery_mode_join")}</option>
              </Select>
            )}
          </Field>
          {form.mode === "points" && (
            <Field label={t("lottery_points_per")} hint={t("lottery_points_per_hint")}>
              {(id) => <NumberInput id={id} value={form.points_per} onChange={(v) => setForm({ ...form, points_per: v ?? 0 })} />}
            </Field>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("lottery_pick")}>
              {(id) => (
                <Select id={id} value={form.pick} onChange={(e) => setForm({ ...form, pick: e.target.value as LotteryRow["pick"] })}>
                  <option value="top">{t("lottery_pick_top")}</option>
                  <option value="random">{t("lottery_pick_random")}</option>
                </Select>
              )}
            </Field>
            <Field label={t("lottery_board")}>
              {(id) => (
                <Select id={id} value={form.board} onChange={(e) => setForm({ ...form, board: e.target.value as LotteryRow["board"] })}>
                  <option value="none">{t("lottery_board_none")}</option>
                  <option value="self">{t("lottery_board_self")}</option>
                  <option value="top">{t("lottery_board_top")}</option>
                </Select>
              )}
            </Field>
          </div>
          <Field label={t("lottery_ends_at")}>
            {(id) => <Input id={id} type="datetime-local" value={toLocalInput(form.ends_at)} onChange={(e) => setForm({ ...form, ends_at: fromLocalInput(e.target.value) })} />}
          </Field>
          <div className="flex flex-col gap-2">
            <span className="text-[0.85rem] font-semibold">{t("lottery_prizes")}</span>
            <p className="text-[0.8rem] text-[var(--hint)]">{t("lottery_prizes_hint")}</p>
            <PrizeEditor prizes={prizes} panels={panels} onChange={setPrizes} />
          </div>
        </div>
      </Sheet>
    </Page>
  );
}
