import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { api } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Panel, Screen } from "../ui";

interface Account {
  number: string;
  holder: string;
  bank: string;
}

const STORAGE_KEY = "pauru.card-payment";

export function rememberCard(paymentId: number, accounts: Account[], amount: number): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ paymentId, accounts, amount }));
  } catch {
    // storage may be unavailable inside some Telegram clients
  }
}

function recall(paymentId: number): { accounts: Account[]; amount: number } | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const data = raw ? (JSON.parse(raw) as { paymentId: number; accounts: Account[]; amount: number }) : null;
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
