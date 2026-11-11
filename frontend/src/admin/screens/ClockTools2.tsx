// ClockBot parity, batch 2, panel side. Editors and tools added beside ours; nothing replaced.
import { useState } from "react";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Button, Checkbox, Field, Input, Money, NumberInput, Page, Section, Select, Table, Textarea, When, useConfirm, Intro } from "../ui";

export interface Tier {
  min: number;
  percent: number;
}

/** Tiers the server accepts: 2+ items, 1–90%, sorted by size. */
export function cleanTiers(rows: Tier[]): Tier[] {
  return rows
    .filter((r) => Number.isInteger(r.min) && r.min >= 2 && Number.isInteger(r.percent) && r.percent >= 1 && r.percent <= 90)
    .sort((a, b) => a.min - b.min);
}

export function TierEditor({ value, onChange }: { value: Tier[]; onChange: (value: Tier[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<Tier>) => onChange(value.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2 rounded-xl bg-[var(--bg)] p-2">
          <NumberInput aria-label={t("tier_min")} placeholder={t("tier_min")} value={row.min} onChange={(v) => update(i, { min: v ?? 0 })} />
          <NumberInput aria-label={t("tier_percent")} placeholder={t("tier_percent")} value={row.percent} onChange={(v) => update(i, { percent: v ?? 0 })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, j) => j !== i))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 8} onClick={() => onChange([...value, { min: 5, percent: 5 }])}>
        {t("tier_add")}
      </Button>
    </div>
  );
}

export interface ExtraPackage {
  unit: "gb" | "days";
  amount: number;
  price: number;
}

export function PackagesEditor({ value, onChange }: { value: ExtraPackage[]; onChange: (value: ExtraPackage[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<ExtraPackage>) => onChange(value.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_1.4fr_auto] items-center gap-2 rounded-xl bg-[var(--bg)] p-2">
          <Select aria-label={t("package_unit")} value={row.unit} onChange={(e) => update(i, { unit: e.target.value as ExtraPackage["unit"] })}>
            <option value="gb">{t("package_unit_gb")}</option>
            <option value="days">{t("package_unit_days")}</option>
          </Select>
          <NumberInput aria-label={t("package_amount")} placeholder={t("package_amount")} value={row.amount} onChange={(v) => update(i, { amount: v ?? 0 })} />
          <NumberInput aria-label={t("package_price")} placeholder={t("package_price")} value={row.price} onChange={(v) => update(i, { price: v ?? 0 })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, j) => j !== i))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 12} onClick={() => onChange([...value, { unit: "gb", amount: 10, price: 30_000 }])}>
        {t("package_add")}
      </Button>
    </div>
  );
}

