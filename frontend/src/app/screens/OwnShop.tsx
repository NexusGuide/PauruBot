import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { date, daysLeft, digits, gb, money } from "@shared/format";
import { api, type OwnExtra, type OwnFeatures, type OwnPlan, type OwnPlanInput, type OwnShop } from "../api";
import { Icon } from "../icons";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Button, Empty, Item, Loading, Panel, ReadFailure, Screen, Switch } from "../ui";
import { useResource } from "../useResource";

// The reseller's own management of its dedicated bot (OPB-4): what it sells, where buyers pay
// and how its admin account is doing. Receipts are reviewed on the usual /manage cards.

const latin = (text: string) =>
  text
    .replace(/[\u06F0-\u06F9]/g, (d) => String(d.charCodeAt(0) - 0x06f0))
    .replace(/[\u0660-\u0669]/g, (d) => String(d.charCodeAt(0) - 0x0660));
/** A typed whole number: Persian digits and separators are fine; empty is 0. */
export const whole = (text: string) => {
  const clean = latin(text).replace(/[^\d]/g, "");
  return clean ? Number(clean) : 0;
};

function useOwn() {
  const load = useCallback(() => api.ownShop(), []);
  return useResource(load);
}

function NumberField({ label, value, onChange, hint }: { label: string; value: string; onChange: (v: string) => void; hint?: string }) {
  return (
    <label className="flex min-w-0 flex-1 flex-col gap-1.5">
      <span className="text-sm font-semibold">{label}</span>
      <input inputMode="numeric" dir="ltr" maxLength={12} value={value} onChange={(e) => onChange(e.target.value)} className="num w-full" />
      {hint && <small className="text-xs text-[var(--hint)]">{hint}</small>}
    </label>
  );
}

