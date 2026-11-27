import { useState } from "react";
import { ResellerHub, StaffHubButton } from "../reseller-hub/ResellerHub";
import { TicketDesk, StaffTicketPage } from "../tickets/TicketDesk";
import { Link, useNavigate, useParams } from "react-router-dom";
import { money } from "@shared/format";
import {
  call,
  type BillingView,
  type CatalogData,
  type PanelRow,
  type ResellerPrice,
  type ResellerData,
  type ResellerGroup,
  type ResellerProfile,
  type StaffRow,
  type SubReseller,
  type Ticket,
  type TicketMessage,
} from "../api";
import { PAGE_SIZE, useAction, useList, useLoad } from "../hooks";
import { useSession } from "../session";
import { BotLogsSheet } from "./BotLogsSheet";
import { BotFigures, WebhookHealth, type ResellerBot } from "./MyBot";
import {
  Async,
  Badge,
  Bytes,
  Button,
  Checkbox,
  Empty,
  Facts,
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
  Toolbar,
  When,
  type Column,
} from "../ui";
import { ResellerRanking } from "./Tools";

// ---------------------------------------------------------------- resellers

type GroupDraft = Omit<ResellerGroup, "id"> & { id?: number };

function GroupSheet({ draft, onClose, onSaved }: { draft: GroupDraft; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState(draft);
  const [busy, run] = useAction();
  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() => call(id ? `/api/admin/reseller-groups/${id}` : "/api/admin/reseller-groups", { method: id ? "PUT" : "POST", body }));
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
      title={form.id ? t("edit_group") : t("new_group")}
      footer={
        <Button tone="primary" busy={busy} disabled={!form.title.trim()} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("title")}>
          {(id) => <Input id={id} value={form.title} maxLength={64} onChange={(e) => setForm({ ...form, title: e.target.value })} />}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("discount_percent")}>
            {(id) => (
              <NumberInput id={id} value={form.discount_percent} onChange={(v) => setForm({ ...form, discount_percent: Math.min(100, v ?? 0) })} />
            )}
          </Field>
          <Field label={t("min_topup")}>
            {(id) => <NumberInput id={id} value={form.min_topup} onChange={(v) => setForm({ ...form, min_topup: v ?? 0 })} />}
          </Field>
        </div>
        <Field label={t("max_active_services")} hint={t("empty_no_limit")}>
          {(id) => (
            <NumberInput id={id} value={form.max_active_services} onChange={(v) => setForm({ ...form, max_active_services: v || null })} allowEmpty />
          )}
        </Field>
        <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} />
        <HiddenPanels value={form.hidden_panel_ids ?? []} onChange={(ids) => setForm({ ...form, hidden_panel_ids: ids })} />
        {form.id ? <PlanPrices owner={{ group_id: form.id }} /> : <p className="text-[0.78rem] text-[var(--hint)]">{t("reseller_prices_after_save")}</p>}
      </div>
    </Sheet>
  );
}

// Panels whose plans this group's resellers never see (TEN-12).
export function HiddenPanels({
  value,
  onChange,
  hint = "hidden_panels_hint",
}: {
  value: number[];
  onChange: (ids: number[]) => void;
  hint?: string;
}) {
  const { t } = useSession();
  const { data } = useLoad<{ items: PanelRow[] }>("/api/admin/panels");
  if (!data?.items.length) return null;
  const chosen = new Set(value);
  return (
    <fieldset className="flex flex-col gap-2 border-t border-[var(--line)] pt-4">
      <legend className="mb-1 text-[0.88rem] font-bold">{t("hidden_panels")}</legend>
      <p className="text-[0.78rem] text-[var(--hint)]">{t(hint)}</p>
      {data.items.map((panel) => (
        <label key={panel.id} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox
            label={panel.name}
            checked={chosen.has(panel.id)}
            onChange={(on) => onChange(on ? [...value, panel.id] : value.filter((id) => id !== panel.id))}
          />
          <span dir="auto">{panel.name}</span>
        </label>
      ))}
    </fieldset>
  );
}

