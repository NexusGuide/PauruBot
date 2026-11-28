import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { daysLeft, money, size } from "@shared/format";
import { api, type Service } from "../api";
import { buildAlerts, lowVolumeWarning, parseAlertPolicy, readAlertIds } from "../alerts";
import { Icon } from "../icons";
import { errorText, useApp } from "../state";
import { haptic, initData } from "../telegram";
import { Chevron, CountUp, Item, ReadFailure, Screen, Spinner } from "../ui";

import { useResource } from "../useResource";
import { dataLeft } from "../data-left";

export function usageRatio(service: Service): number | null {
  if (!service.data_limit) return null;
  return 1 - service.used_bytes / service.data_limit;
}

export function timeRatio(service: Service, totalDays = 30): number | null {
  const left = daysLeft(service.expire_at);
  if (left === null) return null;
  return Math.min(1, left / totalDays);
}

/** The shop's logo (REF-08), fetched with the Mini App's credentials; null until it loads. */
function useBrandLogo(enabled: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const abort = new AbortController();
    let created: string | null = null;
    void fetch("/api/webapp/brand/logo", { headers: { "X-Telegram-Init-Data": initData() }, signal: abort.signal })
      .then(async (response) => {
        if (!response.ok) return;
        created = URL.createObjectURL(await response.blob());
        setUrl(created);
      })
      .catch(() => {
        // no logo: the title alone is fine
      });
    return () => {
      abort.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [enabled]);
  return url;
}

export function Home() {
  const { me } = useApp();
  return me ? <HomeBody key={me.id} /> : null;
}

