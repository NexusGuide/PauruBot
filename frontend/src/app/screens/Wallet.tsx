import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dateTime, money } from "@shared/format";
import { api, type HistoryItem } from "../api";
import { errorText, useApp } from "../state";
import { haptic, openExternal, openInvoice } from "../telegram";
import { Button, Panel, Screen } from "../ui";
import { rememberCard } from "./Payment";

const PRESETS = [50_000, 100_000, 200_000, 500_000];

export function Wallet() {
  const { me, t, locale, toast, reload } = useApp();
  const navigate = useNavigate();
  const [history, setHistory] = useState<HistoryItem[] | null>(null);
  const [amount, setAmount] = useState<number | null>(null);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    api.history().then((r) => setHistory(r.items));
  }, []);
  if (!me) return null;
  const [low, high] = me.topup_bounds;
  const presets = PRESETS.filter((p) => p >= low && p <= high);
  const chosen = amount ?? (Number(custom.replace(/\D/g, "")) || null);
  const valid = chosen !== null && chosen >= low && chosen <= high;

  const topup = async (method: string) => {
    if (!valid || chosen === null) return;
    setBusy(method);
    try {
      const { payment } = await api.topup(chosen, method);
      if (payment.accounts) {
        rememberCard(payment.payment_id, payment.accounts, payment.amount);
        navigate(`/payment/${payment.payment_id}`);
      } else if (payment.redirect_url) {
        openExternal(payment.redirect_url);
        navigate(`/payment/${payment.payment_id}`);
      } else if (payment.invoice_link) {
        const status = await openInvoice(payment.invoice_link);
        if (status === "paid") {
          haptic("success");
          await reload();
          toast(t("topped_up"));
        }
      }
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Screen title={t("wallet")}>
      <Panel className="mb-5 text-center">
        <p className="text-sm text-[var(--hint)]">{t("balance")}</p>
        <p className="num mt-1 text-4xl font-extrabold">{money(me.balance, locale)}</p>
        <p className="text-sm text-[var(--hint)]">{t("currency")}</p>
      </Panel>

      <h2 className="mb-3 text-lg font-bold">{t("top_up")}</h2>
      <div className="mb-3 grid grid-cols-2 gap-2">
        {presets.map((p) => (
          <button key={p} type="button" onClick={() => { setAmount(p); setCustom(""); haptic("select"); }}
            className={`num rounded-2xl py-3 font-bold ${amount === p ? "bg-[var(--accent)] text-white" : "bg-[var(--surface)]"}`}>
            {money(p, locale)}
          </button>
        ))}
      </div>
      <input inputMode="numeric" value={custom}
        onChange={(e) => { setCustom(e.target.value); setAmount(null); }}
        placeholder={t("custom_amount", { low: money(low, locale), high: money(high, locale) })}
        className="num mb-3 min-h-12 w-full rounded-2xl bg-[var(--surface)] px-4 outline-none ring-1 ring-[var(--line)] focus:ring-[var(--accent)]" />
      <div className="mb-8 flex flex-col gap-2">
        {me.payment_methods.map((method, index) => (
          <Button key={method} tone={index === 0 ? "primary" : "quiet"} disabled={!valid} busy={busy === method}
            onClick={() => topup(method)}>
            {t(`method_${method}`)}
          </Button>
        ))}
        {me.payment_methods.length === 0 && <p className="text-center text-sm text-[var(--hint)]">{t("no_methods")}</p>}
      </div>

      <h2 className="mb-3 text-lg font-bold">{t("transactions")}</h2>
      {history && history.length === 0 && <p className="text-sm text-[var(--hint)]">{t("no_transactions")}</p>}
      <ul className="overflow-hidden rounded-3xl bg-[var(--surface)]">
        {history?.map((item, index) => (
          <li key={item.id} className={`flex items-center justify-between px-4 py-3 ${index ? "border-t border-[var(--line)]" : ""}`}>
            <div>
              <p className="text-sm font-semibold">{t(`tx_${item.kind}`)}</p>
              <p className="num text-xs text-[var(--hint)]">{dateTime(item.created_at, locale)}</p>
            </div>
            <p className={`num font-bold ${item.amount > 0 ? "text-pista-700" : ""}`} dir="ltr">
              {item.amount > 0 ? "+" : "−"}{money(Math.abs(item.amount), locale)}
            </p>
          </li>
        ))}
      </ul>
    </Screen>
  );
}
