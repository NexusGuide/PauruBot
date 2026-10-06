import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { api, type Catalog, type Plan } from "../api";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { Empty, Loading, Screen } from "../ui";

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
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const serviceId = Number(params.get("service")) || undefined;
  const kind = params.get("kind") ?? "purchase";
  const panelId = Number(params.get("panel")) || undefined;
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [location, setLocation] = useState<number | null>(null);
  const [category, setCategory] = useState<number | null>(null);

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
      {plans.length === 0 ? (
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
                  <p className="font-bold">{plan.title}</p>
                  <p className="mt-0.5 text-sm text-[var(--hint)]">
                    {plan.days ? t("n_days", { days: money(plan.days, locale) }) : t("unlimited_time")}
                  </p>
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
        </ul>
      )}
    </Screen>
  );
}