// Fixed reseller prices per plan for one group or one reseller (TEN-12); empty means the discount applies.
export function PlanPrices({ owner }: { owner: { group_id: number } | { profile_id: number } }) {
  const { t, locale, toast, errorText } = useSession();
  const key = "group_id" in owner ? `group_id=${owner.group_id}` : `profile_id=${owner.profile_id}`;
  const catalog = useLoad<CatalogData>("/api/admin/catalog");
  const prices = useLoad<{ items: ResellerPrice[] }>(`/api/admin/reseller-prices?${key}`);
  const [draft, setDraft] = useState<Record<number, number | null>>({});
  const [busy, run] = useAction();
  const current = new Map((prices.data?.items ?? []).map((row) => [row.plan_id, row.price]));
  const save = async () => {
    try {
      for (const [planId, price] of Object.entries(draft)) {
        await run(() => call("/api/admin/reseller-prices", { method: "PUT", body: { plan_id: Number(planId), ...owner, price: price || null } }));
      }
      setDraft({});
      await prices.reload();
      toast(t("saved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const plans = catalog.data?.plans ?? [];
  if (!plans.length) return null;
  return (
    <fieldset className="flex flex-col gap-2 border-t border-[var(--line)] pt-4">
      <legend className="mb-1 text-[0.88rem] font-bold">{t("reseller_prices")}</legend>
      <p className="text-[0.78rem] text-[var(--hint)]">{t("reseller_prices_hint")}</p>
      {plans.map((plan) => (
        <div key={plan.id} className="grid grid-cols-[1fr_9rem] items-center gap-3">
          <span className="text-[0.88rem]" dir="auto">
            {plan.title} <span className="text-[var(--hint)]">({money(plan.price, locale)})</span>
          </span>
          <NumberInput
            value={plan.id in draft ? (draft[plan.id] ?? null) : (current.get(plan.id) ?? null)}
            onChange={(v) => setDraft({ ...draft, [plan.id]: v || null })}
            allowEmpty
            aria-label={plan.title}
          />
        </div>
      ))}
      <div>
        <Button small tone="primary" busy={busy} disabled={!Object.keys(draft).length} onClick={() => void save()}>
          {t("save_prices")}
        </Button>
      </div>
    </fieldset>
  );
}

const MODES = ["prepaid", "usage", "hourly"] as const;

function ProfileSheet({
  profile,
  data,
  onClose,
  onSaved,
}: {
  profile: ResellerProfile;
  data: ResellerData;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, locale, toast, errorText } = useSession();
  const [form, setForm] = useState(profile);
  const [busy, run] = useAction();
  const set = <K extends keyof ResellerProfile>(key: K, value: ResellerProfile[K]) => setForm((f) => ({ ...f, [key]: value }));
  const parents = data.profiles.filter((p) => p.parent_id === null && p.id !== profile.id);
  const parent = parents.find((p) => p.id === form.parent_id);
  const save = async () => {
    try {
      await run(() =>
        call("/api/admin/resellers", {
          method: "PUT",
          body: {
            customer_id: form.customer_id,
            group_id: form.group_id,
            permissions: form.permissions,
            is_active: form.is_active,
            expires_at: form.expires_at,
            max_purchases_per_day: form.max_purchases_per_day,
            parent_id: form.parent_id,
            discount_override: form.discount_override,
            max_subresellers: form.max_subresellers,
            billing_mode: form.billing_mode,
            gb_price: form.gb_price,
            hourly_price: form.hourly_price,
            credit_limit: form.credit_limit,
            low_balance_threshold: form.low_balance_threshold,
            max_active_services: form.max_active_services,
            max_total_gb: form.max_total_gb,
          },
        }),
      );
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const suspend = async (suspended: boolean) => {
    try {
      await run(() => call(`/api/admin/resellers/${profile.id}/suspend`, { method: "POST", body: { suspended } }));
      toast(suspended ? t("reseller_suspended_toast") : t("reseller_resumed_toast"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const toggle = (perm: string, on: boolean) =>
    setForm({ ...form, permissions: on ? [...form.permissions, perm] : form.permissions.filter((p) => p !== perm) });
  const optional = (key: keyof ResellerProfile, label: string, hint?: string) => (
    <Field label={label} hint={hint ?? t("empty_no_limit")}>
      {(id) => (
        <NumberInput
          id={id}
          allowEmpty
          value={(form[key] as number | null) ?? null}
          onChange={(v) => setForm((f) => ({ ...f, [key]: v ?? null }))}
        />
      )}
    </Field>
  );
  return (
    <Sheet
      open
      onClose={onClose}
      title={form.customer}
      wide
      footer={
        <div className="flex flex-wrap gap-2">
          <Button tone="primary" busy={busy} onClick={() => void save()}>
            {t("save")}
          </Button>
          {profile.suspended_at ? (
            <Button tone="approve" busy={busy} onClick={() => void suspend(false)}>
              {t("resume_reseller")}
            </Button>
          ) : (
            <Button tone="danger" busy={busy} onClick={() => void suspend(true)}>
              {t("suspend_reseller")}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-5">
        <Facts
          items={[
            [t("balance"), <Money key="b" amount={profile.balance} />],
            [t("reseller_debt"), <Money key="d" amount={profile.billing_debt} />],
            [t("discount"), t("discount_value", { percent: money(profile.discount_percent, locale) })],
            [t("col_status"), profile.suspended_at ? t(`suspend_reason_${profile.suspend_reason ?? "staff"}`) : profile.is_current ? t("active") : t("inactive")],
          ]}
        />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("reseller_group")}>
            {(id) => (
              <Select id={id} value={form.group_id ?? ""} onChange={(e) => set("group_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">—</option>
                {data.groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.title}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {optional("discount_override", t("discount_override"), t("discount_override_hint"))}
          <Field label={t("parent_reseller")} hint={t("parent_reseller_hint")}>
            {(id) => (
              <Select id={id} value={form.parent_id ?? ""} onChange={(e) => set("parent_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">{t("level_one")}</option>
                {parents.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.customer}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {form.parent_id === null && optional("max_subresellers", t("max_subresellers"))}
          {parent && <p className="text-[0.78rem] text-[var(--hint)] sm:col-span-2">{t("parent_discount_cap", { percent: parent.discount_percent })}</p>}
          <Field label={t("reseller_expires")} hint={t("reseller_expires_hint")}>
            {(id) => (
              <Input
                id={id}
                type="date"
                value={form.expires_at ? form.expires_at.slice(0, 10) : ""}
                onChange={(e) => set("expires_at", e.target.value ? new Date(`${e.target.value}T23:59:59`).toISOString() : null)}
              />
            )}
          </Field>
        </div>

        <fieldset className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
          <legend className="text-[0.88rem] font-bold">{t("reseller_billing")}</legend>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label={t("billing_mode")} hint={t(`billing_mode_${form.billing_mode}_hint`)}>
              {(id) => (
                <Select id={id} value={form.billing_mode} onChange={(e) => set("billing_mode", e.target.value as ResellerProfile["billing_mode"])}>
                  {MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {t(`billing_mode_${mode}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {form.billing_mode === "usage" && (
              <Field label={t("gb_price")}>
                {(id) => <NumberInput id={id} value={form.gb_price} onChange={(v) => set("gb_price", v ?? 0)} />}
              </Field>
            )}
            {form.billing_mode === "hourly" && (
              <Field label={t("hourly_price")}>
                {(id) => <NumberInput id={id} value={form.hourly_price} onChange={(v) => set("hourly_price", v ?? 0)} />}
              </Field>
            )}
            {form.billing_mode !== "prepaid" && (
              <Field label={t("credit_limit")} hint={t("credit_limit_hint")}>
                {(id) => <NumberInput id={id} value={form.credit_limit} onChange={(v) => set("credit_limit", v ?? 0)} />}
              </Field>
            )}
            {optional("low_balance_threshold", t("low_balance_threshold"), t("low_balance_threshold_hint"))}
          </div>
        </fieldset>

        <fieldset className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
          <legend className="text-[0.88rem] font-bold">{t("reseller_limits")}</legend>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            {optional("max_purchases_per_day", t("max_purchases_per_day"))}
            {optional("max_active_services", t("max_active_services"))}
            {optional("max_total_gb", t("max_total_gb"))}
          </div>
        </fieldset>

        <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("active")} />
        <fieldset className="flex flex-col gap-2 border-t border-[var(--line)] pt-4">
          <legend className="mb-1 text-[0.88rem] font-bold">{t("reseller_permissions")}</legend>
          <p className="text-[0.78rem] text-[var(--hint)]">{t("reseller_permissions_hint")}</p>
          {data.permissions.map((perm) => (
            <label key={perm} className="flex items-center gap-2 text-[0.88rem]">
              <Checkbox label={perm} checked={form.permissions.includes(perm)} onChange={(on) => toggle(perm, on)} />
              {t(`perm_${perm.replace(".", "_")}`)}
            </label>
          ))}
        </fieldset>
        {profile.id && <PlanPrices owner={{ profile_id: profile.id }} />}
        {(profile.billing_mode !== "prepaid" || profile.billing_debt > 0) && (
          <BillingHistory path={`/api/admin/resellers/${profile.id}/billing`} canBillNow onChanged={onSaved} />
        )}
        {profile.id && <ResellerBotInfo profileId={profile.id} />}
      </div>
    </Sheet>
  );
}

export function BillingHistory({
  path,
  canBillNow = false,
  onChanged,
}: {
  path: string;
  canBillNow?: boolean;
  onChanged?: () => void;
}) {
  const { t, toast, errorText } = useSession();
  const state = useLoad<BillingView>(path);
  const [busy, run] = useAction();

  const billNow = async () => {
    try {
      const res = await run(() =>
        call<BillingView & { billed?: boolean; amount?: number; charged?: number }>(`${path}/bill-now`, {
          method: "POST",
        }),
      );
      toast(res?.billed ? t("billing_run_done") : t("billing_nothing_due"));
      void state.reload();
      onChanged?.();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const settle = async () => {
    try {
      const res = await run(() =>
        call<BillingView & { charged?: number; resumed?: boolean }>(`${path}/settle`, { method: "POST" }),
      );
      if ((res?.charged ?? 0) > 0) {
        toast(res?.resumed ? t("debt_settled_resumed") : t("debt_settled"));
      } else {
        toast(t("debt_settle_none"));
      }
      void state.reload();
      onChanged?.();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async {...state}>
      {(view) => (
        <div className="flex flex-col gap-2 border-t border-[var(--line)] pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-[0.88rem] font-bold">{t("billing_history")}</h3>
            <div className="flex flex-wrap gap-2">
              {canBillNow && view.billing_mode !== "prepaid" && (
                <Button small tone="quiet" busy={busy} onClick={() => void billNow()}>
                  {t("billing_bill_now")}
                </Button>
              )}
              {view.debt > 0 && (
                <Button small tone="primary" busy={busy} onClick={() => void settle()}>
                  {t("billing_settle_debt")}
                </Button>
              )}
              <a
                href={`${path}.csv`}
                download
                className="inline-flex min-h-9 items-center rounded-xl bg-[var(--bg)] px-3 text-[0.82rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--line)]"
              >
                {t("billing_export_csv")}
              </a>
            </div>
          </div>
          <Table
            rows={view.items}
            empty={<Empty title={t("no_billing_yet")} />}
            columns={[
              { key: "end", label: t("billing_period"), render: (r) => <When iso={r.period_end} time /> },
              { key: "used", label: t("col_usage"), render: (r) => <Bytes value={r.used_bytes} /> },
              { key: "hours", label: t("service_hours"), render: (r) => r.service_hours },
              { key: "amount", label: t("amount"), render: (r) => <Money amount={r.amount} /> },
              { key: "charged", label: t("billing_charged"), mobile: "hide", render: (r) => <Money amount={r.charged} /> },
              { key: "debt", label: t("reseller_debt"), mobile: "end", render: (r) => <Money amount={r.debt_after} /> },
            ]}
          />
        </div>
      )}
    </Async>
  );
}

export function Resellers() {
  const { t, locale, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<ResellerData>("/api/admin/resellers");
  const [group, setGroup] = useState<GroupDraft | null>(null);
  const [profile, setProfile] = useState<ResellerProfile | null>(null);
  const [decision, setDecision] = useState<{ id: number; group_id: number | null } | null>(null);
  const [busy, run] = useAction();

  const decide = async (id: number, approve: boolean, groupId: number | null) => {
    try {
      await run(() => call(`/api/admin/reseller-applications/${id}`, { method: "POST", body: { approve, group_id: groupId } }));
      toast(approve ? t("reseller_approved") : t("reseller_rejected"));
      setDecision(null);
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Page
          title={t("nav_resellers")}
          actions={
            <Button
              tone="primary"
              icon="plus"
              onClick={() => setGroup({ title: "", discount_percent: 10, min_topup: 0, max_active_services: null, is_active: true, hidden_panel_ids: [] })}
            >
              {t("new_group")}
            </Button>
          }
        >
          {value.applications.length > 0 && (
            <Section title={t("reseller_applications")} flush>
              <ul className="divide-y divide-[var(--line)]">
                {value.applications.map((app) => (
                  <li key={app.id} className="flex flex-col gap-2 border-s-4 border-s-saffron-500 px-4 py-3 sm:flex-row sm:items-center">
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <Link className="font-semibold text-firouzeh-700" to={`/customers/${app.customer_id}`}>
                        <bdi>{app.customer}</bdi>
                      </Link>
                      {app.text && <p className="text-[0.85rem] leading-relaxed" dir="auto">{app.text}</p>}
                      <span className="text-[0.75rem] text-[var(--hint)]">
                        <When iso={app.created_at} time />
                      </span>
                    </div>
                    <div className="flex gap-2">
                      <Button small tone="danger" busy={busy} onClick={() => void decide(app.id, false, null)}>
                        {t("reject")}
                      </Button>
                      <Button small tone="approve" onClick={() => setDecision({ id: app.id, group_id: value.groups[0]?.id ?? null })}>
                        {t("approve")}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title={t("reseller_groups")} flush>
            {value.groups.length === 0 ? (
              <Empty title={t("no_groups")} hint={t("no_groups_hint")} />
            ) : (
              <ul className="divide-y divide-[var(--line)]">
                {value.groups.map((g) => (
                  <li key={g.id}>
                    <button
                      type="button"
                      onClick={() => setGroup({ ...g })}
                      className="flex w-full items-center justify-between gap-3 px-4 py-3 text-start hover:bg-[var(--bg)]"
                    >
                      <span className="flex items-center gap-2 font-semibold">
                        {g.title}
                        {!g.is_active && <Badge>{t("inactive")}</Badge>}
                      </span>
                      <span className="text-[0.85rem] text-[var(--hint)]">
                        {t("group_summary", { percent: money(g.discount_percent, locale), min: money(g.min_topup, locale) })}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title={t("resellers")} flush>
            {value.profiles.length === 0 ? (
              <Empty title={t("no_resellers")} hint={t("no_resellers_hint")} />
            ) : (
              <ul className="divide-y divide-[var(--line)]">
                {value.profiles.map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => setProfile(p)}
                      className="flex w-full items-center justify-between gap-3 px-4 py-3 text-start hover:bg-[var(--bg)]"
                    >
                      <span className="flex flex-wrap items-center gap-2 font-semibold">
                        <bdi>{p.customer}</bdi>
                        {!p.is_current && <Badge tone="loss">{t("inactive")}</Badge>}
                        {p.suspended_at && <Badge tone="loss">{t("suspended")}</Badge>}
                        {p.billing_mode !== "prepaid" && <Badge>{t(`billing_mode_${p.billing_mode}`)}</Badge>}
                        {p.parent_id !== null && <Badge>{t("level_two")}</Badge>}
                      </span>
                      <span className="flex flex-col items-end text-[0.85rem] text-[var(--hint)]">
                        <span>{value.groups.find((g) => g.id === p.group_id)?.title ?? "—"}</span>
                        <span>{t("discount_value", { percent: money(p.discount_percent, locale) })}</span>
                        <Money amount={p.balance} />
                      </span>
                    </button>
                    <StaffHubButton profileId={p.id} name={p.customer} />
                  </li>
                ))}
              </ul>
            )}
          </Section>

          {group && (
            <GroupSheet
              draft={group}
              onClose={() => setGroup(null)}
              onSaved={() => {
                setGroup(null);
                void reload();
              }}
            />
          )}
          {profile && (
            <ProfileSheet
              profile={profile}
              data={value}
              onClose={() => setProfile(null)}
              onSaved={() => {
                setProfile(null);
                void reload();
              }}
            />
          )}
          <Sheet
            open={decision !== null}
            onClose={() => setDecision(null)}
            title={t("approve_reseller")}
            footer={
              <Button
                tone="approve"
                busy={busy}
                disabled={!decision?.group_id}
                onClick={() => decision && void decide(decision.id, true, decision.group_id)}
              >
                {t("approve")}
              </Button>
            }
          >
            {value.groups.length === 0 ? (
              <p className="text-[var(--hint)]">{t("no_groups_hint")}</p>
            ) : (
              <Field label={t("reseller_group")}>
                {(id) => (
                  <Select
                    id={id}
                    value={decision?.group_id ?? ""}
                    onChange={(e) => decision && setDecision({ ...decision, group_id: Number(e.target.value) })}
                  >
                    {value.groups.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.title}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            )}
          </Sheet>
          <ResellerRanking />
        </Page>
      )}
    </Async>
  );
}

// ---------------------------------------------------------------- staff

interface StaffData {
  items: StaffRow[];
  roles: string[];
  matrix: Record<string, string[]>;
}

function StaffSheet({
  row,
  roles,
  onClose,
  onSaved,
}: {
  row: StaffRow | null;
  roles: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, me, toast, errorText } = useSession();
  const [form, setForm] = useState({
    role: row?.role ?? "support",
    username: row?.username ?? "",
    password: "",
    telegram_id: row?.telegram_id ? String(row.telegram_id) : "",
    display_name: row?.display_name ?? "",
    is_active: row?.is_active ?? true,
    reseller_profile_id: row?.reseller_profile_id ?? null,
  });
  const [busy, run] = useAction();
  const isSelf = row?.id === me?.id;
  const isOwner = row?.role === "owner";

  const save = async () => {
    const telegram = form.telegram_id.trim() ? Number(form.telegram_id.trim()) : null;
    try {
      if (row) {
        const body: Record<string, unknown> = { display_name: form.display_name || null };
        if (!isSelf && !isOwner) {
          body.role = form.role;
          body.is_active = form.is_active;
        }
        if (form.password) body.password = form.password;
        if (telegram) body.telegram_id = telegram;
        await run(() => call(`/api/admin/staff/${row.id}`, { method: "PATCH", body }));
      } else {
        await run(() =>
          call("/api/admin/staff", {
            method: "POST",
            body: {
              role: form.role,
              username: form.username.trim(),
              password: form.password || null,
              telegram_id: telegram,
              display_name: form.display_name || null,
              reseller_profile_id: form.reseller_profile_id,
            },
          }),
        );
      }
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
      title={row ? row.username : t("new_staff")}
      footer={
        <Button
          tone="primary"
          busy={busy}
          disabled={!row && (!/^[A-Za-z0-9_.-]{3,64}$/.test(form.username) || (!form.password && !form.telegram_id))}
          onClick={() => void save()}
        >
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        {!row && (
          <Field label={t("username")}>
            {(id) => <Input id={id} dir="ltr" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />}
          </Field>
        )}
        <Field label={t("display_name")}>
          {(id) => <Input id={id} value={form.display_name} maxLength={96} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />}
        </Field>
        <Field label={t("role")} hint={isOwner ? t("owner_fixed") : isSelf ? t("own_role_fixed") : t(`role_${form.role}_hint`)}>
          {(id) => (
            <Select id={id} value={form.role} disabled={isSelf || isOwner} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              {roles
                .filter((role) => role !== "owner" || isOwner)
                .map((role) => (
                  <option key={role} value={role}>
                    {t(`role_${role}`)}
                  </option>
                ))}
            </Select>
          )}
        </Field>
        {form.role === "reseller" && !row && (
          <Field label={t("reseller_profile_id")} hint={t("reseller_profile_id_hint")}>
            {(id) => (
              <NumberInput
                id={id}
                value={form.reseller_profile_id}
                onChange={(v) => setForm({ ...form, reseller_profile_id: v })}
                allowEmpty
              />
            )}
          </Field>
        )}
        <Field label={row ? t("new_password") : t("password")} hint={t("password_hint")}>
          {(id) => (
            <Input
              id={id}
              type="password"
              dir="ltr"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          )}
        </Field>
        <Field label={t("telegram_id")} hint={t("staff_telegram_hint")}>
          {(id) => (
            <Input id={id} dir="ltr" inputMode="numeric" value={form.telegram_id} onChange={(e) => setForm({ ...form, telegram_id: e.target.value })} />
          )}
        </Field>
        {row && !isSelf && !isOwner && (
          <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} hint={t("deactivate_hint")} />
        )}
      </div>
    </Sheet>
  );
}

export function Staff() {
  const { t } = useSession();
  const { data, error, reload } = useLoad<StaffData>("/api/admin/staff");
  const [editing, setEditing] = useState<StaffRow | "new" | null>(null);
  const columns: Column<StaffRow>[] = [
    {
      key: "name",
      label: t("username"),
      mobile: "title",
      render: (s) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-semibold" dir="ltr">
            {s.username}
          </span>
          {s.display_name && <span className="text-[var(--hint)]">{s.display_name}</span>}
          {!s.is_active && <Badge tone="loss">{t("inactive")}</Badge>}
        </span>
      ),
    },
    { key: "role", label: t("role"), mobile: "end", render: (s) => <Badge tone="brand">{t(`role_${s.role}`)}</Badge> },
    { key: "login", label: t("last_login"), mobile: "meta", render: (s) => <When iso={s.last_login_at} time /> },
  ];
  return (
    <Page
      title={t("nav_staff")}
      actions={
        <Button tone="primary" icon="plus" onClick={() => setEditing("new")}>
          {t("new_staff")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <Section flush>
              <Table columns={columns} rows={value.items} onRow={setEditing} />
            </Section>
            <Section title={t("role_matrix")}>
              <div className="overflow-x-auto">
                <table className="w-full text-[0.8rem]">
                  <thead>
                    <tr className="text-[var(--hint)]">
                      <th className="py-1.5 text-start font-semibold">{t("permission")}</th>
                      {value.roles.map((role) => (
                        <th key={role} className="px-2 py-1.5 text-center font-semibold">
                          {t(`role_${role}`)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(value.matrix.owner ?? []).map((perm) => (
                      <tr key={perm} className="border-t border-[var(--line)]">
                        <td className="py-1.5">{t(`perm_${perm.replace(".", "_")}`)}</td>
                        {value.roles.map((role) => (
                          <td key={role} className="px-2 py-1.5 text-center">
                            {value.matrix[role]?.includes(perm) ? (
                              <span className="text-pista-700" aria-label={t("yes")}>
                                ✓
                              </span>
                            ) : (
                              <span className="text-[var(--hint)]" aria-label={t("no")}>
                                –
                              </span>
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
            {editing && (
              <StaffSheet
                row={editing === "new" ? null : editing}
                roles={value.roles}
                onClose={() => setEditing(null)}
                onSaved={() => {
                  setEditing(null);
                  void reload();
                }}
              />
            )}
          </>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- tickets

function NewTicketSheet({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const { t, toast, errorText } = useSession();
  const [subject, setSubject] = useState("");
  const [text, setText] = useState("");
  const [busy, run] = useAction();
  const send = async () => {
    try {
      const result = await run(() => call<{ id: number }>("/api/admin/tickets", { method: "POST", body: { subject, text } }));
      toast(t("ticket_sent"));
      if (result) onCreated(result.id);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={t("new_ticket_to_shop")}
      footer={
        <Button tone="primary" busy={busy} disabled={!subject.trim() || !text.trim()} onClick={() => void send()}>
          {t("send")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("ticket_subject")}>
          {(id) => <Input id={id} maxLength={128} value={subject} onChange={(e) => setSubject(e.target.value)} />}
        </Field>
        <Field label={t("ticket_message")}>
          {(id) => <Textarea id={id} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} />}
        </Field>
      </div>
    </Sheet>
  );
}

function ResellerTickets() {
  const { t, me, can } = useSession();
  const navigate = useNavigate();
  const [composing, setComposing] = useState(false);
  const list = useList<Ticket>("/api/admin/tickets", ["status"]);
  const reseller = me?.role === "reseller";
  const columns: Column<Ticket>[] = [
    {
      key: "subject",
      label: t("ticket_subject"),
      mobile: "title",
      render: (x) => (
        <span className="flex items-center gap-2">
          <span className="font-semibold" dir="auto">{x.subject}</span>
          {x.source === "panel" && <Badge>{t("ticket_from_panel")}</Badge>}
        </span>
      ),
    },
    { key: "customer", label: t("col_customer"), mobile: "meta", render: (x) => x.customer ?? "—" },
    { key: "status", label: t("col_status"), mobile: "end", render: (x) => <Status value={x.status} /> },
    { key: "updated", label: t("updated_at"), mobile: "meta", render: (x) => <When iso={x.updated_at} time /> },
  ];
  return (
    <Page
      title={t("nav_tickets")}
      actions={
        reseller && can("tickets.reply") ? (
          <Button tone="primary" icon="plus" onClick={() => setComposing(true)}>
            {t("new_ticket_to_shop")}
          </Button>
        ) : null
      }
    >
      {composing && <NewTicketSheet onClose={() => setComposing(false)} onCreated={(id) => navigate(`/tickets/${id}`)} />}
      <Section flush>
        <Toolbar>
          <Select className="w-auto" aria-label={t("filter_status")} value={list.get("status")} onChange={(e) => list.set({ status: e.target.value })}>
            <option value="">{t("filter_all_statuses")}</option>
            {["open", "answered", "closed"].map((s) => (
              <option key={s} value={s}>
                {t(`status_${s}`)}
              </option>
            ))}
          </Select>
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} onRow={(x) => navigate(`/tickets/${x.id}`)} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
    </Page>
  );
}

function ResellerTicketPage() {
  const { id } = useParams();
  const { t, can, me, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ ticket: Ticket; customer: string | null; messages: TicketMessage[] }>(
    `/api/admin/tickets/${id}`,
  );
  const [reply, setReply] = useState("");
  const [busy, run] = useAction();

  const send = async () => {
    try {
      await run(() => call(`/api/admin/tickets/${id}/reply`, { method: "POST", body: { text: reply.trim() } }));
      setReply("");
      toast(t("reply_sent"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const close = async () => {
    try {
      await run(() => call(`/api/admin/tickets/${id}/close`, { method: "POST" }));
      toast(t("ticket_closed"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Page
          title={value.ticket.subject}
          actions={
            can("tickets.reply") && value.ticket.status !== "closed" ? (
              <Button busy={busy} onClick={() => void close()}>
                {t("close_ticket")}
              </Button>
            ) : null
          }
        >
          <p className="-mt-3 flex flex-wrap items-center gap-2 text-[0.85rem] text-[var(--hint)]">
            <Status value={value.ticket.status} />
            <Link className="font-semibold text-firouzeh-700" to={`/customers/${value.ticket.customer_id}`}>
              <bdi>{value.customer}</bdi>
            </Link>
          </p>
          <ol className="flex flex-col gap-3">
            {value.messages.map((m) => {
              const mine = me?.role === "reseller" ? m.author_type === "customer" : m.author_type === "staff";
              return (
                <li key={m.id} className={`flex ${mine ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[85%] rounded-2xl px-4 py-2.5 ${
                      mine ? "bg-firouzeh-600 text-white" : "bg-[var(--surface)] ring-1 ring-[var(--line)]"
                    }`}
                  >
                    {m.text && <p className="whitespace-pre-wrap leading-relaxed" dir="auto">{m.text}</p>}
                    {m.has_attachment && <p className="text-[0.8rem] opacity-80">{t("attachment_in_telegram", { kind: m.attachment_kind ?? "" })}</p>}
                    <p className={`mt-1 text-[0.72rem] ${mine ? "text-white/75" : "text-[var(--hint)]"}`}>
                      <When iso={m.created_at} time />
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
          {can("tickets.reply") && value.ticket.status !== "closed" && (
            <Section>
              <div className="flex flex-col gap-3">
                <Textarea
                  aria-label={t("reply")}
                  placeholder={t("reply_placeholder")}
                  value={reply}
                  maxLength={4000}
                  onChange={(e) => setReply(e.target.value)}
                />
                <Button tone="primary" className="self-end" busy={busy} disabled={!reply.trim()} onClick={() => void send()}>
                  {t("send_reply")}
                </Button>
              </div>
            </Section>
          )}
        </Page>
      )}
    </Async>
  );
}

// ---------------------------------------------------------------- level-2 resellers (TEN-01)

function SubSheet({
  row,
  maxDiscount,
  onClose,
  onSaved,
}: {
  row: SubReseller | null;
  maxDiscount: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toast, errorText } = useSession();
  const [telegramId, setTelegramId] = useState<number | null>(null);
  const [discount, setDiscount] = useState<number>(row?.discount_percent ?? Math.min(5, maxDiscount));
  const [active, setActive] = useState(row?.is_active ?? true);
  const [busy, run] = useAction();
  const save = async () => {
    try {
      await run(() =>
        row
          ? call(`/api/admin/sub-resellers/${row.id}`, { method: "PUT", body: { discount_percent: discount, is_active: active } })
          : call("/api/admin/sub-resellers", { method: "POST", body: { telegram_id: telegramId, discount_percent: discount } }),
      );
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
      title={row ? (row.customer ?? `#${row.customer_id}`) : t("add_sub_reseller")}
      footer={
        <Button tone="primary" busy={busy} disabled={!row && !telegramId} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        {!row && (
          <Field label={t("telegram_id")} hint={t("sub_reseller_telegram_hint")}>
            {(id) => <NumberInput id={id} allowEmpty value={telegramId} onChange={setTelegramId} />}
          </Field>
        )}
        <Field label={t("discount_percent")} hint={t("parent_discount_cap", { percent: maxDiscount })}>
          {(id) => <NumberInput id={id} value={discount} onChange={(v) => setDiscount(Math.min(maxDiscount, v ?? 0))} />}
        </Field>
        {row && <Toggle checked={active} onChange={setActive} label={t("active")} />}
        <p className="text-[0.78rem] text-[var(--hint)]">{t("commission_explained")}</p>
      </div>
    </Sheet>
  );
}

export function SubResellers() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<{
    max_discount: number;
    limit: number | null;
    total_commission_month?: number;
    items: SubReseller[];
  }>("/api/admin/sub-resellers");
  const [editing, setEditing] = useState<SubReseller | "new" | null>(null);
  const columns: Column<SubReseller>[] = [
    { key: "name", label: t("col_customer"), mobile: "title", render: (r) => <span className="font-semibold">{r.customer ?? `#${r.customer_id}`}</span> },
    { key: "discount", label: t("discount"), mobile: "meta", render: (r) => t("discount_value", { percent: money(r.discount_percent, locale) }) },
    { key: "balance", label: t("balance"), render: (r) => <Money amount={r.balance} /> },
    { key: "orders", label: t("sub_orders_month"), mobile: "hide", render: (r) => money(r.orders_month ?? 0, locale) },
    { key: "sales", label: t("sub_sales_month"), mobile: "hide", render: (r) => <Money amount={r.sales_month ?? 0} /> },
    { key: "commission", label: t("sub_commission_month"), render: (r) => <Money amount={r.commission_month ?? 0} /> },
    { key: "status", label: t("col_status"), mobile: "end", render: (r) => (r.is_active ? <Badge tone="health">{t("active")}</Badge> : <Badge tone="loss">{t("inactive")}</Badge>) },
    { key: "created", label: t("col_created"), mobile: "hide", render: (r) => <When iso={r.created_at} /> },
  ];
  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Page
          title={t("nav_sub_resellers")}
          actions={
            <Button
              tone="primary"
              icon="plus"
              disabled={value.limit !== null && value.items.length >= value.limit}
              onClick={() => setEditing("new")}
            >
              {t("add_sub_reseller")}
            </Button>
          }
        >
          <div className="-mt-2 flex flex-wrap items-center justify-between gap-2 text-[0.85rem] text-[var(--hint)]">
            <p>{t("sub_resellers_intro", { percent: value.max_discount })}</p>
            {(value.total_commission_month ?? 0) > 0 && (
              <Badge tone="brand">
                {t("sub_total_commission")}: <Money amount={value.total_commission_month ?? 0} />
              </Badge>
            )}
          </div>
          <Section flush>
            <Table
              columns={columns}
              rows={value.items}
              onRow={(r) => setEditing(r)}
              empty={<Empty title={t("no_sub_resellers")} hint={t("no_sub_resellers_hint")} />}
            />
          </Section>
          {editing && (
            <SubSheet
              row={editing === "new" ? null : editing}
              maxDiscount={value.max_discount}
              onClose={() => setEditing(null)}
              onSaved={() => {
                setEditing(null);
                void reload();
              }}
            />
          )}
        </Page>
      )}
    </Async>
  );
}

export function MyBilling() {
  const { t, me, can } = useSession();
  const allowed = me?.role === "reseller" && can("dashboard.view");
  return (
    <Page title={t("nav_reseller_hub")}>
      <ResellerHub mode="own" />
      {allowed && <Section title={t("billing_history")}>
        <BillingHistory path="/api/admin/my/billing" />
      </Section>}
    </Page>
  );
}


function ResellerBotInfo({ profileId }: { profileId: number }) {
  const { t, toast, errorText } = useSession();
  const state = useLoad<ResellerBot>(`/api/admin/resellers/${profileId}/bot`);
  const [busy, run] = useAction();
  const [logsOpen, setLogsOpen] = useState(false);
  if (!state.data || !state.data.shop_id) return null;
  const stop = async () => {
    try {
      await run(() => call(`/api/admin/resellers/${profileId}/bot/stop`, { method: "POST" }));
      void state.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const allow = async () => {
    try {
      await run(() => call(`/api/admin/resellers/${profileId}/bot/allow`, { method: "POST" }));
      void state.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <section className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
      <h3 className="text-[0.88rem] font-bold">{t("my_bot_owner_title")}</h3>
      <BotFigures data={state.data} />
      {state.data.connected && <WebhookHealth basePath={`/api/admin/resellers/${profileId}/bot`} />}
      <div className="flex flex-wrap gap-2">
        <Button small tone="quiet" onClick={() => setLogsOpen(true)}>
          {t("bot_logs_open")}
        </Button>
        {state.data.connected && (
          <Button small tone="danger" busy={busy} onClick={() => void stop()}>
            {t("my_bot_stop")}
          </Button>
        )}
      </div>
      {logsOpen && (
        <BotLogsSheet
          basePath={`/api/admin/customer-bots/${state.data.shop_id}/logs`}
          title={state.data.bot_username ? `@${state.data.bot_username}` : state.data.title ?? `#${state.data.shop_id}`}
          onClose={() => setLogsOpen(false)}
        />
      )}
      {state.data.blocked && (
        <Button small busy={busy} className="self-start" onClick={() => void allow()}>
          {t("my_bot_allow")}
        </Button>
      )}
    </section>
  );
}

// Resellers keep their customer-side flow; staff-only desk operations never mount for them.
export function Tickets() {
  const {me,checking,can,t}=useSession();
  if(checking)return null;
  if(!can("tickets.read"))return <Page title={t("nav_tickets")}><p role="alert">{t("err_forbidden")}</p></Page>;
  return me?.role === "reseller" ? <ResellerTickets/> : <TicketDesk/>;
}
export function TicketPage() {
  const {id}=useParams();const {me,checking,can,t}=useSession();
  if(checking)return null;
  if(!can("tickets.read"))return <Page title={t("nav_tickets")}><p role="alert">{t("err_forbidden")}</p></Page>;
  return me?.role === "reseller" ? <ResellerTicketPage key={id}/> : <StaffTicketPage key={id}/>;
}