/** A local date-time input value (2026-11-12T20:00) to the ISO string the server keeps, and back. */
export function toIso(local: string): string {
  if (!local) return "";
  const date = new Date(local);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

export function toLocal(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function DateTimeInput({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <Input type="datetime-local" dir="ltr" value={toLocal(value)} onChange={(e) => onChange(toIso(e.target.value))} />;
}

export function IdPicker({ source, value, onChange }: { source: "panels" | "categories"; value: number[]; onChange: (value: number[]) => void }) {
  const { t } = useSession();
  const panels = useLoad<{ items: { id: number; name: string }[] }>(source === "panels" ? "/api/admin/panels" : null);
  const catalog = useLoad<{ categories: { id: number; title: string }[] }>(source === "categories" ? "/api/admin/catalog" : null);
  const options =
    source === "panels"
      ? (panels.data?.items ?? []).map((p) => ({ id: p.id, title: p.name }))
      : (catalog.data?.categories ?? []).map((c) => ({ id: c.id, title: c.title }));
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-[0.78rem] text-[var(--hint)]">{t("ids_none_means_all")}</p>
      {options.map((o) => (
        <label key={o.id} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox label={o.title} checked={value.includes(o.id)} onChange={(on) => onChange(on ? [...value, o.id] : value.filter((id) => id !== o.id))} />
          {o.title}
        </label>
      ))}
    </div>
  );
}

export interface Guide {
  title: string;
  text: string;
  media_url?: string;
  media_type?: "photo" | "video";
  link?: string;
}

export function GuidesEditor({ items, onChange }: { items: Guide[]; onChange: (items: Guide[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<Guide>) => onChange(items.map((g, j) => (j === i ? { ...g, ...patch } : g)));
  return (
    <div className="flex flex-col gap-3">
      {items.map((guide, i) => (
        <div key={i} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-2">
          <Input aria-label={t("guide_title")} placeholder={t("guide_title")} maxLength={40} value={guide.title} onChange={(e) => update(i, { title: e.target.value })} />
          <div className="flex gap-2">
            <Select aria-label={t("guide_media_type")} value={guide.media_type ?? "photo"} onChange={(e) => update(i, { media_type: e.target.value as Guide["media_type"] })}>
              <option value="photo">{t("guide_media_photo")}</option>
              <option value="video">{t("guide_media_video")}</option>
            </Select>
            <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(items.filter((_, j) => j !== i))} />
          </div>
          <Textarea aria-label={t("guide_text")} placeholder={t("guide_text")} maxLength={3000} rows={3} className="sm:col-span-2" value={guide.text} onChange={(e) => update(i, { text: e.target.value })} />
          <Input aria-label={t("guide_media_url")} placeholder="https://… (jpg / mp4)" dir="ltr" value={guide.media_url ?? ""} onChange={(e) => update(i, { media_url: e.target.value.trim() })} />
          <Input aria-label={t("guide_link")} placeholder="https://… (app)" dir="ltr" value={guide.link ?? ""} onChange={(e) => update(i, { link: e.target.value.trim() })} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={items.length >= 12} onClick={() => onChange([...items, { title: "", text: "", media_type: "photo" }])}>
        {t("guide_add")}
      </Button>
    </div>
  );
}

export function ClonePlanButton({ planId, onDone }: { planId: number; onDone: () => void }) {
  const { t, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const clone = async () => {
    try {
      await run(() => call(`/api/admin/plans/${planId}/clone`, { method: "POST" }));
      toast(t("plan_cloned"));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button small tone="ghost" busy={busy} onClick={() => void clone()}>
      {t("plan_clone")}
    </Button>
  );
}

export function SettingsFileButtons({ onImported }: { onImported: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  if (!can("settings.write")) return null;
  const download = async () => {
    try {
      const data = await run(() => call<unknown>("/api/admin/settings/export"));
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "paurubot-settings.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const upload = async (file: File) => {
    const ok = await confirm({ title: t("settings_import"), body: t("settings_import_body"), confirm: t("settings_import"), danger: true });
    if (!ok) return;
    try {
      const body = JSON.parse(await file.text());
      const done = await run(() => call<{ settings: number; texts: number; skipped: number }>("/api/admin/settings/import", { method: "POST", body }));
      toast(t("settings_imported", { settings: done?.settings ?? 0, texts: done?.texts ?? 0, skipped: done?.skipped ?? 0 }));
      onImported();
    } catch (err) {
      toast(err instanceof SyntaxError ? t("err_settings_file_invalid") : errorText(err), "error");
    }
  };
  return (
    <div className="flex flex-wrap gap-2">
      <Button small tone="quiet" busy={busy} onClick={() => void download()}>
        {t("settings_export")}
      </Button>
      <label className="inline-flex min-h-8 cursor-pointer items-center rounded-xl px-3 text-[0.82rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--bg)]">
        {t("settings_import")}
        <input type="file" accept="application/json,.json" className="hidden" onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = ""; // the same file can be picked again
            if (file) void upload(file);
          }} />
      </label>
    </div>
  );
}

export function VerifyBackupButton({ name }: { name: string }) {
  const { t, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const verify = async () => {
    try {
      const done = await run(() => call<{ objects: number; master_key_included: boolean }>(`/api/admin/backups/${encodeURIComponent(name)}/verify`, { method: "POST" }));
      toast(t("backup_verified", { objects: done?.objects ?? 0 }));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button small tone="ghost" busy={busy} onClick={() => void verify()}>
      {t("backup_verify")}
    </Button>
  );
}

/** Chat ids typed as «-100123, 456»: whole numbers, at most ten. */
export function parseChatIds(text: string): number[] {
  return text
    .split(/[,،\s]+/)
    .map((part) => Number(part))
    .filter((n) => Number.isInteger(n) && n !== 0)
    .slice(0, 10);
}

export function BackupBotForm() {
  const { t, toast, errorText } = useSession();
  const current = useLoad<{ bot: boolean; chat_ids: number[] }>("/api/admin/backups/telegram");
  const [token, setToken] = useState("");
  const [chats, setChats] = useState<string | null>(null); // null: still the saved chats
  const [busy, run] = useAction();
  const shownChats = chats ?? (current.data?.chat_ids ?? []).join(", ");
  const save = async () => {
    try {
      const done = await run(() =>
        call<{ bot: boolean; chat_ids: number[] }>("/api/admin/backups/telegram", {
          method: "PUT",
          body: { bot_token: token.trim() || null, chat_ids: parseChatIds(shownChats) },
        }),
      );
      setToken("");
      setChats(null);
      void current.reload();
      toast(t(done?.bot && done.chat_ids.length ? "backup_bot_saved" : "backup_bot_cleared"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("backup_bot_title")}>
      <p className="mb-3 text-[0.82rem] text-[var(--hint)]">{t("backup_bot_hint")}</p>
      <div className="grid gap-3 md:grid-cols-2">
        <Field label={t("backup_bot_token")}>{(id) => <Input id={id} type="password" dir="ltr" autoComplete="off" placeholder={current.data?.bot ? "••••••••" : "123456:ABC…"} value={token} onChange={(e) => setToken(e.target.value)} />}</Field>
        <Field label={t("backup_bot_chats")}>{(id) => <Input id={id} dir="ltr" placeholder="-1001234567890" value={shownChats} onChange={(e) => setChats(e.target.value)} />}</Field>
      </div>
      <Button className="mt-3" tone="primary" busy={busy} onClick={() => void save()}>
        {t("save")}
      </Button>
    </Section>
  );
}

interface StuckRow {
  id: number;
  kind: string;
  status: string;
  amount: number;
  customer_id: number;
  created_at: string;
  error: string | null;
}

export function StuckOrders() {
  const { t, can, toast, errorText } = useSession();
  const { data, reload } = useLoad<{ items: StuckRow[] }>("/api/admin/stuck-orders");
  const [busy, run] = useAction();
  const [noteFor, setNoteFor] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const act = async (path: string, body?: unknown) => {
    try {
      await run(() => call(path, { method: "POST", body }));
      toast(t("saved"));
      setNoteFor(null);
      setNote("");
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const writable = can("services.write");
  return (
    <Page title={t("nav_stuck_orders")} wide>
      <Intro>{t("stuck_intro")}</Intro>
      <Section flush>
        <Table
          rows={data?.items ?? []}
          columns={[
            { key: "id", label: "#", mobile: "title", render: (r) => <b>#{r.id}</b> },
            { key: "kind", label: t("col_kind"), render: (r) => t(`order_kind_${r.kind}`) },
            { key: "status", label: t("col_status"), mobile: "meta", render: (r) => t(`status_${r.status}`) },
            { key: "amount", label: t("col_amount"), render: (r) => <Money amount={r.amount} /> },
            { key: "when", label: t("col_created"), render: (r) => <When iso={r.created_at} time /> },
            { key: "error", label: t("stuck_error"), render: (r) => <span className="text-[0.8rem] text-[var(--hint)]">{r.error ?? "—"}</span> },
            {
              key: "actions",
              label: "",
              mobile: "end",
              render: (r) =>
                writable ? (
                  <span className="flex flex-wrap gap-2">
                    {r.status !== "failed" && (
                      <Button small disabled={busy} onClick={() => void act(`/api/admin/orders/${r.id}/retry`)}>
                        {t("stuck_retry")}
                      </Button>
                    )}
                    {noteFor === r.id ? (
                      <span className="flex gap-1">
                        <Input aria-label={t("stuck_note")} placeholder={t("stuck_note")} value={note} onChange={(e) => setNote(e.target.value)} />
                        <Button small tone="primary" disabled={busy || !note.trim()} onClick={() => void act(`/api/admin/orders/${r.id}/mark-delivered`, { note })}>
                          {t("save")}
                        </Button>
                      </span>
                    ) : (
                      <Button small onClick={() => setNoteFor(r.id)}>
                        {t("stuck_mark_delivered")}
                      </Button>
                    )}
                  </span>
                ) : null,
            },
          ]}
        />
      </Section>
    </Page>
  );
}

export function CustomerPhoto({ customerId, name }: { customerId: number; name: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className="flex size-12 items-center justify-center rounded-full bg-firouzeh-100 text-lg font-extrabold text-firouzeh-700">
        {name.slice(0, 1).toUpperCase()}
      </span>
    );
  }
  return <img src={`/api/admin/customers/${customerId}/photo`} alt="" onError={() => setFailed(true)} className="size-12 rounded-full object-cover" />;
}

export function QuickRenewButton({ customerId, onDone }: { customerId: number; onDone: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  if (!can("services.create")) return null;
  const renew = async () => {
    const ok = await confirm({ title: t("quick_renew"), body: t("quick_renew_body"), confirm: t("quick_renew") });
    if (!ok) return;
    try {
      await run(() => call(`/api/admin/customers/${customerId}/quick-renew`, { method: "POST", body: {} }));
      toast(t("quick_renew_done"));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button busy={busy} onClick={() => void renew()}>
      {t("quick_renew")}
    </Button>
  );
}

export function TestCustomerToggle({ customerId, value, onDone }: { customerId: number; value: boolean; onDone: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const [busy, run] = useAction();
  if (!can("customers.write")) return null;
  const flip = async () => {
    try {
      await run(() => call(`/api/admin/customers/${customerId}`, { method: "PATCH", body: { is_test: !value } }));
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button tone={value ? "primary" : "quiet"} busy={busy} aria-pressed={value} onClick={() => void flip()}>
      {t(value ? "test_customer_on" : "test_customer_off")}
    </Button>
  );
}
