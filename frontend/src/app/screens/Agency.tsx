import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { date, daysLeft, money, size } from "@shared/format";
import { api, type PanelAccount, type PanelPlan } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Icon } from "../icons";
import { Button, Chevron, Empty, ReadFailure, Ring, Screen, Switch } from "../ui";
import { useResource } from "../useResource";
import { Chips } from "./Buy";

import { DAY_STEPS, GB_STEPS, USER_STEPS, parseAmount, stepsWithin } from "../agency-extras";

type Picker = "gb" | "days" | "users" | "renew";
const ORDER_KIND: Record<Exclude<Picker, "renew">, string> = { gb: "account_gb", days: "account_days", users: "account_users" };

function badgeTone(status: string) {
  if (status === "active") return "green";
  if (status === "paused") return "orange";
  return "red";
}

function planLine(
  t: (key: string, vars?: Record<string, string | number>) => string,
  locale: string,
  plan: PanelPlan,
) {
  const parts = [
    plan.data_gb ? `${money(plan.data_gb, locale)} ${t("gb")}` : t("unlimited_data"),
    plan.max_users ? t("agency_users_n", { count: money(plan.max_users, locale) }) : null,
    plan.days ? t("n_days", { days: money(plan.days, locale) }) : t("unlimited_time"),
  ].filter(Boolean);
  return parts.join(" · ") || plan.title;
}

/** Hub of panel admin accounts: plans to buy on top, the customer's own ones below (TEN-14). */
export function Agency() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const plans = useResource(api.accountPlans);
  const accounts = useResource(api.accounts);
  const [category, setCategory] = useState<number | null>(null);
  if (plans.error || accounts.error) {
    const failure = plans.error ?? accounts.error;
    const retry = plans.error ? plans.retry : accounts.retry;
    return (
      <Screen title={t("agency_title")}>
        <ReadFailure error={failure} retry={retry} />
      </Screen>
    );
  }
  if (!plans.data || !accounts.data) return <div className="app-loading" />;
  const items = accounts.data.items;
  const groups = (plans.data.categories ?? []).filter((group) => plans.data?.plans.some((plan) => plan.category_id === group.id));
  // Uncategorised plans stay under "all" when the shop sorts the rest into categories.
  const shown = category === null ? plans.data.plans : plans.data.plans.filter((plan) => plan.category_id === category);
  return (
    <Screen title={t("agency_title")} subtitle={t("agency_sub")}>
      <p className="app-lbl">{t("agency_plans")}</p>
      {plans.data.plans.length === 0 && items.length === 0 && (
        <Empty title={t("agency_none")} hint={t("agency_none_hint")} />
      )}
      {plans.data.plans.length === 0 && items.length > 0 && (
        <p className="app-hint px-1 text-sm">{t("agency_none_now")}</p>
      )}
      {groups.length > 0 && (
        <Chips
          items={[{ id: null, label: t("all") }, ...groups.map((group) => ({ id: group.id as number | null, label: group.title }))]}
          value={category}
          onChange={setCategory}
        />
      )}
      <ul className="flex flex-col gap-3" role="group" aria-label={t("agency_plans")}>
        {shown.map(plan => (
          <li key={plan.id} className="app-card">
            <button
              type="button"
              className="app-row app-rip-host flex w-full items-center gap-3 px-4 py-4 text-start"
              onClick={() => {
                haptic("select");
                navigate(`/checkout?kind=account_purchase&aplan=${plan.id}&name=${encodeURIComponent(plan.title)}`);
              }}
            >
              <span className="app-ico" data-tone="green">
                <Icon name="panel" />
              </span>
              <span className="app-grow">
                <b className="block text-[0.95rem] font-bold leading-7">{plan.title}</b>
                <small className="mt-0.5 block text-[0.8rem] font-medium text-[var(--hint)]">
                  {planLine(t, locale, plan)}
                </small>
                {plan.description && (
                  <small className="mt-0.5 block text-[0.75rem] font-medium text-[var(--hint)]">
                    {plan.description}
                  </small>
                )}
              </span>
              <span className="flex flex-col items-end justify-center text-end">
                <b className="num text-[1.02rem] font-extrabold">{money(plan.price, locale)}</b>
                <small className="text-[0.7rem] font-medium text-[var(--hint)]">{t("currency")}</small>
              </span>
              <Chevron />
            </button>
          </li>
        ))}
      </ul>
      <p className="app-lbl">{t("agency_mine")}</p>
      {items.length === 0 && (
        <p className="app-hint px-1 text-sm">{t("agency_mine_hint")}</p>
      )}
      <AccountRows items={items} />
    </Screen>
  );
}

