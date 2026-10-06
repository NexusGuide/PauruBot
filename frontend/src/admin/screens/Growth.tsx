import { useState } from "react";
import { money } from "@shared/format";
import { call, type Broadcast, type CatalogData, type Discount } from "../api";
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
  Status,
  Table,
  Textarea,
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

interface ButtonDraft {
  text: string;
  url: string;
}

export function Broadcasts() {
  const { t, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const list = useList<Broadcast>("/api/admin/broadcasts", []);
  const audience = useLoad<{ reachable: number }>("/api/admin/broadcasts/audience");
  const [composing, setComposing] = useState(false);
  const [text, setText] = useState("");
  const [buttons, setButtons] = useState<ButtonDraft[]>([]);
  const [resellersOnly, setResellersOnly] = useState(false);
  const [buyersOnly, setBuyersOnly] = useState(false);
  const [lang, setLang] = useState("");
  const [busy, run] = useAction();

  const buttonsValid = buttons.every((b) => b.text.trim() && /^(https:\/\/|tg:\/\/)/.test(b.url.trim()));

  const send = async () => {
    const ok = await confirm({
      title: t("broadcast_confirm_title"),
      body: t("broadcast_confirm_body", { n: money(audience.data?.reachable ?? 0, locale) }),
      confirm: t("send"),
    });
    if (!ok) return;
    try {
      await run(() =>
        call("/api/admin/broadcasts", {
          method: "POST",
          body: {
            text,
            buttons: buttons.map((b) => ({ text: b.text.trim(), url: b.url.trim() })),
            resellers_only: resellersOnly,
            buyers_only: buyersOnly,
            locale: lang || null,
          },
        }),
      );
      toast(t("broadcast_queued"));
      setComposing(false);
      setText("");
      setButtons([]);
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const cancel = async (row: Broadcast) => {
    try {
      await call(`/api/admin/broadcasts/${row.id}/cancel`, { method: "POST" });
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const columns: Column<Broadcast>[] = [
    {
      key: "text",
      label: t("message_text"),
      mobile: "title",
      className: "max-w-md",
      render: (b) => <span className="line-clamp-2" dir="auto">{b.text}</span>,
    },
    { key: "status", label: t("col_status"), mobile: "end", render: (b) => <Status value={b.status} /> },
    {
      key: "progress",
      label: t("delivered"),
      mobile: "meta",
      render: (b) => (
        <span className="num">
          {money(b.sent, locale)} / {money(b.total, locale)}
          {b.failed > 0 && <span className="text-anar-700"> ({t("failed_n", { n: money(b.failed, locale) })})</span>}
        </span>
      ),
    },
    { key: "created", label: t("col_created"), mobile: "meta", render: (b) => <When iso={b.created_at} time /> },
    {
      key: "actions",
      label: "",
      mobile: "hide",
      render: (b) =>
        ["queued", "running"].includes(b.status) ? (
          <Button small tone="ghost" onClick={() => void cancel(b)}>
            {t("cancel")}
          </Button>
        ) : null,
    },
  ];

  return (
    <Page
      title={t("nav_broadcasts")}
      actions={
        <Button tone="primary" icon="plus" onClick={() => setComposing(true)}>
          {t("new_broadcast")}
        </Button>
      }
    >
      <Section flush>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      <Sheet
        open={composing}
        onClose={() => setComposing(false)}
        title={t("new_broadcast")}
        footer={
          <Button tone="primary" busy={busy} disabled={!text.trim() || !buttonsValid} onClick={() => void send()}>
            {t("send_broadcast")}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="text-[0.85rem] text-[var(--hint)]">
            {t("broadcast_reach", { n: money(audience.data?.reachable ?? 0, locale) })}
          </p>
          <Field label={t("message_text")} hint={t("broadcast_text_hint")}>
            {(id) => <Textarea id={id} value={text} maxLength={4000} rows={7} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <div className="flex flex-col gap-2">
            <p className="text-[0.82rem] font-semibold">{t("broadcast_buttons")}</p>
            {buttons.map((b, index) => (
              <div key={index} className="grid grid-cols-[1fr_1.4fr_auto] gap-2">
                <Input
                  aria-label={t("button_text")}
                  placeholder={t("button_text")}
                  value={b.text}
                  onChange={(e) => setButtons(buttons.map((x, i) => (i === index ? { ...x, text: e.target.value } : x)))}
                />
                <Input
                  aria-label={t("button_url")}
                  placeholder="https://"
                  dir="ltr"
                  value={b.url}
                  onChange={(e) => setButtons(buttons.map((x, i) => (i === index ? { ...x, url: e.target.value } : x)))}
                />
                <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => setButtons(buttons.filter((_, i) => i !== index))} />
              </div>
            ))}
            {buttons.length < 6 && (
              <Button small icon="plus" className="self-start" onClick={() => setButtons([...buttons, { text: "", url: "" }])}>
                {t("add_button")}
              </Button>
            )}
          </div>
          <Toggle checked={buyersOnly} onChange={setBuyersOnly} label={t("buyers_only")} />
          <Toggle checked={resellersOnly} onChange={setResellersOnly} label={t("resellers_only")} />
          <Field label={t("language")}>
            {(id) => (
              <Select id={id} value={lang} onChange={(e) => setLang(e.target.value)}>
                <option value="">{t("all_languages")}</option>
                <option value="fa">فارسی</option>
                <option value="en">English</option>
              </Select>
            )}
          </Field>
        </div>
      </Sheet>
    </Page>
  );
}
