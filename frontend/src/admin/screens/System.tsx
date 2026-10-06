import { useMemo, useState } from "react";
import { money } from "@shared/format";
import { call, qs, type AuditRow, type Channels as ChannelData, type JobRow, type SalesReport, type SettingItem, type TextItem } from "../api";
import { Columns } from "../chart";
import { PAGE_SIZE, useAction, useList, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Bytes,
  Button,
  Empty,
  Facts,
  Field,
  Input,
  Money,
  NumberInput,
  Page,
  Pager,
  SearchBox,
  Section,
  Select,
  Sheet,
  Status,
  Table,
  Textarea,
  Toggle,
  Toolbar,
  When,
  type Column,
} from "../ui";
import { compact } from "./Home";

// ---------------------------------------------------------------- settings

interface CardAccount {
  number: string;
  holder: string;
  bank: string;
}

function CardAccounts({ value, onChange }: { value: CardAccount[]; onChange: (value: CardAccount[]) => void }) {
  const { t } = useSession();
  const update = (index: number, key: keyof CardAccount, text: string) =>
    onChange(value.map((row, i) => (i === index ? { ...row, [key]: text } : row)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((row, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1.4fr_1fr_1fr_auto]">
          <Input aria-label={t("card_number")} placeholder={t("card_number")} dir="ltr" value={row.number} onChange={(e) => update(index, "number", e.target.value)} />
          <Input aria-label={t("card_holder")} placeholder={t("card_holder")} value={row.holder} onChange={(e) => update(index, "holder", e.target.value)} />
          <Input aria-label={t("card_bank")} placeholder={t("card_bank")} value={row.bank} onChange={(e) => update(index, "bank", e.target.value)} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" onClick={() => onChange([...value, { number: "", holder: "", bank: "" }])}>
        {t("add_card")}
      </Button>
    </div>
  );
}

function SettingControl({
  item,
  value,
  onChange,
  canSecrets,
}: {
  item: SettingItem;
  value: unknown;
  onChange: (value: unknown) => void;
  canSecrets: boolean;
}) {
  const { t } = useSession();
  const label = t(`setting_${item.key}`);
  const hintKey = `setting_${item.key}_hint`;
  const hintText = t(hintKey);
  const hint = hintText === hintKey ? undefined : hintText;

  if (item.type === "bool") return <Toggle checked={Boolean(value)} onChange={onChange} label={label} hint={hint} />;
  if (item.key === "username_method") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "telegram_id")} onChange={(e) => onChange(e.target.value)}>
            {["telegram_id", "telegram_username", "random", "sequential", "custom_label"].map((method) => (
              <option key={method} value={method}>
                {t(`username_method_${method}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "card_accounts") {
    return (
      <Field label={label} hint={hint}>
        {() => <CardAccounts value={(value as CardAccount[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.type === "list") {
    const list = Array.isArray(value) ? (value as unknown[]).map(String) : [];
    return (
      <Field label={label} hint={hint ?? t("comma_separated")}>
        {(id) => (
          <Input
            id={id}
            dir="ltr"
            value={list.join(", ")}
            onChange={(e) =>
              onChange(
                e.target.value
                  .split(",")
                  .map((part) => part.trim())
                  .filter(Boolean),
              )
            }
          />
        )}
      </Field>
    );
  }
  if (item.secret) {
    const set = typeof value === "object" && value !== null && "set" in value ? Boolean((value as { set: boolean }).set) : Boolean(value);
    return (
      <Field label={label} hint={canSecrets ? (set ? t("secret_set_hint") : hint) : t("secrets_owner_only")}>
        {(id) => (
          <Input
            id={id}
            type="password"
            dir="ltr"
            autoComplete="off"
            disabled={!canSecrets}
            placeholder={set ? "••••••••" : ""}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
      </Field>
    );
  }
  if (item.type === "int") {
    return (
      <Field label={label} hint={hint}>
        {(id) => <NumberInput id={id} value={typeof value === "number" ? value : null} onChange={onChange} allowEmpty />}
      </Field>
    );
  }
  return (
    <Field label={label} hint={hint}>
      {(id) => <Input id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  );
}

export function Settings() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ groups: Record<string, SettingItem[]> }>("/api/admin/settings");
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [busy, run] = useAction();
  const writable = can("settings.write");
  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    try {
      await run(() => call("/api/admin/settings", { method: "PUT", body: { values: changes } }));
      setChanges({});
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_settings")}
      actions={
        writable && (
          <>
            {dirty && <Button onClick={() => setChanges({})}>{t("discard")}</Button>}
            <Button tone="primary" busy={busy} disabled={!dirty} onClick={() => void save()}>
              {t("save_changes")}
            </Button>
          </>
        )
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            {Object.entries(value.groups).map(([group, items]) => (
              <Section key={group} title={t(`settings_group_${group}`)}>
                <div className="grid gap-5 md:grid-cols-2">
                  {items.map((item) => (
                    <div key={item.key} className={item.key === "card_accounts" || item.type === "bool" ? "md:col-span-2" : ""}>
                      <SettingControl
                        item={item}
                        value={item.key in changes ? changes[item.key] : item.value}
                        canSecrets={can("panels.secrets") && writable}
                        onChange={(next) => writable && setChanges((prev) => ({ ...prev, [item.key]: next }))}
                      />
                    </div>
                  ))}
                </div>
              </Section>
            ))}
          </>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- texts

interface TextsData {
  locales: string[];
  globals: string[];
  items: TextItem[];
}

function TextSheet({ item, locales, globals, onClose, onSaved }: { item: TextItem; locales: string[]; globals: string[]; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [locale, setLocale] = useState(locales[0] ?? "fa");
  const [value, setValue] = useState(item.overrides[locale] ?? item.defaults[locale] ?? "");
  const [busy, run] = useAction();

  const pick = (next: string) => {
    setLocale(next);
    setValue(item.overrides[next] ?? item.defaults[next] ?? "");
  };
  const save = async () => {
    try {
      await run(() => call(`/api/admin/texts/${locale}/${item.key}`, { method: "PUT", body: { value } }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const reset = async () => {
    try {
      await run(() => call(`/api/admin/texts/${locale}/${item.key}`, { method: "DELETE" }));
      toast(t("text_reset_done"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Sheet
      open
      onClose={onClose}
      title={item.key}
      wide
      footer={
        <>
          {item.overrides[locale] !== undefined && (
            <Button busy={busy} onClick={() => void reset()}>
              {t("text_reset")}
            </Button>
          )}
          <Button tone="primary" busy={busy} disabled={!value.trim()} onClick={() => void save()}>
            {t("save")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex gap-2">
          {locales.map((loc) => (
            <Button key={loc} small tone={loc === locale ? "primary" : "quiet"} onClick={() => pick(loc)}>
              {loc === "fa" ? "فارسی" : loc === "en" ? "English" : loc}
            </Button>
          ))}
        </div>
        <Textarea aria-label={item.key} rows={10} dir={locale === "fa" ? "rtl" : "ltr"} value={value} onChange={(e) => setValue(e.target.value)} />
        <div className="flex flex-col gap-1.5">
          <p className="text-[0.8rem] font-semibold">{t("text_variables")}</p>
          <p className="flex flex-wrap gap-1.5" dir="ltr">
            {[...item.variables, ...globals].map((name) => (
              <code key={name} className="rounded-md bg-[var(--bg)] px-1.5 py-0.5 text-[0.78rem]">{`{${name}}`}</code>
            ))}
          </p>
          <p className="text-[0.78rem] text-[var(--hint)]">{t("text_html_hint")}</p>
        </div>
        <details className="text-[0.85rem]">
          <summary className="cursor-pointer font-semibold text-[var(--hint)]">{t("text_default")}</summary>
          <p className="mt-2 whitespace-pre-wrap rounded-xl bg-[var(--bg)] p-3" dir="auto">
            {item.defaults[locale]}
          </p>
        </details>
      </div>
    </Sheet>
  );
}

export function Texts() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<TextsData>("/api/admin/texts");
  const [query, setQuery] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [open, setOpen] = useState<TextItem | null>(null);
  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (data?.items ?? []).filter((item) => {
      if (onlyChanged && Object.keys(item.overrides).length === 0) return false;
      if (!q) return true;
      return item.key.includes(q) || Object.values(item.defaults).some((text) => text.toLowerCase().includes(q));
    });
  }, [data, query, onlyChanged]);

  return (
    <Page title={t("nav_texts")}>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <Section flush>
            <Toolbar>
              <SearchBox value={query} onChange={setQuery} placeholder={t("search_texts")} />
              <Button small tone={onlyChanged ? "primary" : "quiet"} onClick={() => setOnlyChanged((v) => !v)}>
                {t("only_changed")}
              </Button>
            </Toolbar>
            {items.length === 0 ? (
              <Empty title={t("empty_list")} />
            ) : (
              <ul className="divide-y divide-[var(--line)]">
                {items.map((item) => (
                  <li key={item.key}>
                    <button type="button" onClick={() => setOpen(item)} className="flex w-full flex-col gap-1 px-4 py-3 text-start hover:bg-[var(--bg)]">
                      <span className="flex items-center gap-2">
                        <code className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                          {item.key}
                        </code>
                        {Object.keys(item.overrides).length > 0 && <Badge tone="brand">{t("customised")}</Badge>}
                      </span>
                      <span className="line-clamp-2 text-[0.88rem]" dir="auto">
                        {item.overrides[locale] ?? item.defaults[locale] ?? item.defaults.fa}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {open && (
              <TextSheet
                item={open}
                locales={value.locales}
                globals={value.globals}
                onClose={() => setOpen(null)}
                onSaved={() => {
                  setOpen(null);
                  void reload();
                }}
              />
            )}
          </Section>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- channels

export function Channels() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<ChannelData>("/api/admin/channels");
  const [busy, run] = useAction();
  const [forced, setForced] = useState({ chat_id: "", title: "", url: "https://t.me/" });
  const [contact, setContact] = useState({ title: "", url: "https://t.me/" });
  const [log, setLog] = useState<{ topic: string; chat_id: string; thread_id: string } | null>(null);
  const writable = can("settings.write");

  const act = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await run(fn);
      toast(t(done));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page title={t("nav_channels")}>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <Section title={t("forced_channels")}>
              <p className="mb-3 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("forced_channels_hint")}</p>
              <ul className="mb-4 divide-y divide-[var(--line)]">
                {value.forced.map((c) => (
                  <li key={c.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="flex flex-col">
                      <span className="font-semibold">{c.title}</span>
                      <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                        {c.chat_id}
                      </span>
                    </span>
                    {writable && (
                      <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/channels/forced/${c.id}`, { method: "DELETE" }), "removed")}>
                        {t("remove")}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              {writable && (
                <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1.3fr_auto]">
                  <Input aria-label={t("channel_id")} placeholder="@channel / -100…" dir="ltr" value={forced.chat_id} onChange={(e) => setForced({ ...forced, chat_id: e.target.value })} />
                  <Input aria-label={t("title")} placeholder={t("title")} value={forced.title} onChange={(e) => setForced({ ...forced, title: e.target.value })} />
                  <Input aria-label={t("invite_link")} dir="ltr" value={forced.url} onChange={(e) => setForced({ ...forced, url: e.target.value })} />
                  <Button
                    tone="primary"
                    busy={busy}
                    disabled={!forced.chat_id || !forced.title}
                    onClick={() =>
                      void act(async () => {
                        await call("/api/admin/channels/forced", { method: "POST", body: forced });
                        setForced({ chat_id: "", title: "", url: "https://t.me/" });
                      }, "added")
                    }
                  >
                    {t("add")}
                  </Button>
                </div>
              )}
            </Section>

            <Section title={t("log_channels")}>
              <p className="mb-3 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("log_channels_hint")}</p>
              <ul className="divide-y divide-[var(--line)]">
                {value.topics.map((topic) => {
                  const row = value.logs.find((l) => l.topic === topic);
                  return (
                    <li key={topic} className="flex items-center justify-between gap-3 py-2.5">
                      <span className="flex flex-col">
                        <span className="font-semibold">{t(`log_topic_${topic}`)}</span>
                        <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                          {row ? `${row.chat_id}${row.thread_id ? ` / ${row.thread_id}` : ""}` : t("not_set")}
                        </span>
                      </span>
                      {writable && (
                        <span className="flex gap-2">
                          {row && (
                            <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/channels/logs/${row.id}`, { method: "DELETE" }), "removed")}>
                              {t("remove")}
                            </Button>
                          )}
                          <Button
                            small
                            onClick={() => setLog({ topic, chat_id: row ? String(row.chat_id) : "", thread_id: row?.thread_id ? String(row.thread_id) : "" })}
                          >
                            {row ? t("edit") : t("set")}
                          </Button>
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Section>

            <Section title={t("support_contacts")}>
              <ul className="mb-4 divide-y divide-[var(--line)]">
                {value.contacts.map((c) => (
                  <li key={c.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="flex flex-col">
                      <span className="font-semibold">{c.title}</span>
                      <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                        {c.url}
                      </span>
                    </span>
                    {writable && (
                      <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/contacts/${c.id}`, { method: "DELETE" }), "removed")}>
                        {t("remove")}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              {writable && (
                <div className="grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]">
                  <Input aria-label={t("title")} placeholder={t("title")} value={contact.title} onChange={(e) => setContact({ ...contact, title: e.target.value })} />
                  <Input aria-label={t("link")} dir="ltr" value={contact.url} onChange={(e) => setContact({ ...contact, url: e.target.value })} />
                  <Button
                    tone="primary"
                    busy={busy}
                    disabled={!contact.title}
                    onClick={() =>
                      void act(async () => {
                        await call("/api/admin/contacts", { method: "POST", body: contact });
                        setContact({ title: "", url: "https://t.me/" });
                      }, "added")
                    }
                  >
                    {t("add")}
                  </Button>
                </div>
              )}
            </Section>

            <Sheet
              open={log !== null}
              onClose={() => setLog(null)}
              title={log ? t(`log_topic_${log.topic}`) : ""}
              footer={
                <Button
                  tone="primary"
                  busy={busy}
                  disabled={!log || !/^-?\d+$/.test(log.chat_id.trim())}
                  onClick={() =>
                    log &&
                    void act(async () => {
                      await call("/api/admin/channels/logs", {
                        method: "PUT",
                        body: { topic: log.topic, chat_id: Number(log.chat_id.trim()), thread_id: log.thread_id.trim() ? Number(log.thread_id.trim()) : null },
                      });
                      setLog(null);
                    }, "saved")
                  }
                >
                  {t("save")}
                </Button>
              }
            >
              {log && (
                <div className="flex flex-col gap-4">
                  <Field label={t("chat_id")} hint={t("chat_id_hint")}>
                    {(id) => <Input id={id} dir="ltr" value={log.chat_id} onChange={(e) => setLog({ ...log, chat_id: e.target.value })} />}
                  </Field>
                  <Field label={t("thread_id")} hint={t("thread_id_hint")}>
                    {(id) => <Input id={id} dir="ltr" value={log.thread_id} onChange={(e) => setLog({ ...log, thread_id: e.target.value })} />}
                  </Field>
                </div>
              )}
            </Sheet>
          </>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- audit

function Diff({ before, after }: { before: Record<string, unknown> | null; after: Record<string, unknown> | null }) {
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])];
  if (keys.length === 0) return null;
  const show = (v: unknown) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
  return (
    <table className="w-full text-[0.8rem]" dir="ltr">
      <tbody>
        {keys.map((key) => {
          const a = before?.[key];
          const b = after?.[key];
          const changed = JSON.stringify(a) !== JSON.stringify(b);
          return (
            <tr key={key} className="border-t border-[var(--line)] align-top">
              <td className="py-1.5 pe-3 font-semibold">{key}</td>
              <td className={`py-1.5 pe-3 break-all ${changed && before ? "text-anar-700 line-through decoration-anar-500/50" : "text-[var(--hint)]"}`}>
                {before ? show(a) : ""}
              </td>
              <td className={`py-1.5 break-all ${changed ? "text-pista-700" : "text-[var(--hint)]"}`}>{show(b)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function Audit() {
  const { t } = useSession();
  const list = useList<AuditRow>("/api/admin/audit", ["action", "entity", "entity_id", "actor_id"]);
  const [open, setOpen] = useState<AuditRow | null>(null);
  const columns: Column<AuditRow>[] = [
    { key: "action", label: t("audit_action"), mobile: "title", render: (r) => <code className="text-[0.82rem]" dir="ltr">{r.action}</code> },
    {
      key: "entity",
      label: t("audit_entity"),
      mobile: "meta",
      render: (r) => (r.entity ? <span dir="ltr">{`${r.entity}${r.entity_id ? `#${r.entity_id}` : ""}`}</span> : "—"),
    },
    { key: "actor", label: t("audit_actor"), mobile: "meta", render: (r) => <span dir="ltr">{`${r.actor_type}${r.actor_id ? `#${r.actor_id}` : ""}`}</span> },
    { key: "ip", label: "IP", mobile: "hide", render: (r) => <span dir="ltr">{r.ip ?? "—"}</span> },
    { key: "at", label: t("audit_when"), mobile: "end", render: (r) => <When iso={r.created_at} time /> },
  ];
  return (
    <Page title={t("nav_audit")} wide>
      <Section flush>
        <Toolbar>
          <SearchBox value={list.get("action")} onChange={(action) => list.set({ action })} placeholder={t("audit_filter_action")} />
          <Input className="w-40" aria-label={t("audit_entity")} placeholder={t("audit_entity")} dir="ltr" value={list.get("entity")} onChange={(e) => list.set({ entity: e.target.value })} />
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} onRow={setOpen} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      {open && (
        <Sheet open onClose={() => setOpen(null)} title={open.action} wide>
          <div className="flex flex-col gap-4">
            <Facts
              items={[
                [t("audit_actor"), <span key="a" dir="ltr">{`${open.actor_type}${open.actor_id ? `#${open.actor_id}` : ""}`}</span>],
                ["IP", <span key="ip" dir="ltr">{open.ip ?? "—"}</span>],
                [t("audit_when"), <When key="w" iso={open.created_at} time />],
              ]}
            />
            <Diff before={open.before} after={open.after} />
          </div>
        </Sheet>
      )}
    </Page>
  );
}

// ---------------------------------------------------------------- jobs

export function Jobs() {
  const { t, locale, toast, errorText } = useSession();
  const list = useList<JobRow>("/api/admin/jobs", ["status"]);
  const [busy, run] = useAction();
  const data = list.data as (typeof list.data & { counts?: Record<string, number> }) | null;

  const retry = async (job: JobRow) => {
    try {
      await run(() => call(`/api/admin/jobs/${job.id}/retry`, { method: "POST" }));
      toast(t("job_requeued"));
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const columns: Column<JobRow>[] = [
    { key: "kind", label: t("job_kind"), mobile: "title", render: (j) => <code dir="ltr" className="text-[0.82rem]">{`${j.queue}/${j.kind}`}</code> },
    { key: "status", label: t("col_status"), mobile: "end", render: (j) => <Status value={j.status} /> },
    { key: "attempts", label: t("job_attempts"), mobile: "meta", render: (j) => <span className="num">{`${money(j.attempts, locale)} / ${money(j.max_attempts, locale)}`}</span> },
    { key: "error", label: t("job_error"), mobile: "meta", className: "max-w-sm", render: (j) => <span className="line-clamp-2 text-anar-700" dir="ltr">{j.last_error ?? ""}</span> },
    { key: "run", label: t("job_run_at"), mobile: "hide", render: (j) => <When iso={j.run_at} time /> },
    {
      key: "retry",
      label: "",
      mobile: "end",
      render: (j) =>
        ["dead", "failed"].includes(j.status) ? (
          <Button small busy={busy} onClick={() => void retry(j)}>
            {t("retry")}
          </Button>
        ) : null,
    },
  ];
  return (
    <Page title={t("nav_jobs")} wide>
      {data?.counts && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(data.counts)
            .filter(([, n]) => n > 0)
            .map(([status, n]) => (
              <button key={status} type="button" onClick={() => list.set({ status })} className="rounded-xl px-3 py-1.5 ring-1 ring-[var(--line)] hover:bg-[var(--surface)]">
                <Status value={status} /> <span className="num ms-1 font-bold">{money(n, locale)}</span>
              </button>
            ))}
        </div>
      )}
      <Section flush>
        <Toolbar>
          <Select className="w-auto" aria-label={t("filter_status")} value={list.get("status")} onChange={(e) => list.set({ status: e.target.value })}>
            <option value="">{t("filter_all_statuses")}</option>
            {["queued", "running", "done", "failed", "dead", "canceled"].map((s) => (
              <option key={s} value={s}>
                {t(`status_${s}`)}
              </option>
            ))}
          </Select>
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(value) => (
            <>
              <Table columns={columns} rows={value.items} />
              <Pager page={list.page} total={value.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
    </Page>
  );
}

// ---------------------------------------------------------------- reports

function isoDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

export function Reports() {
  const { t, locale } = useSession();
  const [range, setRange] = useState({ start: isoDay(-29), end: isoDay(0) });
  const path = `/api/admin/reports/sales${qs(range)}`;
  const { data, error, reload } = useLoad<SalesReport>(path);
  const presets: [string, number][] = [
    ["range_7", -6],
    ["range_30", -29],
    ["range_90", -89],
  ];

  return (
    <Page
      title={t("nav_reports")}
      actions={
        <a className="inline-flex min-h-10 items-center rounded-xl px-3.5 text-[0.88rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--surface)]" href={`/api/admin/reports/sales.csv${qs(range)}`}>
          {t("download_csv")}
        </a>
      }
    >
      <Section>
        <div className="flex flex-wrap items-end gap-3">
          {presets.map(([key, offset]) => (
            <Button key={key} small tone={range.start === isoDay(offset) && range.end === isoDay(0) ? "primary" : "quiet"} onClick={() => setRange({ start: isoDay(offset), end: isoDay(0) })}>
              {t(key)}
            </Button>
          ))}
          <Field label={t("from")}>{(id) => <Input id={id} type="date" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} />}</Field>
          <Field label={t("to")}>{(id) => <Input id={id} type="date" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} />}</Field>
        </div>
      </Section>
      <Async data={data} error={error} reload={reload}>
        {(report) => (
          <>
            <Section>
              <p className="mb-4 text-[0.95rem]">
                {t("report_totals", { orders: money(report.totals.orders, locale) })} <Money amount={report.totals.amount} className="font-extrabold" />
              </p>
              <Columns
                title={t("report_daily")}
                data={report.days.map((d) => ({
                  key: d.day,
                  label: new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", { day: "numeric", month: "short" }).format(new Date(d.day)),
                  value: d.amount,
                  detail: t("dash_orders_n", { n: money(d.orders, locale) }),
                }))}
                format={(v) => `${money(v, locale)} ${t("currency")}`}
                axisFormat={(v) => compact(v, locale)}
              />
            </Section>
            <div className="grid gap-6 lg:grid-cols-2">
              <Section title={t("report_by_plan")} flush>
                <ReportRows rows={report.plans.map((p, i) => ({ id: i, label: `${p.plan ?? "—"} (${t(`order_kind_${p.kind}`)})`, count: p.orders, amount: p.amount }))} />
              </Section>
              {report.providers && (
                <Section title={t("report_by_method")} flush>
                  <ReportRows rows={report.providers.map((p, i) => ({ id: i, label: t(`provider_${p.provider}`), count: p.payments, amount: p.amount }))} />
                </Section>
              )}
            </div>
          </>
        )}
      </Async>
    </Page>
  );
}

function ReportRows({ rows }: { rows: { id: number; label: string; count: number; amount: number }[] }) {
  const { t, locale } = useSession();
  if (rows.length === 0) return <Empty title={t("empty_list")} />;
  const max = Math.max(...rows.map((r) => r.amount), 1);
  return (
    <ul className="divide-y divide-[var(--line)]">
      {rows.map((row) => (
        <li key={row.id} className="flex flex-col gap-1.5 px-4 py-2.5">
          <span className="flex items-center justify-between gap-3 text-[0.86rem]">
            <span className="font-semibold">{row.label}</span>
            <Money amount={row.amount} />
          </span>
          <span className="flex items-center gap-2">
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--line)]">
              <span className="block h-full rounded-full bg-firouzeh-500" style={{ width: `${(row.amount / max) * 100}%` }} />
            </span>
            <span className="num text-[0.75rem] text-[var(--hint)]">{money(row.count, locale)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------- account

export function Account() {
  const { t, me, toast, errorText } = useSession();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, run] = useAction();
  const change = async () => {
    try {
      await run(() => call("/api/auth/password", { method: "POST", body: { current_password: current, new_password: next } }));
      setCurrent("");
      setNext("");
      toast(t("password_changed"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Page title={t("account")}>
      <Section>
        <p className="font-bold" dir="ltr">
          {me?.username}
        </p>
        <p className="text-[0.85rem] text-[var(--hint)]">{t(`role_${me?.role ?? "admin"}`)}</p>
      </Section>
      <Section title={t("change_password")}>
        <div className="flex max-w-md flex-col gap-4">
          <Field label={t("current_password")}>
            {(id) => <Input id={id} type="password" dir="ltr" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />}
          </Field>
          <Field label={t("new_password")} hint={t("password_hint")}>
            {(id) => <Input id={id} type="password" dir="ltr" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />}
          </Field>
          <p className="text-[0.8rem] text-[var(--hint)]">{t("password_change_signs_out")}</p>
          <Button tone="primary" busy={busy} disabled={!current || next.length < 10} onClick={() => void change()} className="self-start">
            {t("change_password")}
          </Button>
        </div>
      </Section>
    </Page>
  );
}

// ---------------------------------------------------------------- backups

interface BackupData {
  items: { name: string; size: number; created_at: string }[];
  schedule: { hour: number; keep: number; telegram: boolean; passphrase_set: boolean; timezone: string };
}

export function Backups() {
  const { t, locale, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<BackupData>("/api/admin/backups");
  const [busy, run] = useAction();
  const start = async () => {
    try {
      await run(() => call("/api/admin/backups", { method: "POST" }));
      toast(t("backup_started"));
      window.setTimeout(() => void reload(), 8000);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Page
      title={t("nav_backups")}
      actions={
        <Button tone="primary" busy={busy} disabled={!data?.schedule.passphrase_set} onClick={() => void start()}>
          {t("backup_now")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            {!value.schedule.passphrase_set && (
              <p className="rounded-2xl bg-anar-100 px-4 py-3 text-[0.88rem] font-semibold text-anar-700">{t("backup_no_passphrase")}</p>
            )}
            <Section>
              <p className="leading-relaxed">
                {t("backup_schedule", {
                  hour: money(value.schedule.hour, locale),
                  keep: money(value.schedule.keep, locale),
                })}{" "}
                {value.schedule.telegram ? t("backup_schedule_telegram") : ""}
              </p>
              <p className="mt-2 text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("backup_contents_hint")}</p>
            </Section>
            <Section flush title={t("backup_files")}>
              {value.items.length === 0 ? (
                <Empty title={t("backup_none")} />
              ) : (
                <ul className="divide-y divide-[var(--line)]">
                  {value.items.map((item) => (
                    <li key={item.name} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                      <span className="flex flex-col">
                        <span className="font-mono text-[0.82rem]" dir="ltr">
                          {item.name}
                        </span>
                        <span className="text-[0.78rem] text-[var(--hint)]">
                          <When iso={item.created_at} time /> — <Bytes value={item.size} />
                        </span>
                      </span>
                      <a
                        className="inline-flex min-h-8 items-center rounded-lg px-2.5 text-[0.8rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--bg)]"
                        href={`/api/admin/backups/${encodeURIComponent(item.name)}`}
                        download
                      >
                        {t("download")}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          </>
        )}
      </Async>
    </Page>
  );
}