/** The customer's panel admin accounts, one row each, opening the account's page. */
function AccountRows({ items }: { items: PanelAccount[] }) {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  return (
      <ul className="flex flex-col">
        {items.map(account => (
          <li key={account.id} className="app-card">
            <button
              type="button"
              className="app-row app-rip-host flex w-full items-center gap-3 px-4 py-3 text-start"
              onClick={() => {
                haptic("select");
                navigate(`/agency/${account.id}`);
              }}
            >
              <span className="app-ico">
                <Icon name="panel" />
              </span>
              <span className="app-grow">
                <b className="flex items-center gap-2 text-[0.95rem] font-bold">
                  <span dir="ltr" className="truncate">{account.username}</span>
                  <span className="app-badge" data-tone={badgeTone(account.status)}>
                    {t(`pacc_${account.status}`)}
                  </span>
                </b>
                <small className="num mt-0.5 block text-[0.8rem] font-medium text-[var(--hint)]">
                  {account.plan_title}
                  {account.expire_at &&
                    ` · ${t("days_left", { days: money(daysLeft(account.expire_at) ?? 0, locale) })}`}
                </small>
              </span>
              <Chevron />
            </button>
          </li>
        ))}
      </ul>
  );
}

/** "My panels" wherever the customer looks for what they own (the services list too);
 *  nothing at all when they have none or the list cannot be read. */
export function MyPanels() {
  const { t } = useApp();
  const accounts = useResource(api.accounts);
  const items = accounts.data?.items ?? [];
  if (items.length === 0) return null;
  return (
    <>
      <p className="app-lbl">{t("agency_mine")}</p>
      <AccountRows items={items} />
    </>
  );
}

