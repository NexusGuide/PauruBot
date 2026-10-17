import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { api, type Catalog, type Plan } from "../api";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic, openExternal, tg } from "../telegram";
import { ActionBar, Button, Empty, Item, Loading, ReadFailure, Screen, Steps } from "../ui";

import { useResource } from "../useResource";

function Chips({
  items,
  value,
  onChange,
}: {
  items: { id: number | null; label: string }[];
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  return (
    <div className="app-chips" role="group">
      {items.map((item) => (
        <button
          key={String(item.id)}
          type="button"
          aria-pressed={item.id === value}
          className="app-chip"
          onClick={() => {
            haptic("select");
            onChange(item.id);
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** Keeps a valid plan selected while the list changes. */
function useSelectedPlan(plans: Plan[]) {
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    setSelected((current) => (current !== null && plans.some((plan) => plan.id === current) ? current : plans[0]?.id ?? null));
  }, [plans]);
  return [selected, setSelected] as const;
}

function PlanList({ plans, selected, onSelect }: { plans: Plan[]; selected: number | null; onSelect: (id: number) => void }) {
  const { t, locale } = useApp();
  return (
    <div className="flex flex-col gap-3" role="radiogroup" aria-label={t("plan_pick_title")}>
      {plans.map((plan) => (
        <button
          key={plan.id}
          type="button"
          role="radio"
          aria-checked={plan.id === selected}
          className="app-plan"
          onClick={() => {
            haptic("select");
            onSelect(plan.id);
          }}
        >
          <span className="app-grow">
            <b className="block text-[0.95rem] font-bold leading-7">
              {plan.emoji ? `${plan.emoji} ` : ""}
              {plan.title}
              {plan.badge && <span className="app-badge ms-2 align-middle">{t(`badge_${plan.badge}`)}</span>}
            </b>
            <small className="mt-0.5 block text-[0.8rem] font-medium text-[var(--hint)]">
              {plan.data_gb ? `${money(plan.data_gb, locale)} ${t("gb")}` : t("unlimited_data")}
              {" · "}
              {plan.days ? t("n_days", { days: money(plan.days, locale) }) : t("unlimited_time")}
            </small>
            {plan.on_hold && <small className="block text-[0.75rem] font-medium text-[var(--hint)]">{t("starts_on_first_use")}</small>}
            {plan.description && <small className="mt-0.5 block text-[0.75rem] font-medium text-[var(--hint)]">{plan.description}</small>}
          </span>
          <span className="flex flex-col items-end justify-center text-end">
            {plan.price < plan.base_price && (
              <small className="num text-[0.75rem] text-[var(--hint)] line-through">{money(plan.base_price, locale)}</small>
            )}
            <b className="num text-[1.02rem] font-extrabold">{money(plan.price, locale)}</b>
            <small className="text-[0.7rem] font-medium text-[var(--hint)]">{t("currency")}</small>
          </span>
          <span className="app-rad" aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}

export function Buy() {
  const { me, t, locale, toast } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const serviceId = Number(params.get("service")) || undefined;
  if (serviceId) return <RenewPicker serviceId={serviceId} />;

  const openBot = () => {
    const username = me?.shop.bot_username;
    if (!username) {
      toast(t("contact_support"), "error");
      return;
    }
    const url = `https://t.me/${username}`;
    if (tg) tg.openTelegramLink(url);
    else openExternal(url);
  };

  if (!me) return null;
  return (
    <Screen title={t("buy_title")} subtitle={t("buy_hub_sub")}>
      <div className="app-card">
        <Item icon={<Icon name="globe" />} title={t("buy_v2ray")} sub={t("buy_v2ray_sub")} to="/panel" />
        <Item icon={<Icon name="panel" />} tone="green" title={t("buy_agency")} sub={t("buy_agency_sub")} onClick={openBot} />
      </div>
      <div className="app-card">
        <div className="app-row flex items-center gap-3 px-4">
          <span className="app-ico">
            <Icon name="wallet" />
          </span>
          <span className="app-grow">
            <small className="block text-[0.8rem] font-medium text-[var(--hint)]">{t("your_balance")}</small>
            <b className="num block text-[0.95rem] font-bold">
              {money(me.balance, locale)}{" "}
              <small className="font-semibold text-[var(--hint)]">{t("currency")}</small>
            </b>
          </span>
          <Button className="w-auto" onClick={() => navigate("/wallet")}>
            {t("home_topup")}
          </Button>
        </div>
      </div>
    </Screen>
  );
}

/** Step 1 of the buy flow: pick the panel (location) the config should live on. */
export function PanelStep() {
  const { t } = useApp();
  const { data: catalog, error, retry } = useResource(api.catalog);
  if (error) return <Screen title={t("buy_v2ray")}><ReadFailure error={error} retry={retry} /></Screen>;
  if (!catalog) return <Loading />;
  return (
    <Screen title={t("buy_v2ray")} subtitle={t("panel_pick_sub")}>
      <Steps n={1} note={t("step_note_panel")} />
      <div className="app-card">
        {catalog.locations.length === 0 ? (
          <Item icon={<Icon name="globe" />} title={t("all")} to="/plan" />
        ) : (
          catalog.locations.map((location) => (
            <Item
              key={location.id}
              icon={<Icon name="globe" />}
              title={`${location.flag ?? ""} ${location.title}`.trim()}
              to={`/plan?location=${location.id}`}
            />
          ))
        )}
      </div>
    </Screen>
  );
}

/** Step 2 of the buy flow: pick the plan for the chosen panel. */
export function PlanStep() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const locationParam = Number(params.get("location")) || null;
  const { data: catalog, error, retry } = useResource(api.catalog);
  const [category, setCategory] = useState<number | null>(null);

  const plans = useMemo(() => {
    if (!catalog) return [];
    const valid = catalog.locations.some((item) => item.id === locationParam) ? locationParam : null;
    const restrict = valid !== null && catalog.locations.length >= 2;
    return catalog.plans.filter(
      (plan) => (!restrict || plan.location_id === valid) && (category === null || plan.category_id === category),
    );
  }, [catalog, locationParam, category]);

  const [selected, setSelected] = useSelectedPlan(plans);
  const chosen = plans.find((plan) => plan.id === selected) ?? null;

  if (error) return <Screen title={t("plan_pick_title")}><ReadFailure error={error} retry={retry} /></Screen>;
  if (!catalog) return <Loading />;
  const locations = catalog.locations;
  const valid = locations.some((item) => item.id === locationParam) ? locationParam : null;
  const active = valid !== null && locations.length >= 2 ? locations.find((item) => item.id === valid) ?? null : null;
  const usedCategories = catalog.categories.filter((item) => plans.some((plan) => plan.category_id === item.id) || item.id === category);

  const choose = () => {
    if (chosen) navigate(`/checkout?kind=purchase&plan=${chosen.id}`);
  };

  return (
    <Screen title={t("plan_pick_title")}>
      <Steps n={2} note={t("step_note_plan")} />
      {active && (
        <div className="app-chips">
          <button type="button" className="app-chip" onClick={() => navigate("/panel")}>
            {`${active.flag ?? ""} ${active.title}`.trim()}
            {" · "}
            <b className="font-bold text-[var(--accent)]">{t("change_panel")}</b>
          </button>
        </div>
      )}
      {usedCategories.length > 1 && (
        <Chips
          items={[{ id: null, label: t("all") }, ...usedCategories.map((item) => ({ id: item.id as number | null, label: item.title }))]}
          value={category}
          onChange={setCategory}
        />
      )}
      {plans.length === 0 && !(catalog.custom && !active) ? (
        <Empty title={t("no_plans")} />
      ) : (
        <PlanList plans={plans} selected={selected} onSelect={setSelected} />
      )}
      {catalog.custom && !active && (
        <button
          type="button"
          onClick={() => navigate("/custom")}
          className="mt-3 flex w-full items-center justify-between gap-4 rounded-3xl border-2 border-dashed border-[var(--line)] p-4 text-start active:opacity-80"
        >
          <span>
            <b className="block font-bold">{t("custom_plan")}</b>
            <small className="mt-0.5 block text-[0.8rem] font-medium text-[var(--hint)]">{t("custom_plan_hint")}</small>
          </span>
          <Icon name="back" size={18} className="app-chev rotate-180" />
        </button>
      )}
      {chosen && (
        <ActionBar
          summary={`${chosen.data_gb ? `${money(chosen.data_gb, locale)} ${t("gb")}` : t("unlimited_data")} · ${
            chosen.days ? t("n_days", { days: money(chosen.days, locale) }) : t("unlimited_time")
          }`}
          price={`${money(chosen.price, locale)} ${t("currency")}`}
          action={t("continue")}
          onClick={choose}
        />
      )}
    </Screen>
  );
}

/** Renew / upgrade: plan picking without the panel step (the service pins the panel). */
function RenewPicker({ serviceId }: { serviceId: number }) {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const kind = params.get("kind") === "upgrade" ? "upgrade" : "renew";
  const panelId = Number(params.get("panel")) || undefined;
  const { data: catalog, error, retry } = useResource(api.catalog);
  const [category, setCategory] = useState<number | null>(null);

  const plans = useMemo(() => {
    if (!catalog) return [];
    return catalog.plans.filter(
      (plan) =>
        (panelId ? plan.panel_id === panelId : true) &&
        (category === null || plan.category_id === category) &&
        (kind !== "renew" || plan.renewable),
    );
  }, [catalog, panelId, category, kind]);

  const [selected, setSelected] = useSelectedPlan(plans);
  const chosen = plans.find((plan) => plan.id === selected) ?? null;

  const title = t(kind === "upgrade" ? "choose_upgrade" : "choose_renew");
  if (error) return <Screen title={title}><ReadFailure error={error} retry={retry} /></Screen>;
  if (!catalog) return <Loading />;
  const usedCategories = catalog.categories.filter((item) => plans.some((plan) => plan.category_id === item.id) || item.id === category);

  return (
    <Screen title={title}>
      {usedCategories.length > 1 && (
        <Chips
          items={[{ id: null, label: t("all") }, ...usedCategories.map((item) => ({ id: item.id as number | null, label: item.title }))]}
          value={category}
          onChange={setCategory}
        />
      )}
      {plans.length === 0 ? (
        <Empty title={t("no_plans")} />
      ) : (
        <PlanList plans={plans} selected={selected} onSelect={setSelected} />
      )}
      {chosen && (
        <ActionBar
          summary={`${chosen.data_gb ? `${money(chosen.data_gb, locale)} ${t("gb")}` : t("unlimited_data")} · ${
            chosen.days ? t("n_days", { days: money(chosen.days, locale) }) : t("unlimited_time")
          }`}
          price={`${money(chosen.price, locale)} ${t("currency")}`}
          action={t("continue")}
          onClick={() => navigate(`/checkout?kind=${kind}&plan=${chosen.id}&service=${serviceId}`)}
        />
      )}
    </Screen>
  );
}

function roundPrice(value: number) {
  return Math.round(value / 100) * 100;
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  onChange: (v: number) => void;
}) {
  const { locale } = useApp();
  return (
    <label className="block">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-sm text-[var(--hint)]">{label}</span>
        <span className="num text-xl font-extrabold">
          {money(value, locale)} <span className="text-sm font-semibold text-[var(--hint)]">{unit}</span>
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-[var(--accent)]"
      />
      <div className="num flex justify-between text-xs text-[var(--hint)]">
        <span>{money(min, locale)}</span>
        <span>{money(max, locale)}</span>
      </div>
    </label>
  );
}

export function Custom() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [offer, setOffer] = useState<Catalog["custom"] | undefined>(undefined);
  const [gb, setGb] = useState(0);
  const [days, setDays] = useState(0);

  useEffect(() => {
    api.catalog().then((c) => {
      setOffer(c.custom);
      if (c.custom) {
        setGb(Math.min(c.custom.max_gb, Math.max(c.custom.min_gb, 50)));
        setDays(Math.min(c.custom.max_days, Math.max(c.custom.min_days, 30)));
      }
    });
  }, []);

  if (offer === undefined) return <Loading />;
  if (offer === null) return <Empty title={t("custom_unavailable")} />;
  const base = roundPrice(offer.base_price + gb * offer.gb_price + days * offer.day_price);
  const price = offer.discount_percent ? roundPrice((base * (100 - offer.discount_percent)) / 100) : base;
  return (
    <Screen title={t("custom_plan")}>
      <div className="app-card p-5">
        <div className="flex flex-col gap-6">
          <Slider label={t("custom_data")} value={gb} min={offer.min_gb} max={offer.max_gb}
            step={offer.max_gb - offer.min_gb >= 100 ? 5 : 1} unit={t("gb")} onChange={setGb} />
          <Slider label={t("custom_time")} value={days} min={offer.min_days} max={offer.max_days}
            step={1} unit={t("days")} onChange={setDays} />
          <div className="flex items-baseline justify-between border-t border-dashed border-[var(--line)] pt-4">
            <span className="text-sm text-[var(--hint)]">{t("price")}</span>
            <span className="num text-2xl font-extrabold">
              {money(price, locale)} <span className="text-sm font-semibold text-[var(--hint)]">{t("currency")}</span>
            </span>
          </div>
        </div>
      </div>
      <ActionBar
        summary={t("custom_plan")}
        price={`${money(price, locale)} ${t("currency")}`}
        action={t("continue")}
        onClick={() => navigate(`/checkout?kind=purchase&cgb=${gb}&cdays=${days}`)}
      />
    </Screen>
  );
}