function HomeBody() {
  const { me, t, locale, reload, toast } = useApp();
  const logo = useBrandLogo(Boolean(me?.shop.brand?.logo));
  const navigate = useNavigate();
  const { data, error, retry } = useResource(api.services);
  const { data: history } = useResource(api.history);
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialPicker, setTrialPicker] = useState(false);
  const [read] = useState<Set<string>>(() => (me ? readAlertIds(me.id) : new Set<string>()));

  if (!me) return null;
  const units = t("units").split(",");
  const policy = parseAlertPolicy(me.alert_policy);
  const services = data?.items ?? null;
  const featured = services?.find((service) => service.status === "active") ?? services?.[0];
  const left = featured ? daysLeft(featured.expire_at) : null;
  const unread =
    services && history ? buildAlerts(services, history.items, t, locale, policy).filter((alert) => !read.has(alert.id)).length : 0;
  const ratio = featured ? usageRatio(featured) : null;
  const remaining = featured && ratio !== null ? dataLeft(featured.data_limit, featured.used_bytes) : null;
  const remainingPct = ratio === null ? null : Math.max(0, Math.min(100, Math.round(ratio * 100)));

  const trialPanels = me.trial.panels ?? [];
  const hasTrialChoice = Boolean(me.trial.choose_panel && trialPanels.length > 1);
  const durationLabel = (days: number, hours: number) =>
    hours > 0
      ? t("trial_hours_label", { hours: money(hours, locale) })
      : t("trial_days_label", { days: money(days, locale) });
  const volumeLabel = (gb: number, mb: number) =>
    mb > 0
      ? t("trial_mb_label", { mb: money(mb, locale) })
      : t("trial_gb_label", { gb: money(gb, locale) });
  const trialText = (days: number, hours: number, gb: number, mb: number) =>
    hours > 0 || mb > 0
      ? t("take_trial_custom", { duration: durationLabel(days, hours), volume: volumeLabel(gb, mb) })
      : t("take_trial", { days, gb });
  const trialTitle = trialText(me.trial.days, me.trial.hours ?? 0, me.trial.gb, me.trial.mb ?? 0);

  const takeTrial = async (panelId?: number) => {
    if (panelId === undefined && hasTrialChoice) {
      setTrialPicker((open) => !open);
      return;
    }
    setTrialBusy(true);
    try {
      await api.trial(panelId);
      setTrialPicker(false);
      haptic("success");
      toast(t("trial_started"));
      await reload();
      navigate("/services");
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setTrialBusy(false);
    }
  };

  return (
    <Screen>
      <header className="app-heading">
        {logo && <img className="app-brand-logo" src={logo} alt="" />}
        <div className="app-heading-grow">
          <p className="app-hint text-sm font-semibold">{me.shop.title}</p>
          <h1>{t("home_hello", { name: me.name ?? "" })}</h1>
        </div>
        <div className="app-heading-action">
          <button
            type="button"
            className="app-iconbtn app-rip-host"
            aria-label={t("notifications")}
            onClick={() => navigate("/notifications")}
          >
            <Icon name="bell" size={20} />
            {unread > 0 && (
              <span aria-hidden="true" className="absolute end-2 top-2 h-2.5 w-2.5 rounded-full bg-[var(--danger)]" />
            )}
          </button>
        </div>
      </header>

      {me.maintenance && (
        <p className="app-brand-notice" role="status">
          {t("shop_maintenance")}
        </p>
      )}

      {me.shop.brand?.notice && (
        <p className="app-brand-notice" dir="auto">
          {me.shop.brand.notice}
        </p>
      )}

      <div className="app-wcard">
        <p className="app-wcard-label">{t("home_wallet")}</p>
        <div className="app-wcard-amt num">
          <CountUp value={me.balance} format={(value) => money(value, locale)} /> <small>{t("currency")}</small>
        </div>
        <div className="app-wcard-actions">
          <button type="button" className="app-wcard-btn app-rip-host" onClick={() => navigate("/wallet")}>
            <Icon name="plus" size={17} />
            {t("home_topup")}
          </button>
        </div>
      </div>

      {error ? (
        <ReadFailure error={error} retry={retry} />
      ) : services === null ? (
        <div className="flex justify-center py-10 text-[var(--hint)]">
          <Spinner />
        </div>
      ) : (
        <>
          <p className="app-lbl">{t("home_active")}</p>
          <div className="app-card">
            {featured ? (
              <button
                type="button"
                onClick={() => navigate(`/services/${featured.id}`)}
                className="app-row app-rip-host flex w-full items-center gap-3 px-4 text-start"
              >
                <span className="app-ico">
                  <Icon name="globe" size={22} />
                </span>
                <span className="app-grow">
                  <b className="block truncate text-[0.95rem] font-bold" dir="ltr">
                    {featured.label || featured.username}
                  </b>
                  <small className="mt-0.5 block text-[0.8rem] font-medium text-[var(--hint)]">
                    {remaining === null ? t("unlimited_data") : `${size(remaining, locale, units)} ${t("left")}`}
                    {" · "}
                    {left === null ? t("never_expires") : t("days_left", { days: money(left, locale) })}
                  </small>
                  {remainingPct !== null && (
                    <span
                      className="app-prog mt-2 block"
                      data-tone={remainingPct <= 0 ? "red" : lowVolumeWarning(featured, policy) ? "orange" : undefined}
                      aria-hidden="true"
                    >
                      <i style={{ inlineSize: `${remainingPct}%` }} />
                    </span>
                  )}
                </span>
                <Chevron />
              </button>
            ) : (
              <div className="app-empty">
                <b className="block text-[0.95rem] font-bold text-[var(--text)]">{t("no_service_yet")}</b>
                <p className="mt-1">{t("no_service_hint")}</p>
              </div>
            )}
          </div>
        </>
      )}

      <p className="app-lbl">{t("home_quick")}</p>
      <div className="app-card">
        <Item icon={<Icon name="bag" />} title={t("buy_plan")} sub={t("home_buy_sub")} to="/buy" />
        <Item icon={<Icon name="card" />} tone="green" title={t("home_topup_full")} sub={t("home_topup_sub")} to="/wallet" />
        <Item icon={<Icon name="headset" />} tone="orange" title={t("contact_support")} sub={t("home_support_sub")} to="/tickets" />
        {me.trial.available && (
          <>
            <Item
              icon={<Icon name="gift" />}
              tone="orange"
              title={trialTitle}
              sub={hasTrialChoice ? t("trial_choose_panel") : t("trial_row_sub")}
              onClick={() => void takeTrial()}
              disabled={trialBusy}
            />
            {trialPicker && hasTrialChoice && (
              <div className="border-t border-[var(--line)] px-4 py-3">
                <p className="mb-2 text-xs font-semibold text-[var(--hint)]">{t("trial_choose_panel")}</p>
                <div className="flex flex-wrap gap-2">
                  {trialPanels.map((panel) => {
                    const loc = panel.location ? `${panel.flag ? `${panel.flag} ` : ""}${panel.location}` : null;
                    const label = loc && panel.name !== panel.location ? `${loc} · ${panel.name}` : loc || panel.name;
                    const limits = `${durationLabel(panel.days ?? me.trial.days, panel.hours ?? 0)} · ${volumeLabel(panel.gb ?? me.trial.gb, panel.mb ?? 0)}`;
                    return (
                      <button
                        key={panel.id}
                        type="button"
                        disabled={trialBusy}
                        onClick={() => void takeTrial(panel.id)}
                        className="app-chip app-rip-host text-xs font-semibold"
                      >
                        {label} · {limits}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Screen>
  );
}