function Toggle({ title, hint, checked, onChange }: { title: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1">
      <span className="flex flex-col">
        <b className="text-[0.95rem]">{title}</b>
        {hint && <small className="text-xs leading-6 text-[var(--hint)]">{hint}</small>}
      </span>
      <Switch checked={checked} label={title} onChange={() => onChange(!checked)} />
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <p className="mb-4 rounded-2xl bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] px-4 py-3 text-sm leading-7 text-[var(--accent)]">{children}</p>;
}

function Meter({ label, value, used, total }: { label: string; value: string; used: number; total: number }) {
  const share = total > 0 ? Math.min(1, used / total) : 0;
  const tone = share >= 1 ? "bg-anar-500" : share >= 0.9 ? "bg-saffron-500" : "bg-[var(--accent)]";
  return (
    <div className="mb-4">
      <p className="mb-2 flex justify-between text-sm"><span>{label}</span><b className="num">{value}</b></p>
      <div className="h-2.5 overflow-hidden rounded-full bg-[var(--bg)]">
        <div className={`h-full rounded-full ${tone}`} style={{ inlineSize: `${Math.round(share * 100)}%` }} />
      </div>
    </div>
  );
}

function stateOf(shop: OwnShop): { key: string; ok: boolean } {
  if (shop.hold) return { key: "own_state_hold", ok: false };
  if (shop.paused) return { key: `own_state_${shop.paused}`, ok: false };
  return { key: "own_state_selling", ok: true };
}

function Badge({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span className={`rounded-full px-3 py-1 text-xs font-bold ${ok ? "bg-pista-100 text-pista-700" : "bg-anar-100 text-anar-700"}`}>{children}</span>
  );
}

/** Home: the account at a glance, today's figures and the five things to manage. */
export function OwnHome() {
  const { t, locale } = useApp();
  const { data, error, loading, retry } = useOwn();
  if (error) return <Screen title={t("own_title")}><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data) return <Loading />;
  const account = data.account;
  const state = stateOf(data);
  const left = account ? daysLeft(account.expire_at) : null;
  const leftGb = account && account.data_limit ? Math.max(0, Math.round(gb(account.data_limit - account.used_bytes))) : null;
  const n = (value: number) => digits(value, locale);
  const extrasOn = [data.features.trial.enabled, data.features.renew.enabled, data.features.extra_gb.enabled, data.features.extra_days.enabled];
  return (
    <Screen title={t("own_title")} subtitle={data.bot_username ? `@${data.bot_username}` : undefined}>
      <section className="mb-4 rounded-3xl bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_70%,black)] p-5 text-white">
        <div className="mb-3 flex items-center justify-between gap-2">
          <span className="text-sm opacity-90">{t("own_my_panel")}</span>
          <span className="rounded-full bg-white/20 px-3 py-1 text-xs font-bold">{t(state.key)}</span>
        </div>
        <p dir="ltr" className="mb-4 truncate text-end text-lg font-extrabold">{account?.username ?? "—"}</p>
        <div className="grid grid-cols-3 gap-2 text-center">
          <div><p className="text-xs opacity-80">{t("own_data_left")}</p><p className="num font-extrabold">{leftGb === null ? "∞" : t("own_gb", { value: n(leftGb) })}</p></div>
          <div><p className="text-xs opacity-80">{t("own_users")}</p><p className="num font-extrabold">{account ? t("own_of", { used: n(account.users_count), total: account.max_users ? n(account.max_users) : "∞" }) : "—"}</p></div>
          <div><p className="text-xs opacity-80">{t("own_expiry")}</p><p className="num font-extrabold">{left === null ? "∞" : t("own_days", { value: n(Math.max(0, left)) })}</p></div>
        </div>
      </section>
      <div className="mb-4 grid grid-cols-3 gap-2">
        {[
          ["own_sales_today", data.figures.sales_today, ""],
          ["own_customers", data.figures.customers, ""],
          ["own_pending", data.figures.pending_receipts, data.figures.pending_receipts ? "text-saffron-700" : ""],
        ].map(([key, value, tone]) => (
          <div key={key as string} className="rounded-2xl bg-[var(--surface)] p-3">
            <p className="text-xs text-[var(--hint)]">{t(key as string)}</p>
            <p className={`num mt-1 text-lg font-extrabold ${tone}`}>{n(value as number)}</p>
          </div>
        ))}
      </div>
      <div className="app-card">
        <Item icon={<Icon name="layers" />} title={t("own_plans")} sub={t("own_plans_sub", { count: n(data.plans.active) })} to="/shop/plans" />
        <Item icon={<Icon name="card" />} title={t("own_payment")} sub={data.card ? `${data.card.holder} · ${data.card.number.slice(-4)}` : t("own_payment_missing")} to="/shop/payment" />
        <Item
          icon={<Icon name="plus" />}
          title={t("own_features")}
          sub={extrasOn.every(Boolean) ? t("own_features_all") : extrasOn.some(Boolean) ? t("own_features_some") : t("own_features_none")}
          to="/shop/features"
        />
        <Item icon={<Icon name="gauge" />} title={t("own_status")} sub={t("own_status_sub")} to="/shop/status" />
        <Item
          icon={<Icon name="check" />}
          tone="orange"
          title={t("own_receipts")}
          sub={t("own_receipts_sub")}
          badge={data.figures.pending_receipts ? <span className="num rounded-full bg-saffron-500 px-2.5 py-0.5 text-sm font-bold text-white">{n(data.figures.pending_receipts)}</span> : undefined}
          to="/manage"
        />
      </div>
      {!data.card && <p className="mt-4 rounded-2xl bg-saffron-100 px-4 py-3 text-sm font-semibold text-saffron-700">{t("own_card_first")}</p>}
    </Screen>
  );
}

const EMPTY_PLAN: OwnPlanInput = { title: "", data_gb: 30, days: 30, price: 0, group_ids: [], is_active: true, renewable: true };

