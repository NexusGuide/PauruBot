import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { call, type AccountPlan, type PanelAccountRow } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Bytes,
  Button,
  Empty,
  Field,
  Input,
  Money,
  NumberInput,
  Page,
  Section,
  Select,
  Sheet,
  Status,
  Textarea,
  Toggle,
  When,
  useConfirm,
} from "../ui";

type PlanDraft = Omit<AccountPlan, "id"> & { id?: number };

const EMPTY: PlanDraft = {
  panel_id: 0,
  title: "",
  description: null,
  role_id: 0,
  data_gb: 0,
  max_users: null,
  days: 30,
  price: 0,
  gb_price: 0,
  user_price: 0,
  sort: 0,
  is_active: true,
};

interface PlansData {
  items: AccountPlan[];
  panels: { id: number; name: string; is_active: boolean }[];
}

function Roles({ panelId, value, onChange }: { panelId: number; value: number; onChange: (v: number) => void }) {
  const { t } = useSession();
  const [roles, setRoles] = useState<{ id: string; title: string }[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => {
    if (!panelId) return;
    let live = true;
    setRoles(null);
    setFailed(null);
    call<{ ok: boolean; error?: string; roles: { id: string; title: string }[] }>(`/api/admin/account-plans/roles/${panelId}`)
      .then((r) => {
        if (!live) return;
        setRoles(r.roles);
        if (!r.ok) setFailed(r.error ?? "");
      })
      .catch(() => live && setRoles([]));
    return () => {
      live = false;
    };
  }, [panelId]);
  return (
    <Field label={t("account_role")} hint={failed !== null ? t("account_roles_failed") : t("account_role_hint")}>
      {(id) =>
        roles && roles.length > 0 ? (
          <Select id={id} value={String(value || "")} onChange={(e) => onChange(Number(e.target.value))}>
            <option value="">—</option>
            {roles.map((r) => (
              <option key={r.id} value={r.id}>
                {r.title} (#{r.id})
              </option>
            ))}
          </Select>
        ) : (
          <NumberInput id={id} value={value || null} onChange={(v) => onChange(v ?? 0)} />
        )
      }
    </Field>
  );
}

function PlanSheet({ draft, panels, onClose, onSaved }: { draft: PlanDraft | null; panels: PlansData["panels"]; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState<PlanDraft>(EMPTY);
  const [busy, run] = useAction();
  useEffect(() => {
    if (draft) setForm({ ...draft, panel_id: draft.panel_id || panels[0]?.id || 0 });
  }, [draft, panels]);
  const set = (patch: Partial<PlanDraft>) => setForm((f) => ({ ...f, ...patch }));
  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() => call(id ? `/api/admin/account-plans/${id}` : "/api/admin/account-plans", { method: id ? "PUT" : "POST", body }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open={draft !== null}
      onClose={onClose}
      title={form.id ? form.title : t("new_account_plan")}
      footer={
        <Button tone="primary" busy={busy} disabled={!form.title.trim() || !form.panel_id || !form.role_id} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("title")}>{(id) => <Input id={id} maxLength={96} value={form.title} onChange={(e) => set({ title: e.target.value })} />}</Field>
        <Field label={t("plan_description")}>
          {(id) => <Textarea id={id} maxLength={2000} value={form.description ?? ""} onChange={(e) => set({ description: e.target.value || null })} />}
        </Field>
        <Field label={t("panel")}>
          {(id) => (
            <Select id={id} value={String(form.panel_id)} onChange={(e) => set({ panel_id: Number(e.target.value), role_id: 0 })}>
              {panels.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Roles panelId={form.panel_id} value={form.role_id} onChange={(role_id) => set({ role_id })} />
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("account_data_gb")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.data_gb} onChange={(v) => set({ data_gb: v ?? 0 })} />}
          </Field>
          <Field label={t("account_max_users")} hint={t("account_max_users_hint")}>
            {(id) => <NumberInput id={id} allowEmpty value={form.max_users} onChange={(v) => set({ max_users: v || null })} />}
          </Field>
          <Field label={t("days")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.days} onChange={(v) => set({ days: v ?? 0 })} />}
          </Field>
          <Field label={t("price_toman")}>{(id) => <NumberInput id={id} value={form.price} onChange={(v) => set({ price: v ?? 0 })} />}</Field>
          <Field label={t("account_gb_price")} hint={t("zero_not_sold")}>
            {(id) => <NumberInput id={id} value={form.gb_price} onChange={(v) => set({ gb_price: v ?? 0 })} />}
          </Field>
          <Field label={t("account_user_price")} hint={t("zero_not_sold")}>
            {(id) => <NumberInput id={id} value={form.user_price} onChange={(v) => set({ user_price: v ?? 0 })} />}
          </Field>
          <Field label={t("sort_order")}>{(id) => <NumberInput id={id} value={form.sort} onChange={(v) => set({ sort: v ?? 0 })} />}</Field>
        </div>
        <Toggle checked={form.is_active} onChange={(is_active) => set({ is_active })} label={t("active")} />
      </div>
    </Sheet>
  );
}

function Plans() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<PlansData>("/api/admin/account-plans");
  const [draft, setDraft] = useState<PlanDraft | null>(null);
  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Section
          title={t("account_plans")}
          flush
          actions={
            value.panels.length > 0 && (
              <Button small tone="primary" icon="plus" onClick={() => setDraft({ ...EMPTY })}>
                {t("new_account_plan")}
              </Button>
            )
          }
        >
          {value.panels.length === 0 ? (
            <Empty title={t("account_needs_pasarguard")} hint={t("account_needs_pasarguard_hint")} action={<Link className="underline" to="/panels">{t("nav_panels")}</Link>} />
          ) : value.items.length === 0 ? (
            <Empty title={t("account_plans_empty")} />
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {value.items.map((plan) => (
                <li key={plan.id}>
                  <button type="button" className="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-start" onClick={() => setDraft(plan)}>
                    <span className={plan.is_active ? "font-bold" : "font-bold text-[var(--hint)] line-through"}>{plan.title}</span>
                    <span className="text-[0.85rem] text-[var(--hint)]">
                      {plan.data_gb ? `${plan.data_gb.toLocaleString(locale)} GB` : "∞"} · {plan.max_users ?? "∞"} 👥 · {plan.days || "∞"}d · <Money amount={plan.price} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <PlanSheet draft={draft} panels={value.panels} onClose={() => setDraft(null)} onSaved={() => { setDraft(null); void reload(); }} />
        </Section>
      )}
    </Async>
  );
}

function Accounts() {
  const { t, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [q, setQ] = useState("");
  const { data, error, reload } = useLoad<{ items: PanelAccountRow[] }>(`/api/admin/panel-accounts?q=${encodeURIComponent(q)}`);
  const [busy, run] = useAction();
  const act = async (row: PanelAccountRow, action: "pause" | "resume" | "refresh" | "delete") => {
    if (action === "delete") {
      const ok = await confirm({ title: t("account_delete_title"), body: t("account_delete_body", { username: row.username }), confirm: t("delete"), danger: true });
      if (!ok) return;
    }
    try {
      await run(() => call(`/api/admin/panel-accounts/${row.id}/${action}`, { method: "POST" }));
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("account_sold")} flush>
      <div className="px-4 py-3">
        <Input placeholder={t("account_search")} value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <Async data={data} error={error} reload={reload}>
        {(value) =>
          value.items.length === 0 ? (
            <Empty title={t("account_sold_empty")} />
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {value.items.map((row) => (
                <li key={row.id} className="flex flex-col gap-2 px-4 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="num font-bold">{row.username}</span>
                    <Status value={row.status} />
                  </div>
                  <p className="text-[0.85rem]">
                    <Link className="underline" to={`/customers/${row.customer_id}`}>
                      {row.customer ?? row.telegram_id}
                    </Link>{" "}
                    · {row.plan ?? "—"} · {row.panel ?? "—"}
                  </p>
                  <p className="text-[0.8rem] text-[var(--hint)]">
                    <Bytes value={row.used_bytes} /> / {row.data_limit ? <Bytes value={row.data_limit} /> : "∞"} · {row.users_count}/{row.max_users ?? "∞"} 👥 ·{" "}
                    {row.expire_at ? <When iso={row.expire_at} /> : "∞"}
                  </p>
                  {row.status !== "deleted" && (
                    <div className="flex flex-wrap gap-2">
                      <Button small busy={busy} onClick={() => void act(row, "refresh")}>
                        {t("refresh")}
                      </Button>
                      {row.status === "active" && (
                        <Button small busy={busy} onClick={() => void act(row, "pause")}>
                          {t("account_pause")}
                        </Button>
                      )}
                      {row.status === "paused" && (
                        <Button small busy={busy} onClick={() => void act(row, "resume")}>
                          {t("account_resume")}
                        </Button>
                      )}
                      <Button small tone="ghost" busy={busy} onClick={() => void act(row, "delete")}>
                        {t("delete")}
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )
        }
      </Async>
    </Section>
  );
}

export function AdminAccounts() {
  const { t } = useSession();
  return (
    <Page title={t("nav_admin_accounts")}>
      <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("account_intro")}</p>
      <Plans />
      <Accounts />
    </Page>
  );
}
