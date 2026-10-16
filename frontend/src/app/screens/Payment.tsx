import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { money } from "@shared/format";
import { api, type CryptoInstructions } from "../api";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { translator } from "../receipts/copy";
import { ReceiptFlow } from "../receipts/ReceiptFlow";
import { Panel, Screen } from "../ui";

interface Account {
  number: string;
  holder: string;
  bank: string;
}

const STORAGE_KEY = "pauru.payment";

interface Remembered {
  paymentId: number;
  amount: number;
  accounts?: Account[];
  crypto?: CryptoInstructions;
}

/** Keep what the customer needs to pay (card numbers or a crypto address) across the screen change. */
export function rememberPayment(value: Remembered): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // storage may be unavailable inside some Telegram clients
  }
}

export function rememberCard(paymentId: number, accounts: Account[], amount: number): void {
  rememberPayment({ paymentId, accounts, amount });
}

function recall(paymentId: number): Remembered | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const data = raw ? (JSON.parse(raw) as Remembered) : null;
    return data && data.paymentId === paymentId ? data : null;
  } catch {
    return null;
  }
}

export function Payment() {
  const { t, locale, toast, reload } = useApp();
  const navigate = useNavigate();
  const { id } = useParams();
  const paymentId = Number(id);
  const remembered = recall(paymentId);
  const [status, setStatus] = useState<string>("unknown");
  const [amount, setAmount] = useState(remembered?.amount ?? 0);
  const [provider, setProvider] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const result = await api.payment(paymentId);
        if (!alive) return;
        setStatus(result.status);
        setProvider(result.provider);
        setAmount(result.amount);
        if (result.status === "paid" && result.fulfilled) {
          haptic("success");
          await reload();
          const order = result.order_id;
          navigate(order ? `/done/${order}` : "/wallet", { replace: true });
          return;
        }
      } catch {
        // keep polling; transient network errors are common on mobile
      }
      if (alive) window.setTimeout(poll, 4000);
    };
    void poll();
    return () => {
      alive = false;
    };
  }, [paymentId, navigate, reload]);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      haptic("select");
      toast(t("copied"));
    } catch {
      toast(text);
    }
  };

  const waiting = status === "verifying";
  const closed = ["failed", "expired", "disputed", "refunded"].includes(status);
  const crypto = remembered?.crypto;

  if (crypto) {
    return (
      <Screen title={t("crypto_payment", { network: crypto.label })}>
        <Panel className="mb-4 text-center">
          <p className="text-sm text-[var(--hint)]">{t("crypto_send_exactly")}</p>
          <button type="button" onClick={() => copy(crypto.amount)} className="mt-1 w-full">
            <p dir="ltr" className="num text-3xl font-extrabold">{crypto.amount}</p>
            <p className="text-sm text-[var(--hint)]">{crypto.label}</p>
          </button>
          <p className="mt-2 text-xs text-[var(--hint)]">
            {t("crypto_equals", { amount: money(amount, locale) })}
          </p>
        </Panel>
        <button type="button" onClick={() => copy(crypto.address)}
          className="mb-3 w-full rounded-3xl bg-[var(--surface)] p-4 text-start">
          <p className="text-xs text-[var(--hint)]">{t("crypto_address")}</p>
          <p dir="ltr" className="mt-1 break-all font-mono text-sm font-semibold">{crypto.address}</p>
          <p className="mt-1 text-xs font-semibold text-[var(--accent)]">{t("tap_to_copy")}</p>
        </button>
        {crypto.memo && (
          <button type="button" onClick={() => copy(crypto.memo ?? "")}
            className="mb-3 w-full rounded-3xl bg-saffron-100 p-4 text-start text-saffron-700">
            <p className="text-xs">{t("crypto_memo")}</p>
            <p dir="ltr" className="mt-1 font-mono text-lg font-bold">{crypto.memo}</p>
          </button>
        )}
        {closed ? (
          <Panel className="text-center text-anar-700">{t(`payment_${status}`)}</Panel>
        ) : (
          <Panel className="text-center">
            <p className="font-bold">{waiting ? t("receipt_under_review") : t("crypto_waiting")}</p>
            <p className="mt-1 text-sm text-[var(--hint)]">{t("crypto_waiting_hint")}</p>
          </Panel>
        )}
      </Screen>
    );
  }

  if (provider === "card" || remembered?.accounts) {
    return <Screen title={t("card_payment")}><ReceiptFlow key={paymentId} paymentId={paymentId} accounts={remembered?.accounts} checkoutAmount={remembered?.amount} /></Screen>;
  }

  if (!provider) {
    return <Screen title={t("payment_status")}><Panel>{translator(locale)("error")}</Panel></Screen>;
  }

  if (!remembered?.accounts) {
    return (
      <Screen title={t("payment_status")}>
        <Panel className="text-center">
          {closed ? (
            <p className="font-bold text-anar-700">{t(`payment_${status}`)}</p>
          ) : (
            <>
              <p className="font-bold">{t("gateway_waiting")}</p>
              <p className="mt-1 text-sm text-[var(--hint)]">{t("gateway_waiting_hint")}</p>
            </>
          )}
        </Panel>
      </Screen>
    );
  }

  return null;
}
