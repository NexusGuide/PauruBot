import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { daysLeft, money, size } from "@shared/format";
import { api, type Service } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Panel, ReadFailure, Ring, Screen, Spinner } from "../ui";

import { useResource } from "../useResource";

export function usageRatio(service: Service): number | null {
  if (!service.data_limit) return null;
  return 1 - service.used_bytes / service.data_limit;
}

export function timeRatio(service: Service, totalDays = 30): number | null {
  const left = daysLeft(service.expire_at);
  if (left === null) return null;
  return Math.min(1, left / totalDays);
}

export function Home() {
  const { me, t, locale, reload, toast } = useApp();
  const navigate = useNavigate();
  const { data, error, retry } = useResource(api.services);
  const services = data?.items ?? null;
  const [trialBusy, setTrialBusy] = useState(false);


  if (!me) return null;
  const units = t("units").split(",");
  const featured = services?.find((s) => s.status === "active") ?? services?.[0];
  const left = featured ? daysLeft(featured.expire_at) : null;

  const takeTrial = async () => {
    setTrialBusy(true);
    try {
      await api.trial();
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
      <header className="app-hero">
        <p className="app-hero-kicker">{me.shop.title}</p>
        <h1 className="app-hero-title">{t("home_hello", { name: me.name ?? "" })}</h1>
        <span className="app-hero-glow" aria-hidden="true" />
      </header>

      {error ? <ReadFailure error={error} retry={retry} /> : services === null ? (
        <div className="flex justify-center py-16 text-[var(--hint)]">
          <Spinner />
        </div>
      ) : featured ? (
        <button
          type="button"
          onClick={() => navigate(`/services/${featured.id}`)}
          className="mb-4 flex w-full items-center gap-5 rounded-3xl bg-[var(--surface)] p-5 text-start"
        >
          <Ring size={128} outer={usageRatio(featured)} inner={timeRatio(featured)}>
            <span className="num text-2xl font-extrabold">
              {featured.data_limit ? size(featured.data_limit - featured.used_bytes, locale, units) : "∞"}
            </span>
            <span className="text-[0.7rem] text-[var(--hint)]">{t("left")}</span>
          </Ring>
          <div className="min-w-0 flex-1">
            <p className="truncate text-lg font-bold">{featured.label}</p>
            <p className="mt-1 text-sm text-[var(--hint)]">
              {left === null ? t("never_expires") : t("days_left", { days: money(left, locale) })}
            </p>
            {featured.location && <p className="mt-1 text-sm text-[var(--hint)]">{featured.location}</p>}
            <p className="mt-3 text-sm font-semibold text-[var(--accent)]">{t("manage_service")}</p>
          </div>
        </button>
      ) : (
        <Panel className="mb-4 py-8 text-center">
          <p className="text-lg font-bold">{t("no_service_yet")}</p>
          <p className="mx-auto mt-1 max-w-xs text-sm text-[var(--hint)]">{t("no_service_hint")}</p>
        </Panel>
      )}

      <div className="mb-4 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={() => navigate("/wallet")}
          className="rounded-3xl bg-[var(--surface)] p-4 text-start"
        >
          <span className="app-mini-icon" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7h16v12H3zM3 7l12-3v3M15 13h4" /></svg>
          </span>
          <p className="mt-3 text-xs text-[var(--hint)]">{t("balance")}</p>
          <p className="num mt-1 text-xl font-extrabold">{money(me.balance, locale)}</p>
          <p className="text-xs text-[var(--hint)]">{t("currency")}</p>
        </button>
        <button
          type="button"
          onClick={() => navigate("/services")}
          className="rounded-3xl bg-[var(--surface)] p-4 text-start"
        >
          <span className="app-mini-icon" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5" /></svg>
          </span>
          <p className="mt-3 text-xs text-[var(--hint)]">{t("services_count")}</p>
          <p className="num mt-1 text-xl font-extrabold">{services && !error ? money(services.length, locale) : "—"}</p>
          <p className="text-xs text-[var(--hint)]">{t("view_all")}</p>
        </button>
      </div>

      <div className="flex flex-col gap-3">
        <Button onClick={() => navigate("/buy")}>{t("buy_plan")}</Button>
        {me.trial.available && (
          <Button tone="quiet" busy={trialBusy} onClick={takeTrial}>
            {t("take_trial", { days: me.trial.days, gb: me.trial.gb })}
          </Button>
        )}
      </div>
    </Screen>
  );
}