/** Plans and prices: the reseller's own catalog, made on its own panel. */
export function OwnPlans() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const load = useCallback(() => api.ownPlans(), []);
  const { data, error, loading, retry } = useResource(load);
  if (error) return <Screen title={t("own_plans")} backTo="/shop"><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data) return <Loading />;
  const groupName = (id: string) => data.groups.find((g) => g.id === id)?.title ?? `#${id}`;
  const line = (plan: OwnPlan) =>
    [
      plan.data_gb ? t("own_gb", { value: digits(plan.data_gb, locale) }) : t("own_unlimited_data"),
      plan.days ? t("own_days", { value: digits(plan.days, locale) }) : t("own_no_expiry"),
      plan.group_ids.length ? plan.group_ids.map(groupName).join(", ") : t("own_all_groups"),
    ].join(" · ");
  return (
    <Screen title={t("own_plans")} backTo="/shop">
      <p className="mb-4 text-sm leading-7 text-[var(--hint)]">{t("own_plans_intro")}</p>
      {data.items.length === 0 && <Empty title={t("own_no_plans")} hint={t("own_no_plans_hint")} />}
      {data.items.map((plan) => (
        <button
          key={plan.id}
          type="button"
          onClick={() => navigate(`/shop/plans/${plan.id}`)}
          className={`app-rip-host mb-3 flex w-full items-center justify-between gap-3 rounded-3xl bg-[var(--surface)] p-4 text-start ${plan.is_active ? "" : "opacity-60"}`}
        >
          <span className="min-w-0">
            <b className="block truncate">{plan.title}</b>
            <small className="block text-xs leading-6 text-[var(--hint)]">{line(plan)}</small>
          </span>
          {plan.is_active ? (
            <span className="flex-none text-end"><b className="num block">{money(plan.price, locale)}</b><small className="text-xs text-[var(--hint)]">{t("currency")}</small></span>
          ) : (
            <span className="flex-none rounded-full bg-[var(--bg)] px-3 py-1 text-xs font-bold text-[var(--hint)]">{t("own_hidden")}</span>
          )}
        </button>
      ))}
      <Note>{t("own_plans_note")}</Note>
      <Button onClick={() => navigate("/shop/plans/new")}>{t("own_new_plan")}</Button>
    </Screen>
  );
}

/** One plan: name, data, days, price, its groups on the panel, on sale and renewable. */
export function OwnPlanEdit() {
  const { t, locale, toast } = useApp();
  const navigate = useNavigate();
  const { id } = useParams();
  const fresh = id === "new";
  const load = useCallback(() => api.ownPlans(), []);
  const { data, error, loading, retry } = useResource(load);
  const [form, setForm] = useState<OwnPlanInput | null>(null);
  const [text, setText] = useState({ data_gb: "", days: "", price: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!data || form) return;
    const found = data.items.find((p) => String(p.id) === id);
    const start: OwnPlanInput = found
      ? { title: found.title, data_gb: found.data_gb, days: found.days, price: found.price, group_ids: found.group_ids, is_active: found.is_active, renewable: found.renewable }
      : EMPTY_PLAN;
    setForm(start);
    setText({ data_gb: String(start.data_gb), days: String(start.days), price: start.price ? String(start.price) : "" });
  }, [data, form, id]);

  if (error) return <Screen title={t("own_plan")} backTo="/shop/plans"><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data || !form) return <Loading />;
  if (!fresh && !data.items.some((p) => String(p.id) === id)) return <Screen title={t("own_plan")} backTo="/shop/plans"><Empty title={t("own_plan_missing")} /></Screen>;

  const body = (): OwnPlanInput => ({ ...form, title: form.title.trim(), data_gb: whole(text.data_gb), days: whole(text.days), price: whole(text.price) });
  const save = async () => {
    setBusy(true);
    try {
      if (fresh) await api.ownCreatePlan(body());
      else await api.ownUpdatePlan(Number(id), body());
      haptic("success");
      toast(t("own_saved"));
      navigate("/shop/plans");
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    setBusy(true);
    try {
      const result = await api.ownDeletePlan(Number(id));
      toast(t(result.deleted ? "own_plan_deleted" : "own_plan_hidden"));
      navigate("/shop/plans");
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };
  const toggleGroup = (gid: string) =>
    setForm({ ...form, group_ids: form.group_ids.includes(gid) ? form.group_ids.filter((g) => g !== gid) : [...form.group_ids, gid] });
  const volume = whole(text.data_gb);
  return (
    <Screen title={fresh ? t("own_new_plan") : t("own_plan")} backTo="/shop/plans">
      <Panel className="mb-4">
        <label className="mb-4 flex flex-col gap-1.5">
          <span className="text-sm font-semibold">{t("own_plan_name")}</span>
          <input value={form.title} maxLength={96} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full" />
        </label>
        <div className="mb-1 flex gap-3">
          <NumberField label={t("own_plan_gb")} value={text.data_gb} onChange={(v) => setText({ ...text, data_gb: v })} />
          <NumberField label={t("own_plan_days")} value={text.days} onChange={(v) => setText({ ...text, days: v })} />
        </div>
        <p className="mb-4 text-xs text-[var(--hint)]">{t("own_zero_unlimited")}</p>
        <NumberField label={t("own_plan_price")} value={text.price} onChange={(v) => setText({ ...text, price: v })} />
      </Panel>
      <Panel className="mb-4">
        <p className="mb-3 font-bold">{t("own_groups")}</p>
        {data.groups_ok ? (
          <div className="flex flex-wrap gap-2">
            {data.groups.map((g) => (
              <button key={g.id} type="button" className="app-chip" aria-pressed={form.group_ids.includes(g.id)} onClick={() => toggleGroup(g.id)}>
                <bdi>{g.title}</bdi>
              </button>
            ))}
          </div>
        ) : (
          <p className="text-sm text-anar-700">{t("own_groups_unreadable")}</p>
        )}
        <p className="mt-3 text-xs leading-6 text-[var(--hint)]">{t("own_groups_hint")}</p>
      </Panel>
      <Panel className="mb-4">
        <Toggle title={t("own_on_sale")} checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} />
        <div className="my-2 border-t border-[var(--line)]" />
        <Toggle title={t("own_renewable")} checked={form.renewable} onChange={(v) => setForm({ ...form, renewable: v })} />
      </Panel>
      {volume > 0 && <Note>{t("own_plan_uses", { value: digits(volume, locale) })}</Note>}
      <Button busy={busy} disabled={!form.title.trim()} onClick={() => void save()}>{t("own_save_plan")}</Button>
      {!fresh && (
        <Button tone="danger" className="mt-3" disabled={busy} onClick={() => void remove()}>{t("own_delete_plan")}</Button>
      )}
    </Screen>
  );
}

