import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "@shared/http";
import { money } from "@shared/format";
import { api } from "../api";
import { Icon } from "../icons";
import { PendingPayments } from "../payments/PendingPayments";
import { errorText, useApp } from "../state";
import { haptic, openExternal, openInvoice } from "../telegram";
import { actionGate, topupIntents } from "../topup-intents";
import { Button, CountUp, Item, Screen } from "../ui";
import { rememberCard, rememberPayment } from "./Payment";

import { WalletTransfer } from "./WalletTransfer";
import { MyDiscounts } from "./MyDiscounts";
import { WalletHistory } from "./WalletHistory";

const PRESETS = [50_000, 100_000, 200_000, 500_000];

export function Wallet() {
  const { me, t, locale, toast, reload } = useApp();
  const navigate = useNavigate();
  const [historyVersion, setHistoryVersion] = useState(0);
  const [amount, setAmount] = useState<number | null>(null);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [gift, setGift] = useState("");
  const gate = useRef(actionGate());

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
    if (!valid || chosen === null || !gate.current.enter()) return;
    setBusy(method);
    let intent: string | undefined;
    try {
      intent = topupIntents().get(me.id, chosen, method);
      const { payment } = await api.topup(chosen, method, intent);
      if (!payment.uncertain) topupIntents().clear(me.id, chosen, method, intent);
      if (payment.provider === "tonpay_custom") {
        navigate(`/payment/${payment.payment_id}`);
      } else if (payment.accounts) {
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
      // A network/5xx error is uncertain: retry the SAME key, never a fresh invoice.
      // A closed/conflicting intent needs another deliberate click, not an automatic retry.
      if (intent && err instanceof ApiError && err.status === 409 &&
          ["payment_intent_closed", "payment_intent_conflict"].includes(err.code)) {
        topupIntents().clear(me.id, chosen, method, intent);
      }
      if (err instanceof ApiError && err.code === "card_verify_required") navigate("/card-verification");
      toast(errorText(t, err), "error");
    } finally {
      gate.current.leave();
      setBusy(null);
    }
  };

  const redeem = async () => {
    if (!gift.trim() || !gate.current.enter()) return;
    setBusy("gift");
    try {
      const result = await api.redeemGift(gift.trim());
      haptic("success");
      setGift("");
      toast(t("gift_redeemed", { amount: money(result.amount, locale) }));
      // Refresh only the read: a history failure must never replay the gift.
      setHistoryVersion(value => value + 1);
      await reload();
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      gate.current.leave();
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

      <Button disabled={busy !== null} onClick={() => navigate("/card-verification")}>{locale.startsWith("fa") ? "تأیید کارت" : "Card verification"}</Button>
      <p className="app-lbl">{t("topup_amount")}</p>
      <div className="app-chips" role="group">
        {presets.map((preset) => (
          <button
            key={preset}
            type="button"
            aria-pressed={amount === preset}
            disabled={busy !== null}
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
        disabled={busy !== null}
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
        {[...me.payment_methods, ...(me.tonpay_custom_available ? ["tonpay_custom"] : [])].map((method) => (
          <Item
            key={method}
            icon={<Icon name={method === "card" ? "card" : "coin"} />}
            tone={method === "card" ? undefined : "green"}
            title={method === "tonpay_custom" ? (locale === "fa" ? "کارت به کارت تون‌پی" : "Tonpay card transfer") : t(`method_${method}`)}
            sub={method === "card" ? t("method_card_sub") : undefined}
            onClick={() => topup(method)}
            disabled={!valid || busy !== null}
          />
        ))}
        {me.payment_methods.length === 0 && !me.tonpay_custom_available && <div className="app-empty">{t("no_methods")}</div>}
      </div>

      <div className="mt-6"><PendingPayments /></div>
      <p className="app-lbl">{t("gift_code")}</p>
      <div className="app-dc">
        <input
          aria-label={t("gift_code")}
          value={gift}
          disabled={busy !== null}
          onChange={(e) => setGift(e.target.value)}
          placeholder={t("gift_code_placeholder")}
          dir="ltr"
          maxLength={32}
        />
        <Button tone="quiet" className="w-auto px-5" busy={busy === "gift"} disabled={!gift.trim() || busy !== null} onClick={() => void redeem()}>
          {t("redeem")}
        </Button>
      </div>

      <WalletTransfer key={me.id} owner={me.id} disabled={busy !== null}
        acquire={() => { if (!gate.current.enter()) return false; setBusy("transfer"); return true; }}
        release={() => { gate.current.leave(); setBusy(null); }}
        onDone={() => { setHistoryVersion(value => value + 1); void reload().catch(() => toast(t("retry"), "error")); }}
      />
      <p className="app-lbl mt-6">{t("transactions")}</p>
      <MyDiscounts key={`discounts-${me.id}`} />
      <WalletHistory key={`${me.id}:${historyVersion}`} />
    </Screen>
  );
}
