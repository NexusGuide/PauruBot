// ClockBot parity, panel side: things ClockBot's panel has that ours lacked. Each is added
// beside what was there; nothing of ours is replaced.
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { digits } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Checkbox, Field, Input, NumberInput, Page, Section, Sheet, Table, Textarea, When, useConfirm } from "../ui";

export const TELEGRAM_ID_MIN = 10_000;

/** A Telegram id the server accepts for a new customer. */
export function validTelegramId(value: number | null): value is number {
  return value !== null && Number.isInteger(value) && value >= TELEGRAM_ID_MIN && value <= 10 ** 13;
}

export function NewCustomerButton() {
  const { t, can, toast, errorText } = useSession();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [telegramId, setTelegramId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [busy, run] = useAction();
  if (!can("customers.write")) return null;
  const save = async () => {
    if (!validTelegramId(telegramId)) return;
    try {
      const made = await run(() =>
        call<{ id: number }>("/api/admin/customers", {
          method: "POST",
          body: { telegram_id: telegramId, first_name: name.trim() || null, username: username.trim() || null },
        }),
      );
      toast(t("cb_customer_created"));
      setOpen(false);
      if (made) navigate(`/customers/${made.id}`);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <>
      <Button tone="quiet" icon="plus" onClick={() => setOpen(true)}>
        {t("cb_new_customer")}
      </Button>
      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("cb_new_customer")}
        footer={
          <Button tone="primary" busy={busy} disabled={!validTelegramId(telegramId)} onClick={() => void save()}>
            {t("cb_create")}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="text-[0.86rem] text-[var(--hint)]">{t("cb_new_customer_hint")}</p>
          <Field label={t("cb_telegram_id")}>{(id) => <NumberInput id={id} dir="ltr" value={telegramId} onChange={setTelegramId} />}</Field>
          <Field label={t("cb_first_name")}>{(id) => <Input id={id} maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />}</Field>
          <Field label={t("cb_username")}>{(id) => <Input id={id} dir="ltr" maxLength={64} value={username} onChange={(e) => setUsername(e.target.value)} />}</Field>
        </div>
      </Sheet>
    </>
  );
}

export function BulkMessageButton({ ids, onDone }: { ids: number[]; onDone: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [reply, setReply] = useState(false); // Foxima parity: a reply button under the message
  const [busy, run] = useAction();
  if (!can("customers.write") || ids.length === 0) return null;
  const send = async () => {
    try {
      const done = await run(() =>
        call<{ queued: number }>("/api/admin/customers/bulk", { method: "POST", body: { action: "message", ids, text, reply } }),
      );
      toast(t("cb_message_queued", { n: done?.queued ?? 0 }));
      setOpen(false);
      setText("");
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <>
      <Button small onClick={() => setOpen(true)}>
        {t("cb_message_many")}
      </Button>
      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("cb_message_many")}
        footer={
          <Button tone="primary" busy={busy} disabled={!text.trim()} onClick={() => void send()}>
            {t("cb_send_to", { n: ids.length })}
          </Button>
        }
      >
        <Field label={t("cb_message_text")} hint={t("cb_message_hint")}>
          {(id) => <Textarea id={id} rows={5} maxLength={3500} value={text} onChange={(e) => setText(e.target.value)} />}
        </Field>
        <label className="mt-3 flex items-center gap-2 text-[0.88rem]">
          <Checkbox label={t("cb_message_reply")} checked={reply} onChange={setReply} />
          {t("cb_message_reply")}
        </label>
      </Sheet>
    </>
  );
}

export function DeleteCustomerButton({ customerId, onDeleted }: { customerId: number; onDeleted: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  if (!can("customers.ban")) return null;
  const remove = async () => {
    const ok = await confirm({ title: t("cb_delete_customer"), body: t("cb_delete_customer_body"), confirm: t("delete"), danger: true });
    if (!ok) return;
    try {
      await run(() => call(`/api/admin/customers/${customerId}`, { method: "DELETE" }));
      toast(t("cb_customer_deleted"));
      onDeleted();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button tone="danger" busy={busy} onClick={() => void remove()}>
      {t("cb_delete_customer")}
    </Button>
  );
}

interface TrialRow {
  customer: { id: number; name: string; username: string | null; banned: boolean };
  trial_at: string | null;
  bought: boolean;
}

export type TrialFilter = "all" | "bought" | "not_bought";

export function TrialUsers() {
  const { t, locale } = useSession();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<TrialFilter>("all");
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const { data, error, reload } = useLoad<{ summary: { trials: number; bought: number; rate: number }; items: TrialRow[] }>(
    `/api/admin/trial-users?filter=${filter}`,
  );
  return (
    <Page title={t("nav_trial_users")} wide>
      <Async data={data} error={error} reload={() => void reload()}>
        {(value) => {
          const rows = value.items.map((row) => ({ ...row, id: row.customer.id }));
          return (
            <>
              <div className="grid grid-cols-3 gap-3">
                {[
                  [t("cb_trials"), digits(value.summary.trials, locale)],
                  [t("cb_trials_bought"), digits(value.summary.bought, locale)],
                  [t("cb_trials_rate"), `${digits(value.summary.rate, locale)}٪`],
                ].map(([label, figure]) => (
                  <div key={label} className="rounded-2xl bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
                    <p className="text-[0.8rem] text-[var(--hint)]">{label}</p>
                    <p className="mt-1 text-xl font-extrabold">{figure}</p>
                  </div>
                ))}
              </div>
              <Section
                flush
                title={t("nav_trial_users")}
                actions={
                  <div className="flex flex-wrap gap-2">
                    {(["all", "bought", "not_bought"] as TrialFilter[]).map((f) => (
                      <Button key={f} small tone={filter === f ? "primary" : "quiet"} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                        {t(`cb_trial_filter_${f}`)}
                      </Button>
                    ))}
                    <BulkMessageButton ids={[...picked]} onDone={() => setPicked(new Set())} />
                  </div>
                }
              >
                <Table
                  rows={rows}
                  onRow={(row) => navigate(`/customers/${row.id}`)}
                  selection={{
                    selected: picked,
                    toggle: (id) =>
                      setPicked((prev) => {
                        const next = new Set(prev);
                        if (next.has(id)) next.delete(id);
                        else next.add(id);
                        return next;
                      }),
                    setAll: (ids, on) => setPicked(on ? new Set(ids) : new Set()),
                  }}
                  columns={[
                    { key: "name", label: t("col_customer"), mobile: "title", render: (r) => <b>{r.customer.name}</b> },
                    { key: "when", label: t("cb_trial_at"), mobile: "meta", render: (r) => <When iso={r.trial_at} /> },
                    {
                      key: "bought",
                      label: t("cb_trial_result"),
                      mobile: "end",
                      render: (r) => <Badge tone={r.bought ? "health" : "neutral"}>{t(r.bought ? "cb_trial_bought" : "cb_trial_not_bought")}</Badge>,
                    },
                  ]}
                />
              </Section>
            </>
          );
        }}
      </Async>
    </Page>
  );
}

export function ClearBroadcastsButton({ onDone }: { onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  const clear = async () => {
    const ok = await confirm({ title: t("cb_clear_history"), body: t("cb_clear_history_body"), confirm: t("cb_clear_history"), danger: true });
    if (!ok) return;
    try {
      const done = await run(() => call<{ deleted: number }>("/api/admin/broadcasts/clear", { method: "POST" }));
      toast(t("cb_cleared", { n: done?.deleted ?? 0 }));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button tone="quiet" busy={busy} onClick={() => void clear()}>
      {t("cb_clear_history")}
    </Button>
  );
}

export const FINISHED_BROADCAST = ["done", "canceled", "failed", "dead"];

export function DeleteBroadcastButton({ id, status, onDone }: { id: number; status: string; onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const [busy, run] = useAction();
  if (!FINISHED_BROADCAST.includes(status)) return null;
  const remove = async () => {
    try {
      await run(() => call(`/api/admin/broadcasts/${id}`, { method: "DELETE" }));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <button type="button" disabled={busy} onClick={() => void remove()}>
      {t("delete")}
    </button>
  );
}

export function InboxResetButton({ onDone }: { onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const reset = async () => {
    try {
      const done = await run(() => call<{ restored: number }>("/api/admin/inbox/reset", { method: "POST" }));
      toast(t("cb_inbox_restored", { n: done?.restored ?? 0 }));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button small tone="ghost" busy={busy} onClick={() => void reset()}>
      {t("cb_inbox_reset")}
    </Button>
  );
}

export function ResetTextsButton({ keys, onDone }: { keys: string[]; onDone: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  if (!can("texts.write") || keys.length === 0) return null;
  const reset = async () => {
    const ok = await confirm({ title: t("cb_reset_tab"), body: t("cb_reset_tab_body", { n: keys.length }), confirm: t("cb_reset_tab"), danger: true });
    if (!ok) return;
    try {
      const done = await run(() => call<{ reset: number }>("/api/admin/texts/reset", { method: "POST", body: { keys } }));
      toast(t("cb_texts_reset", { n: done?.reset ?? 0 }));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button small tone="quiet" busy={busy} onClick={() => void reset()}>
      {t("cb_reset_tab")} · {keys.length}
    </Button>
  );
}

/** Amounts typed as «50000، 100000»: whole positive numbers, at most eight. */
export function parseAmounts(text: string): number[] {
  return text
    .split(/[,،\s]+/)
    .map((part) => Number(part.replace(/[٬,]/g, "")))
    .filter((n) => Number.isInteger(n) && n > 0)
    .slice(0, 8);
}

export function AmountList({ value, onChange }: { value: number[]; onChange: (value: number[]) => void }) {
  const { t } = useSession();
  return (
    <Input
      aria-label={t("setting_wallet_topup_presets")}
      dir="ltr"
      defaultValue={value.join(", ")}
      placeholder="50000, 100000, 200000"
      onBlur={(e) => onChange(parseAmounts(e.target.value))}
    />
  );
}