const EXPIRES = [15, 30, 60, 120, 240, 720, 1440];

/** Card and payment: buyers pay the reseller's own card; it reviews every receipt. */
export function OwnPayment() {
  const { t, locale, toast } = useApp();
  const navigate = useNavigate();
  const load = useCallback(() => api.ownPayment(), []);
  const { data, error, loading, retry } = useResource(load);
  const [number, setNumber] = useState<string | null>(null);
  const [holder, setHolder] = useState("");
  const [expire, setExpire] = useState(60);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!data || number !== null) return;
    setNumber(data.card?.number ?? "");
    setHolder(data.card?.holder ?? "");
    setExpire(data.order_expire_minutes);
  }, [data, number]);

  if (error) return <Screen title={t("own_payment")} backTo="/shop"><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data || number === null) return <Loading />;
  const clean = latin(number).replace(/[\s-]/g, "");
  const grouped = /^\d{16}$/.test(clean) ? clean.replace(/(\d{4})(?=\d)/g, "$1 ") : clean;
  const save = async () => {
    setBusy(true);
    try {
      await api.ownSavePayment({ number: clean, holder: holder.trim(), order_expire_minutes: expire });
      haptic("success");
      toast(t("own_saved"));
      navigate("/shop");
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Screen title={t("own_payment")} backTo="/shop">
      <p className="mb-4 text-sm leading-7 text-[var(--hint)]">{t("own_payment_intro")}</p>
      {clean && (
        <section className="mb-4 rounded-3xl bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_65%,black)] p-5 text-white">
          <p className="mb-3 text-xs opacity-85">{t("own_card_shown")}</p>
          <p dir="ltr" className="num mb-4 text-center text-xl font-extrabold tracking-wide">{grouped}</p>
          <p className="text-sm">{holder || "—"}</p>
        </section>
      )}
      <Panel className="mb-4">
        <label className="mb-4 flex flex-col gap-1.5">
          <span className="text-sm font-semibold">{t("own_card_number")}</span>
          <input dir="ltr" inputMode="numeric" maxLength={32} value={number} onChange={(e) => setNumber(e.target.value)} className="num w-full" />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-semibold">{t("own_card_holder")}</span>
          <input maxLength={64} value={holder} onChange={(e) => setHolder(e.target.value)} className="w-full" />
        </label>
      </Panel>
      <Panel className="mb-4">
        <div className="flex items-center justify-between gap-3">
          <span className="flex flex-col">
            <b className="text-[0.95rem]">{t("own_expire")}</b>
            <small className="text-xs leading-6 text-[var(--hint)]">{t("own_expire_hint")}</small>
          </span>
          <select value={expire} onChange={(e) => setExpire(Number(e.target.value))} className="max-w-[9rem]">
            {EXPIRES.map((m) => (
              <option key={m} value={m}>{m >= 60 ? t("own_hours", { value: digits(m / 60, locale) }) : t("own_minutes", { value: digits(m, locale) })}</option>
            ))}
          </select>
        </div>
      </Panel>
      <Note>{t("own_payment_note")}</Note>
      <Button busy={busy} disabled={!clean || holder.trim().length < 2} onClick={() => void save()}>{t("own_save")}</Button>
    </Screen>
  );
}

