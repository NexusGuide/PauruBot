import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { money } from "@shared/format";
import { api, type PendingPaymentList, type PendingPayment } from "../api";
import { errorText, useApp } from "../state";
import { Icon } from "../icons";
import { actionGate } from "../topup-intents";
import { openExternal } from "../telegram";
import { Button, Item, Panel } from "../ui";
import { rememberPayment } from "../screens/Payment";
import { resumeSavedPayment } from "./resume";

export function PendingPayments() {
  const { t, locale, toast, me } = useApp();
  const navigate = useNavigate();
  const [page, setPage] = useState<PendingPaymentList | null>(null);
  const [cursor, setCursor] = useState<number | undefined>();
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const gate = useRef(actionGate());
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, [me?.id]);
  const refreshPage = useCallback(() => setRefresh((n) => n + 1), []);
  useEffect(() => {
    let active = true;
    setPage(null);
    setFailed(null);
    setReading(true);
    api.pendingPayments(cursor).then((result) => { if (active) setPage(result); })
      .catch((err) => { if (active) setFailed(errorText(t, err)); })
      .finally(() => { if (active) setReading(false); });
    return () => { active = false; };
  }, [cursor, refresh, t, me?.id]);

  const choose = async (row: PendingPayment) => {
    if (!gate.current.enter()) return;
    setBusy(true);
    const current = generation.current;
    try {
      if (row.action === "review") { navigate("/tickets"); return; }
      if (row.action === "status") { navigate(`/payment/${row.id}`); return; }
      await resumeSavedPayment(row.id, {
        read: async (id) => {
          const result = await api.resumePayment(id);
          if (generation.current !== current) throw new Error("stale_resume");
          return result;
        },
        remember: rememberPayment,
        open: openExternal,
        navigate,
      });
    } catch (err) {
      if (generation.current !== current) return;
      toast(errorText(t, err), "error");
      refreshPage();
    } finally {
      gate.current.leave();
      setBusy(false);
    }
  };

  return <section aria-label={t("pending_payments")} aria-busy={reading || busy}>
    <p className="app-lbl">{t("pending_payments")}</p>
    <p className="app-hint mb-3 text-sm">{t("pending_hint")}</p>
    <Button tone="quiet" disabled={busy || reading} onClick={refreshPage}>{t("pending_refresh")}</Button>
    {reading && <Panel>{t("ui_loading")}</Panel>}
    {failed && <Panel><p role="alert">{failed}</p><Button disabled={busy || reading} onClick={refreshPage}>{t("ui_retry")}</Button></Panel>}
    {page && <div className="app-card mt-3">
      {page.items.length === 0 ? <div className="app-empty">{t("pending_empty")}</div> : page.items.map((row) =>
        <Item key={row.id} icon={<Icon name="coin" />} title={`${t(`method_${row.provider}`)} · ${money(row.amount, locale)} ${t("currency")} #${row.id}`}
          sub={t(`pending_${row.action}`)} disabled={busy || reading} onClick={() => void choose(row)} />)}
    </div>}
    <div className="mt-3 flex gap-3">
      {cursor && <Button tone="quiet" disabled={busy || reading} onClick={() => setCursor(undefined)}>{t("pending_latest")}</Button>}
      {page?.next_before && <Button tone="quiet" disabled={busy || reading} onClick={() => setCursor(page.next_before ?? undefined)}>{t("pending_older")}</Button>}
    </div>
  </section>;
}
