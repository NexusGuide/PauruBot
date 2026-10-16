import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { date, money } from "@shared/format";
import { api, type ResellerDesk } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Empty, Loading, Panel, Screen, StatusDot } from "../ui";

export function Desk() {
  const { me, t, locale, reload, toast } = useApp();
  const navigate = useNavigate();
  const [days, setDays] = useState(3);
  const [data, setData] = useState<ResellerDesk | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .resellerDesk(days)
      .then((d) => {
        setData(d);
        setFailed(null);
      })
      .catch((err) => setFailed(errorText(t, err)));
  }, [days, t]);
  useEffect(load, [load]);

  if (!me) return null;
  if (failed) {
    return (
      <Screen title={t("desk")}>
        <Panel className="text-center">{failed}</Panel>
      </Screen>
    );
  }
  if (!data) return <Loading />;

  const needle = q.trim().toLowerCase();
  const items = data.expiring.filter((s) => !needle || `${s.label ?? ""} ${s.panel_username}`.toLowerCase().includes(needle));
  const renewable = items.filter((s) => s.renew_price !== null);
  const total = renewable.reduce((sum, s) => sum + (s.renew_price ?? 0), 0);

  const renewAll = async () => {
    setBusy(true);
    try {
      const result = await api.renewMany(renewable.map((s) => s.id));
      haptic(result.failed.length ? "error" : "success");
      toast(
        t("desk_renewed", { ok: money(result.renewed.length, locale), failed: money(result.failed.length, locale) }),
        result.failed.length ? "error" : "ok",
      );
      await reload();
      load();
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  const figures: [string, string][] = [
    ["desk_active", money(data.figures.active, locale)],
    ["desk_expiring", money(data.figures.expiring, locale)],
    ["desk_month_orders", money(data.figures.month_orders, locale)],
    ["desk_month_amount", money(data.figures.month_amount, locale)],
  ];

  return (
    <Screen title={t("desk")}>
      <div className="mb-4 grid grid-cols-2 gap-3">
        {figures.map(([key, value]) => (
          <Panel key={key}>
            <p className="text-xs text-[var(--hint)]">{t(key)}</p>
            <p className="num mt-1 text-xl font-extrabold">{value}</p>
          </Panel>
        ))}
      </div>
      <div className="mb-4">
        <Button onClick={() => navigate("/buy")}>{t("desk_sell")}</Button>
      </div>
      <div className="mb-3 flex items-center gap-2">
        {[3, 7, 30].map((d) => (
          <button
            key={d}
            aria-pressed={d === days}
            type="button"
            onClick={() => setDays(d)}
            className={`rounded-full px-3 py-1.5 text-sm ${d === days ? "bg-[var(--accent)] font-bold text-[var(--accent-ink)]" : "bg-[var(--surface)]"}`}
          >
            {t("desk_within_days", { days: money(d, locale) })}
          </button>
        ))}
      </div>
      <input
        aria-label={t("desk_search")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={t("desk_search")}
        className="mb-4 min-h-12 w-full rounded-2xl bg-[var(--surface)] px-4 text-[var(--text)] outline-none ring-1 ring-[var(--line)] focus:ring-[var(--accent)]"
      />
      {items.length === 0 ? (
        <Empty title={t("desk_nothing")} />
      ) : (
        <>
          <ul className="mb-4 flex flex-col gap-2">
            {items.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => navigate(`/services/${s.id}`)}
                  className="flex w-full items-center gap-3 rounded-2xl bg-[var(--surface)] px-4 py-3 text-start active:opacity-80"
                >
                  <StatusDot status={s.status} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-bold">{s.label || s.panel_username}</span>
                    <span className="block text-xs text-[var(--hint)]">
                      {s.expire_at ? date(s.expire_at, locale) : "∞"}
                      {s.gb_left !== null ? ` · ${t("desk_gb_left", { gb: money(s.gb_left, locale) })}` : ""}
                    </span>
                  </span>
                  <span className="num text-sm">{s.renew_price !== null ? money(s.renew_price, locale) : "—"}</span>
                </button>
              </li>
            ))}
          </ul>
          {renewable.length > 0 && (
            <Button busy={busy} onClick={renewAll}>
              {t("desk_renew_all", { n: money(renewable.length, locale), total: money(total, locale) })}
            </Button>
          )}
          {total > me.balance && <p className="mt-2 text-center text-sm text-anar-700">{t("desk_low_balance")}</p>}
        </>
      )}
    </Screen>
  );
}
