import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { api, appFlags, type Catalog, type Plan } from "../api";
import { useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Button, Empty, Loading, Screen } from "../ui";

function Chips<T extends number | null>({
  items,
  value,
  onChange,
}: {
  items: { id: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="-mx-4 mb-4 flex gap-2 overflow-x-auto px-4 pb-1">
      {items.map((item) => (
        <button
          key={String(item.id)}
          type="button"
          onClick={() => {
            haptic("select");
            onChange(item.id);
          }}
          className={`shrink-0 rounded-full px-4 py-2 text-sm font-semibold ${
            item.id === value ? "bg-[var(--accent)] text-white" : "bg-[var(--surface)] text-[var(--text)]"
          }`}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

export function Buy() {
  const { t, locale, me } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const serviceId = Number(params.get("service")) || undefined;
  const kind = params.get("kind") ?? "purchase";
  const panelId = Number(params.get("panel")) || undefined;
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [location, setLocation] = useState<number | null>(null);
  const [category, setCategory] = useState<number | null>(null);
  const forcedMiniapp = appFlags(me ?? undefined).forcedMiniapp;

  useEffect(() => {
    api.catalog().then((c) => {
      setCatalog(c);
      setLocation(c.locations[0]?.id ?? null);
    });
  }, []);

  const plans = useMemo(() => {
    if (!catalog) return [];
    return catalog.plans.filter(
      (p) =>
        (panelId ? p.panel_id === panelId : true) &&
        (serviceId || !location || catalog.locations.length < 2 || p.location_id === location) &&
        (category === null || p.category_id === category) &&
        (kind !== "renew" || p.renewable),
    );
  }, [catalog, location, category, panelId, serviceId, kind]);

  if (!catalog) return <Loading />;
  const title = serviceId ? t(kind === "upgrade" ? "choose_upgrade" : "choose_renew") : t("buy_title");
  const usedCategories = catalog.categories.filter((c) => plans.some((p) => p.category_id === c.id) || c.id === category);

  const choose = (plan: Plan) => {
    const query = new URLSearchParams({ kind, plan: String(plan.id) });
    if (serviceId) query.set("service", String(serviceId));
    navigate(`/checkout?${query}`);
  };

  return (
    <Screen title={title}>
      {forcedMiniapp && me?.shop.bot_username && (
        <div className="mb-4 rounded-3xl bg-[var(--surface)] p-4 text-sm">
          <p className="font-bold">
            {locale === "fa" ? "خرید از داخل مینی‌اپ" : "Buying inside the Mini App"}
          </p>
          <p className="mt-1 text-[var(--hint)]">
            {locale === "fa"
              ? "فروشگاه فقط از طریق مینی‌اپ می‌فروشد؛ برای ادامه همان‌جا بازش کن."
              : "This shop sells through the Mini App only; open it there to continue."}
          </p>
          <button
            type="button"
            onClick={() =>
              tg
                ? tg.openTelegramLink(`https://t.me/${me.shop.bot_username}`)
                : window.open(`https://t.me/${me.shop.bot_username}`, "_blank", "noopener")
            }
            className="mt-3 w-full rounded-2xl bg-[var(--accent)] py-3 font-bold text-white"
          >
            {locale === "fa" ? "باز کردن مینی‌اپ" : "Open the Mini App"}
          </button>
        </div>
      )}
      {!serviceId && catalog.locations.length > 1 && (
        <Chips
          items={catalog.locations.map((l) => ({ id: l.id as number | null, label: `${l.flag ?? ""} ${l.title}`.trim() }))}
          value={location}
          onChange={setLocation}
        />
      )}
      {usedCategories.length > 1 && (
        <Chips
          items={[{ id: null, label: t("all") }, ...usedCategories.map((c) => ({ id: c.id as number | null, label: c.title }))]}
          value={category}
          onChange={setCategory}
        />
      )}
      {plans.length === 0 && !(catalog.custom && kind === "purchase" && !serviceId) ? (
        <Empty title={t("no_plans")} />
      ) : (
        <ul className="flex flex-col gap-3">
          {plans.map((plan) => (
            <li key={plan.id}>
              <button
                type="button"
                onClick={() => choose(plan)}
                className="flex w-full items-stretch gap-4 rounded-3xl bg-[var(--surface)] p-4 text-start active:opacity-80"
              >
                <div className="flex w-16 shrink-0 flex-col items-center justify-center rounded-2xl bg-firouzeh-50 text-firouzeh-700">
                  <span className="num text-2xl font-extrabold leading-none">
                    {plan.data_gb ? money(plan.data_gb, locale) : "∞"}
                  </span>
                  <span className="mt-1 text-[0.65rem] font-bold">{t("gb")}</span>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="font-bold">
                    {plan.emoji ? `${plan.emoji} ` : ""}
                    {plan.title}
                    {plan.badge && (
                      <span className="ms-2 rounded-full bg-[var(--accent)] px-2 py-0.5 align-middle text-[0.65rem] font-bold text-white">
                        {t(`badge_${plan.badge}`)}
                      </span>
                    )}
                  </p>
                  <p className="mt-0.5 text-sm text-[var(--hint)]">
                    {plan.days ? t("n_days", { days: money(plan.days, locale) }) : t("unlimited_time")}
                  </p>
                  {plan.on_hold && <p className="mt-0.5 text-xs text-[var(--hint)]">{t("starts_on_first_use")}</p>}
                  {plan.description && <p className="mt-1 line-clamp-2 text-xs text-[var(--hint)]">{plan.description}</p>}
                </div>
                <div className="flex flex-col items-end justify-center">
                  {plan.price < plan.base_price && (
                    <span className="num text-xs text-[var(--hint)] line-through">{money(plan.base_price, locale)}</span>
                  )}
                  <span className="num text-lg font-extrabold">{money(plan.price, locale)}</span>
                  <span className="text-[0.7rem] text-[var(--hint)]">{t("currency")}</span>
                </div>
              </button>
            </li>
          ))}
          {catalog.custom && kind === "purchase" && !serviceId && (
            <li>
              <button
                type="button"
                onClick={() => navigate("/custom")}
                className="flex w-full items-center justify-between gap-4 rounded-3xl border-2 border-dashed border-[var(--line)] p-4 text-start active:opacity-80"
              >
                <div>
                  <p className="font-bold">{t("custom_plan")}</p>
                  <p className="mt-0.5 text-sm text-[var(--hint)]">{t("custom_plan_hint")}</p>
                </div>
                <span aria-hidden="true" className="inline-block text-2xl text-[var(--hint)] ltr:rotate-180">‹</span>
              </button>
            </li>
          )}
        </ul>
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
      <div className="flex flex-col gap-6 rounded-3xl bg-[var(--surface)] p-5">
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
      <div className="mt-4">
        <Button onClick={() => navigate(`/checkout?kind=purchase&cgb=${gb}&cdays=${days}`)}>{t("continue")}</Button>
      </div>
    </Screen>
  );
}