type ExtraText = { price: string; min: string; max: string };
const extraText = (e: OwnExtra): ExtraText => ({ price: String(e.price || ""), min: e.min ? String(e.min) : "", max: e.max ? String(e.max) : "" });

/** Trial, renewals and extras, with the reseller's own prices and ranges. */
export function OwnFeaturesScreen() {
  const { t, toast } = useApp();
  const navigate = useNavigate();
  const { data, error, loading, retry } = useOwn();
  const [form, setForm] = useState<OwnFeatures | null>(null);
  const [trial, setTrial] = useState({ gb: "", days: "", quota: "" });
  const [gbText, setGbText] = useState<ExtraText>({ price: "", min: "", max: "" });
  const [dayText, setDayText] = useState<ExtraText>({ price: "", min: "", max: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!data || form) return;
    const f = data.features;
    setForm(f);
    setTrial({ gb: String(f.trial.gb), days: String(f.trial.days), quota: f.trial.daily_quota ? String(f.trial.daily_quota) : "" });
    setGbText(extraText(f.extra_gb));
    setDayText(extraText(f.extra_days));
  }, [data, form]);

  if (error) return <Screen title={t("own_features")} backTo="/shop"><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data || !form) return <Loading />;
  const save = async () => {
    setBusy(true);
    try {
      await api.ownSaveFeatures({
        trial: { enabled: form.trial.enabled, gb: whole(trial.gb), days: whole(trial.days), daily_quota: whole(trial.quota) },
        renew: form.renew,
        extra_gb: { enabled: form.extra_gb.enabled, price: whole(gbText.price), min: whole(gbText.min), max: whole(gbText.max) },
        extra_days: { enabled: form.extra_days.enabled, price: whole(dayText.price), min: whole(dayText.min), max: whole(dayText.max) },
      });
      haptic("success");
      toast(t("own_saved"));
      navigate("/shop");
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };
  const extra = (key: "extra_gb" | "extra_days", text: ExtraText, set: (v: ExtraText) => void) => (
    <Panel className="mb-4">
      <Toggle title={t(`own_${key}`)} checked={form[key].enabled} onChange={(v) => setForm({ ...form, [key]: { ...form[key], enabled: v } })} />
      {form[key].enabled && (
        <div className="mt-3 flex gap-2">
          <NumberField label={t(`own_${key}_price`)} value={text.price} onChange={(v) => set({ ...text, price: v })} />
          <NumberField label={t("own_min")} value={text.min} onChange={(v) => set({ ...text, min: v })} />
          <NumberField label={t("own_max")} value={text.max} onChange={(v) => set({ ...text, max: v })} />
        </div>
      )}
    </Panel>
  );
  return (
    <Screen title={t("own_features")} backTo="/shop">
      <Panel className="mb-4">
        <Toggle title={t("own_trial")} checked={form.trial.enabled} onChange={(v) => setForm({ ...form, trial: { ...form.trial, enabled: v } })} />
        {form.trial.enabled && (
          <>
            <div className="mt-3 flex gap-2">
              <NumberField label={t("own_trial_gb")} value={trial.gb} onChange={(v) => setTrial({ ...trial, gb: v })} />
              <NumberField label={t("own_trial_days")} value={trial.days} onChange={(v) => setTrial({ ...trial, days: v })} />
              <NumberField label={t("own_trial_quota")} value={trial.quota} onChange={(v) => setTrial({ ...trial, quota: v })} />
            </div>
            <p className="mt-3 text-xs leading-6 text-[var(--hint)]">{t("own_trial_hint")}</p>
          </>
        )}
      </Panel>
      <Panel className="mb-4">
        <Toggle title={t("own_renew")} checked={form.renew.enabled} onChange={(v) => setForm({ ...form, renew: { ...form.renew, enabled: v } })} />
        {form.renew.enabled && (
          <>
            <div className="mt-3 flex items-center justify-between gap-3">
              <span className="text-sm">{t("own_renew_left")}</span>
              <select value={form.renew.carry ? "carry" : "reset"} onChange={(e) => setForm({ ...form, renew: { ...form.renew, carry: e.target.value === "carry" } })} className="max-w-[10rem]">
                <option value="carry">{t("own_renew_carry")}</option>
                <option value="reset">{t("own_renew_reset")}</option>
              </select>
            </div>
            <p className="mt-3 text-xs text-[var(--hint)]">{t("own_renew_hint")}</p>
          </>
        )}
      </Panel>
      {extra("extra_gb", gbText, setGbText)}
      {extra("extra_days", dayText, setDayText)}
      <Note>{t("own_features_note")}</Note>
      <Button busy={busy} onClick={() => void save()}>{t("own_save")}</Button>
    </Screen>
  );
}

