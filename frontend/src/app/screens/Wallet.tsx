import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dateTime, money } from "@shared/format";
import { api, type HistoryItem } from "../api";
import { Icon } from "../icons";
import { errorText, useApp } from "../state";
import { haptic, openExternal, openInvoice } from "../telegram";
import { Button, CountUp, Item, Screen } from "../ui";
import { rememberCard, rememberPayment } from "./Payment";

const PRESETS = [50_000, 100_000, 200_000, 500_000];

export function Wallet() {
  const { me, t, locale, toast, reload } = useApp();
  const navigate = useNavigate();
  const [history, setHistory] = useState<HistoryItem[] | null>(null);
  const [amount, setAmount] = useState<number | null>(null);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [gift, setGift] = useState("");

  useEffect(() => {
    api.history().then((r) => setHistory(r.items));
  }, []);
  useEffect(() => {
    if (!me || amount !== null || custom !== "") return;
    const [low, high] = me.topup_bounds;
    setAmount(PRESETS.find((p) => p >= low && p <= high) ?? low);
  }, [me, amount, custom]);
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
      } else if (payment.crypto) {
        rememberPayment({ paymentId: payment.payment_id, amount: payment.amount, crypto: payment.crypto });
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

  const redeem = async () => {
    if (!gift.trim()) return;
    setBusy("gift");
    try {
      const result = await api.redeemGift(gift.trim());
      haptic("success");
      setGift("");
      toast(t("gift_redeemed", { amount: money(result.amount, locale) }));
      await reload();
      api.history().then((r) => setHistory(r.items));
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Screen
      title={t("wallet")}
      action={
        <button
          type="button"
          className="app-iconbtn app-rip-host"
          aria-label={t("receipts_title")}
          onClick={() => navigate("/receipts")}
        >
          <Icon name="recv" size={20} />
        </button>
      }
    >
      <div className="app-wcard">
        <p className="app-wcard-label">{t("home_wallet")}</p>
        <div className="app-wcard-amt num">
          <CountUp value={me.balance} format={(value) => money(value, locale)} /> <small>{t("currency")}</small>
        </div>
      </div>

      <p className="app-lbl">{t("topup_amount")}</p>
      <div className="app-chips" role="group">
        {presets.map((preset) => (
          <button
            key={preset}
            type="button"
            aria-pressed={amount === preset}
            className="app-chip num"
            onClick={() => {
              setAmount(preset);
              setCustom("");
              haptic("select");
            }}
          >
            {money(preset, locale)}
          </button>
        ))}
      </div>
      <input
        aria-label={t("custom_amount", { low: money(low, locale), high: money(high, locale) })}
        inputMode="numeric"
        value={custom}
        onChange={(e) => {
          setCustom(e.target.value);
          setAmount(null);
        }}
        placeholder={t("custom_amount", { low: money(low, locale), high: money(high, locale) })}
        className="num mb-4 w-full"
      />

      <p className="app-lbl">{t("topup_method")}</p>
      <div className="app-card">
        {me.payment_methods.map((method) => (
          <Item
            key={method}
            icon={<Icon name={method === "card" ? "card" : "coin"} />}
            tone={method === "card" ? undefined : "green"}
            title={t(`method_${method}`)}
            sub={method === "card" ? t("method_card_sub") : undefined}
            onClick={() => topup(method)}
            disabled={!valid || (busy !== null && busy !== method)}
          />
        ))}
        {me.payment_methods.length === 0 && <div className="app-empty">{t("no_methods")}</div>}
      </div>

      <p className="app-lbl">{t("gift_code")}</p>
      <div className="app-dc">
        <input
          aria-label={t("gift_code")}
          value={gift}
          onChange={(e) => setGift(e.target.value)}
          placeholder={t("gift_code_placeholder")}
          dir="ltr"
          maxLength={32}
        />
        <Button tone="quiet" className="w-auto px-5" busy={busy === "gift"} disabled={!gift.trim()} onClick={() => void redeem()}>
          {t("redeem")}
        </Button>
      </div>

      <p className="app-lbl mt-6">{t("transactions")}</p>
      <div className="app-card">
        {history === null ? null : history.length === 0 ? (
          <div className="app-empty">{t("no_transactions")}</div>
        ) : (
          history.map((item) => (
            <div key={item.id} className="app-row flex items-center gap-3 px-4">
              <span className="app-ico" data-tone={item.amount > 0 ? "green" : undefined}>
                <Icon name={item.amount > 0 ? "coin" : "wallet"} size={21} />
              </span>
              <span className="app-grow">
                <b className="block text-[0.93rem] font-bold leading-7">{t(`tx_${item.kind}`)}</b>
                <small className="num mt-0.5 block text-[0.75rem] font-medium text-[var(--hint)]">
                  {dateTime(item.created_at, locale)}
                </small>
              </span>
              <b className={`num text-[0.93rem] font-bold ${item.amount > 0 ? "text-pista-700" : ""}`} dir="ltr">
                {item.amount > 0 ? "+" : "−"}
                {money(Math.abs(item.amount), locale)}
              </b>
            </div>
          ))
        )}
      </div>
    </Screen>
  );
}