/** One panel admin account: quota, users, expiry, the login and its paid actions (TEN-14). */
export function AgencyAccount() {
  const { t, locale, toast } = useApp();
  const navigate = useNavigate();
  const { id } = useParams();
  const accountId = Number(id);
  const load = useCallback(async () => api.account(accountId), [accountId]);
  const { data, error, retry } = useResource(load);
  const [picker, setPicker] = useState<Picker | null>(null);
  const [typed, setTyped] = useState("");
  const [typedGb, setTypedGb] = useState("");
  const [login, setLogin] = useState<{ url: string; username: string; password: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPicker(null);
    setLogin(null);
  }, [accountId]);
  useEffect(() => {
    setTyped("");
    setTypedGb("");
  }, [picker]);

  if (error) {
    return (
      <Screen title={t("agency_title")}>
        <ReadFailure error={error} retry={retry} />
      </Screen>
    );
  }
  if (!data) return <div className="app-loading" />;
  const account: PanelAccount = data.account;
  const left = daysLeft(account.expire_at);
  const units = t("units").split(",");
  const remaining = account.data_limit ? account.data_limit - account.used_bytes : null;
  const quota = account.data_limit
    ? Math.min(1, Math.max(0, account.used_bytes / account.data_limit))
    : 0;
  const usersQuota = account.max_users
    ? Math.min(1, Math.max(0, account.users_count / account.max_users))
    : null;

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      haptic("select");
      toast(t("copied"));
    } catch {
      toast(t("copy_failed"), "error");
    }
  };

  const refresh = async () => {
    setBusy(true);
    try {
      await api.refreshAccount(account.id);
      haptic("success");
      toast(t("agency_refreshed"));
      retry();
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  const setAuto = async (enabled: boolean, pkg?: number) => {
    setBusy(true);
    try {
      await api.accountAutoRenew(account.id, enabled, pkg);
      haptic("success");
      toast(t(enabled ? "agency_auto_renew_on" : "agency_auto_renew_off"));
      retry();
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  const reveal = async () => {
    setBusy(true);
    try {
      setLogin(await api.accountLogin(account.id));
      haptic("success");
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  const go = (kind: string, amount?: number, extra = "") =>
    navigate(
      `/checkout?kind=${kind}&acc=${account.id}&name=${encodeURIComponent(account.username)}${amount ? `&amt=${amount}` : ""}${extra}`,
    );
  const renewal = account.renew;
  const choosesRenewal = renewal !== null && !renewal.plain;

  return (
    <Screen title={account.username} subtitle={account.plan_title ?? t("agency_title")}>
      <section className="app-card p-5 text-center">
        <Ring size={170} outer={quota} inner={usersQuota}>
          <span className="num text-2xl font-extrabold">
            {remaining === null ? "∞" : size(Math.max(0, remaining), locale, units)}
          </span>
          <span className="mt-0.5 text-xs text-[var(--hint)]">{t("data_left_label")}</span>
          <span className="num mt-1 text-sm font-bold">
            {left === null ? t("never") : t("days_left", { days: money(left, locale) })}
          </span>
        </Ring>
        <p className="mt-3">
          <span className="app-badge" data-tone={badgeTone(account.status)}>
            {t(`pacc_${account.status}`)}
          </span>
        </p>
        <div className="-mx-5 mt-2 border-t border-[var(--line)] px-5 text-start">
          <div className="app-ln">
            <small>{t("used")}</small>
            <b className="num">
              {size(account.used_bytes, locale, units)}
              {account.data_limit ? ` / ${size(account.data_limit, locale, units)}` : ""}
            </b>
          </div>
          <div className="app-ln">
            <small>{t("agency_users")}</small>
            <b className="num">
              {money(account.users_count, locale)}
              {account.max_users ? ` / ${money(account.max_users, locale)}` : ""}
            </b>
          </div>
          <div className="app-ln">
            <small>{t("expires")}</small>
            <b className="num">{account.expire_at ? date(account.expire_at, locale) : t("never")}</b>
          </div>
        </div>
      </section>

      <div className="mb-3.5 grid gap-3">
        {renewal?.allowed !== false && <Button
          onClick={() => {
            if (!choosesRenewal) return go("account_renew");
            haptic("select");
            setPicker(picker === "renew" ? null : "renew");
          }}
        >
          {t("agency_renew")}
        </Button>}
        {picker === "renew" && renewal && (() => {
          const days = parseAmount(typed, data.limits.days);
          const gb = renewal.quota ? parseAmount(typedGb, data.limits.gb) : 0;
          const ready = days !== null && gb !== null;
          const price = ready ? days * renewal.day_price + (gb ?? 0) * renewal.gb_price : 0;
          const hint = (range: [number, number]) =>
            t("agency_custom_amount", { low: money(range[0], locale), high: money(range[1], locale) });
          return (
            <>
              {renewal.packages.length > 0 && (
                <ul className="grid grid-cols-2 gap-3">
                  {renewal.packages.map((item, index) => (
                    <li key={index}>
                      <button
                        type="button"
                        onClick={() => {
                          haptic("select");
                          go("account_renew", undefined, `&rpkg=${index}`);
                        }}
                        className="w-full rounded-3xl bg-[var(--surface)] p-4 text-center"
                      >
                        <p className="num text-xl font-extrabold">{item.days ? t("n_days", { days: money(item.days, locale) }) : t("unlimited_time")}</p>
                        <p className="num text-xs text-[var(--hint)]">
                          {item.gb ? `${money(item.gb, locale)} ${t("gb")}` : t("unlimited_data")}
                        </p>
                        <p className="num mt-2 text-sm font-semibold">{money(item.price, locale)} {t("currency")}</p>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {renewal.custom && (
                <div className="app-card flex flex-col gap-2 p-4">
                  <p className="text-sm font-bold">{t("agency_renew_custom")}</p>
                  <label className="text-xs text-[var(--hint)]">
                    {t("days")}
                    <input
                      inputMode="numeric"
                      maxLength={7}
                      placeholder={hint(data.limits.days)}
                      value={typed}
                      onChange={(e) => setTyped(e.target.value)}
                      className="num mt-1 w-full"
                    />
                  </label>
                  {renewal.quota && (
                    <label className="text-xs text-[var(--hint)]">
                      {t("gb")}
                      <input
                        inputMode="numeric"
                        maxLength={7}
                        placeholder={hint(data.limits.gb)}
                        value={typedGb}
                        onChange={(e) => setTypedGb(e.target.value)}
                        className="num mt-1 w-full"
                      />
                    </label>
                  )}
                  <Button
                    disabled={!ready}
                    onClick={() => {
                      if (!ready) return;
                      haptic("select");
                      go("account_renew", undefined, `&cdays=${days}${gb ? `&cgb=${gb}` : ""}`);
                    }}
                  >
                    {ready ? `${t("agency_buy_amount")} · ${money(price, locale)} ${t("currency")}` : t("agency_buy_amount")}
                  </Button>
                </div>
              )}
            </>
          );
        })()}
        {account.gb_price > 0 && account.data_limit > 0 && (
          <Button tone="quiet" onClick={() => { haptic("select"); setPicker(picker === "gb" ? null : "gb"); }}>
            {t("agency_add_data")}
          </Button>
        )}
        {account.day_price > 0 && (
          <Button tone="quiet" onClick={() => { haptic("select"); setPicker(picker === "days" ? null : "days"); }}>
            {t("agency_add_days")}
          </Button>
        )}
        {account.user_price > 0 && account.max_users != null && (
          <Button tone="quiet" onClick={() => { haptic("select"); setPicker(picker === "users" ? null : "users"); }}>
            {t("agency_add_users")}
          </Button>
        )}
        {picker && picker !== "renew" && (() => {
          const unit = picker === "gb" ? account.gb_price : picker === "days" ? account.day_price : account.user_price;
          const range = picker === "users" ? null : data.limits[picker];
          const steps = picker === "users" ? USER_STEPS : stepsWithin(picker === "gb" ? GB_STEPS : DAY_STEPS, range!);
          const label = picker === "gb" ? t("gb") : picker === "days" ? t("days") : t("agency_users");
          const typedValue = range ? parseAmount(typed, range) : null;
          return (
            <>
              <ul className="grid grid-cols-2 gap-3">
                {steps.map(value => (
                  <li key={value}>
                    <button
                      type="button"
                      onClick={() => {
                        haptic("select");
                        go(ORDER_KIND[picker], value);
                      }}
                      className="w-full rounded-3xl bg-[var(--surface)] p-4 text-center"
                    >
                      <p className="num text-2xl font-extrabold">{money(value, locale)}</p>
                      <p className="text-xs text-[var(--hint)]">{label}</p>
                      <p className="num mt-2 text-sm font-semibold">{money(value * unit, locale)} {t("currency")}</p>
                    </button>
                  </li>
                ))}
              </ul>
              {range && (
                <div className="flex gap-2">
                  <input
                    aria-label={t("agency_custom_amount", { low: money(range[0], locale), high: money(range[1], locale) })}
                    placeholder={t("agency_custom_amount", { low: money(range[0], locale), high: money(range[1], locale) })}
                    inputMode="numeric"
                    maxLength={7}
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                    className="num min-w-0 flex-1"
                  />
                  <Button
                    tone="quiet"
                    disabled={typedValue === null}
                    onClick={() => {
                      if (typedValue === null) return;
                      haptic("select");
                      go(ORDER_KIND[picker], typedValue);
                    }}
                  >
                    {typedValue === null ? t("agency_buy_amount") : `${t("agency_buy_amount")} · ${money(typedValue * unit, locale)} ${t("currency")}`}
                  </Button>
                </div>
              )}
              {range && typed.trim() !== "" && typedValue === null && (
                <p className="text-xs text-[var(--hint)]">
                  {t("agency_amount_range", { low: money(range[0], locale), high: money(range[1], locale) })}
                </p>
              )}
            </>
          );
        })()}
        {account.auto_renew.choices.length > 0 && (
          <div className="app-card flex flex-col gap-2 p-4">
            <Switch
              checked={account.auto_renew.on}
              disabled={busy}
              onChange={() =>
                void setAuto(!account.auto_renew.on, account.auto_renew.choice.package ?? account.auto_renew.choices[0]?.package)
              }
              label={t("agency_auto_renew")}
            />
            <p className="text-xs text-[var(--hint)]">{t("agency_auto_renew_hint")}</p>
            {account.auto_renew.on && account.auto_renew.choices.length > 1 && (
              <select
                aria-label={t("agency_auto_renew_choice")}
                value={account.auto_renew.choice.package ?? -1}
                disabled={busy}
                onChange={(e) => void setAuto(true, Number(e.target.value) >= 0 ? Number(e.target.value) : undefined)}
                className="w-full"
              >
                {account.auto_renew.choices.map((choice) => {
                  const item = choice.package !== undefined ? renewal?.packages[choice.package] : undefined;
                  return (
                    <option key={choice.package ?? -1} value={choice.package ?? -1}>
                      {item
                        ? `${item.days ? t("n_days", { days: money(item.days, locale) }) : t("unlimited_time")} · ${item.gb ? `${money(item.gb, locale)} ${t("gb")}` : t("unlimited_data")} · ${money(item.price, locale)} ${t("currency")}`
                        : `${t("agency_auto_renew_plain")} · ${money(account.price, locale)} ${t("currency")}`}
                    </option>
                  );
                })}
              </select>
            )}
          </div>
        )}
        <Button tone="quiet" busy={busy} onClick={refresh}>{t("agency_refresh")}</Button>
      </div>

      <section className="app-card p-5">
        <p className="mb-2 text-sm font-bold">{t("agency_login")}</p>
        {!login ? (
          <>
            <Button tone="quiet" busy={busy} onClick={reveal}>{t("agency_login_show")}</Button>
            <p className="mt-2 text-center text-xs text-[var(--hint)]">{t("agency_login_hint")}</p>
          </>
        ) : (
          <>
            {([
              [t("agency_login_url"), login.url],
              [t("account_username"), login.username],
              [t("account_password"), login.password],
            ] as [string, string][]).map(([label, value]) => (
              <button
                key={label}
                type="button"
                onClick={() => copy(value)}
                className="mb-2 flex w-full items-center justify-between gap-3 rounded-2xl bg-[var(--bg)] p-3 text-start"
              >
                <span className="text-sm text-[var(--hint)]">{label}</span>
                <span dir="ltr" className="min-w-0 truncate font-mono text-sm">{value}</span>
              </button>
            ))}
            <p className="text-center text-xs text-[var(--hint)]">{t("tap_to_copy")}</p>
          </>
        )}
      </section>
    </Screen>
  );
}