const ALERTS = ["own_alert_90", "own_alert_3days", "own_alert_expired", "own_alert_traffic", "own_alert_users", "own_alert_paused", "own_alert_resumed"];

/** Panel status and alerts: the account's room, when sales stop by themselves, what it is told. */
export function OwnStatus() {
  const { t, locale } = useApp();
  const { data, error, loading, retry } = useOwn();
  if (error) return <Screen title={t("own_status")} backTo="/shop"><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !data) return <Loading />;
  const account = data.account;
  const state = stateOf(data);
  const n = (value: number) => digits(value, locale);
  const left = account ? daysLeft(account.expire_at) : null;
  const openMain = () => {
    if (!data.main_bot) return;
    const url = `https://t.me/${data.main_bot}`;
    if (tg) tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener");
  };
  return (
    <Screen title={t("own_status")} backTo="/shop">
      <Panel className="mb-4">
        <div className="mb-4 flex items-center justify-between gap-2">
          <b dir="ltr" className="truncate">{account?.username ?? "—"}</b>
          <Badge ok={state.ok}>{t(state.key)}</Badge>
        </div>
        {account && (
          <>
            <Meter
              label={t("own_traffic")}
              value={account.data_limit ? t("own_gb_of", { used: n(Math.round(gb(account.used_bytes))), total: n(Math.round(gb(account.data_limit))) }) : t("own_unlimited_data")}
              used={account.used_bytes}
              total={account.data_limit}
            />
            <Meter
              label={t("own_users")}
              value={t("own_of", { used: n(account.users_count), total: account.max_users ? n(account.max_users) : "∞" })}
              used={account.users_count}
              total={account.max_users ?? 0}
            />
            <p className="mb-4 flex justify-between text-sm">
              <span>{t("own_panel_end")}</span>
              <b className="num">{account.expire_at ? `${date(account.expire_at, locale)} · ${t("own_days_more", { value: n(Math.max(0, left ?? 0)) })}` : "∞"}</b>
            </p>
          </>
        )}
        {data.main_bot && <Button tone="quiet" onClick={openMain}>{t("own_renew_panel")}</Button>}
      </Panel>
      <Panel className="mb-4">
        <p className="mb-3 font-bold">{t("own_auto_stop")}</p>
        <ul className="mb-3 flex flex-col gap-2 text-sm">
          {["own_stop_expired", "own_stop_traffic", "own_stop_users"].map((key) => (
            <li key={key} className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-anar-500" aria-hidden="true" />{t(key)}</li>
          ))}
        </ul>
        <p className="text-xs leading-6 text-[var(--hint)]">{t("own_auto_stop_hint")}</p>
      </Panel>
      <Panel>
        <p className="mb-3 font-bold">{t("own_alerts")}</p>
        <div className="mb-3 flex flex-wrap gap-2">
          {ALERTS.map((key) => <span key={key} className="rounded-full bg-[var(--bg)] px-3 py-1.5 text-xs font-semibold">{t(key)}</span>)}
        </div>
        <p className="text-xs leading-6 text-[var(--hint)]">{t("own_alerts_hint")}</p>
      </Panel>
    </Screen>
  );
}
