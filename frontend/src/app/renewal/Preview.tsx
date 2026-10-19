import { useEffect, useState } from "react";
import { dateTime, money } from "@shared/format";
import { api, type Order } from "../api";
import { useApp } from "../state";
import { Button, Panel } from "../ui";
import { decodePreview, fresh, type Preview, type Snapshot } from "./model";

export function RenewalPreview({ order, onReady }: { order: Order; onReady: (id: number | null) => void }) {
  const { t, locale } = useApp();
  const [data, setData] = useState<Preview | null>(null);
  const [failed, setFailed] = useState(false);
  const [expired, setExpired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    onReady(null); setData(null); setLoading(true); setFailed(false); setExpired(false);
    api.renewalPreview(order.id, abort.signal).then((raw) => {
      if (!active) return;
      const value = decodePreview(raw, order);
      if (!fresh(value)) throw Error("renewal_preview_expired");
      setData(value); onReady(order.id);
      timer = setTimeout(() => {
        if (active) { onReady(null); setExpired(true); }
      }, Math.min(60_000, Date.parse(value.valid_until) - Date.now()));
    }).catch(() => { if (active) { setFailed(true); onReady(null); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; abort.abort(); clearTimeout(timer); onReady(null); };
  }, [order.id, order.service_id, order.plan_id, order.amount, onReady, retry]);
  const value = data?.order_id === order.id ? data : null;
  const bytes = (n: number) => `${money(n / 1024 ** 3, locale)} ${t("gb")}`;
  const view = (s: Snapshot, title: string) => (
    <section className="mt-3" aria-label={title}>
      <h3 className="font-semibold">{title}</h3>
      <dl className="mt-2 space-y-2">
        <div className="app-ln"><dt>{t("expires")}</dt><dd>{s.expire_at ? dateTime(s.expire_at, locale) : t("never_expires")}</dd></div>
        <div className="app-ln"><dt>{t("renew_preview_limit")}</dt><dd>{s.data_limit === 0 ? t("unlimited_data") : bytes(s.data_limit)}</dd></div>
        <div className="app-ln"><dt>{t("used")}</dt><dd>{bytes(s.used_bytes)}</dd></div>
        <div className="app-ln"><dt>{t("data_left_label")}</dt><dd>{s.remaining_bytes === null ? t("unlimited_data") : bytes(s.remaining_bytes)}</dd></div>
      </dl>
    </section>
  );
  return <Panel className="mt-4">
    <h2 className="font-semibold">{t("renew_preview_title")}</h2>
    <div aria-live="polite">
      {loading && <p>{t("ui_loading")}</p>}
      {failed && <p role="alert">{t("renew_preview_failed")}</p>}
      {expired && <p role="alert">{t("renew_preview_expired")}</p>}
      {value && <>
        <p className="app-hint mt-2 text-sm">{t("renew_preview_basis", { date: dateTime(value.computed_at, locale) })}</p>
        {value.timing === "immediate" && <>
          <p className="mt-2">{t(`renew_preview_time_${value.policy.time}`)}</p>
          <p>{t(`renew_preview_volume_${value.policy.volume}`)}</p>
          <p>{t(value.resets_usage ? "renew_preview_reset" : "renew_preview_keep_usage")}</p>
        </>}
        {view(value.before, t("renew_preview_before"))}
        {value.after ? view(value.after, t("renew_preview_after")) : <p className="mt-3">{t("renew_preview_queued", { date: dateTime(value.queued_until!, locale) })}</p>}
        <p className="app-hint mt-3 text-sm">{t("renew_preview_notice")}</p>
      </>}
    </div>
    <Button tone="quiet" className="mt-3" disabled={loading} onClick={() => setRetry(n => n + 1)}>{t("renew_preview_refresh")}</Button>
  </Panel>;
}
