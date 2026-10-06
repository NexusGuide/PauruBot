import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { api, type CryptoInstructions } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Panel, Screen } from "../ui";

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
  const [params] = useSearchParams();
  const paymentId = Number(id);
  const remembered = recall(paymentId);
  const [status, setStatus] = useState<string>("pending");
  const [amount, setAmount] = useState(remembered?.amount ?? 0);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const result = await api.payment(paymentId);
        if (!alive) return;
        setStatus(result.status);
        setAmount(result.amount);
        if (result.status === "paid" && result.fulfilled) {
          haptic("success");
          await reload();
          const order = params.get("order");
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
  }, [paymentId, navigate, params, reload]);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      haptic("select");
      toast(t("copied"));
    } catch {
      toast(text);
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    try {
      await api.uploadReceipt(paymentId, file);
      haptic("success");
      setStatus("verifying");
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setUploading(false);
    }
  };

  const waiting = status === "verifying";
  const closed = ["failed", "expired", "disputed"].includes(status);
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

  return (
    <Screen title={t("card_payment")}>
      <Panel className="mb-4 text-center">
        <p className="text-sm text-[var(--hint)]">{t("transfer_exactly")}</p>
        <p className="num mt-1 text-3xl font-extrabold">{money(amount, locale)}</p>
        <p className="text-sm text-[var(--hint)]">{t("currency")}</p>
      </Panel>

      {remembered && !waiting && !closed && (
        <ul className="mb-4 flex flex-col gap-3">
          {remembered.accounts.map((account) => (
            <li key={account.number}>
              <button type="button" onClick={() => copy(account.number)}
                className="w-full rounded-3xl bg-[var(--surface)] p-4 text-start">
                <p dir="ltr" className="num text-center text-xl font-bold tracking-wider">
                  {account.number.replace(/(\d{4})(?=\d)/g, "$1 ")}
                </p>
                <p className="mt-2 text-center text-sm text-[var(--hint)]">
                  {[account.holder, account.bank].filter(Boolean).join(" · ")}
                </p>
                <p className="mt-1 text-center text-xs font-semibold text-[var(--accent)]">{t("tap_to_copy")}</p>
              </button>
            </li>
          ))}
        </ul>
      )}

      {closed ? (
        <Panel className="text-center text-anar-700">{t(`payment_${status}`)}</Panel>
      ) : waiting ? (
        <Panel className="text-center">
          <p className="font-bold">{t("receipt_under_review")}</p>
          <p className="mt-1 text-sm text-[var(--hint)]">{t("receipt_under_review_hint")}</p>
        </Panel>
      ) : (
        <>
          <input ref={fileInput} type="file" accept="image/*" className="hidden"
            onChange={(e) => void upload(e.target.files?.[0])} />
          <Button busy={uploading} onClick={() => fileInput.current?.click()}>
            {t("upload_receipt")}
          </Button>
          <p className="mt-3 text-center text-xs text-[var(--hint)]">{t("receipt_hint")}</p>
        </>
      )}
    </Screen>
  );
}
