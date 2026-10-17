import { useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { api } from "../api";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { ActionBar, Button, Overlay, Trail } from "../ui";
import { translator, type CopyKey } from "./copy";
import {
  canSend,
  checkoutAccounts,
  confirmUpload,
  parseReceipt,
  validateFile,
  type ReceiptState,
} from "./model";

export function ReceiptFlow({
  paymentId,
  accounts,
  checkoutAmount,
}: {
  paymentId: number;
  accounts?: unknown;
  checkoutAmount?: number;
}) {
  const { locale, t, toast } = useApp(),
    c = translator(locale);
  const [state, setState] = useState<ReceiptState | null>(null);
  const [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<CopyKey | null>(null),
    [blocked, setBlocked] = useState(true);
  const [file, setFile] = useState<File | null>(null),
    [preview, setPreview] = useState<string | null>(null);
  const [ack, setAck] = useState(false),
    [now, setNow] = useState(Date.now());
  const [overlay, setOverlay] = useState(false);
  const alive = useRef(true),
    inFlight = useRef(false),
    reading = useRef(false),
    overlayTimer = useRef(0);
  useEffect(() => {
    alive.current = true;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      window.clearTimeout(overlayTimer.current);
    };
  }, []);
  useEffect(() => {
    if (!file) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const refresh = async () => {
    if (inFlight.current || reading.current) return;
    reading.current = true;
    setLoading(true);
    setBlocked(true);
    setAck(false);
    setFile(null);
    try {
      const value = parseReceipt(await api.receiptState(paymentId), paymentId);
      if (!alive.current) return;
      setState(value);
      setBlocked(false);
      setMessage(null);
    } catch {
      if (alive.current) setMessage("error");
    } finally {
      reading.current = false;
      if (alive.current) setLoading(false);
    }
  };
  useEffect(() => {
    void refresh();
  }, [paymentId]); // Parent keys this component by payment ID.
  const send = async () => {
    if (
      inFlight.current ||
      reading.current ||
      blocked ||
      !state ||
      !file ||
      !ack ||
      !validateFile(file) ||
      !canSend(state)
    )
      return;
    inFlight.current = true;
    setBusy(true);
    setBlocked(true);
    setAck(false);
    const before = state;
    try {
      const value = confirmUpload(
        await api.sendReceipt(paymentId, file, before.revision),
        before,
      );
      if (!alive.current) return;
      setState(value);
      setFile(null);
      setMessage("saved"); // Actual paid / closed / review result, never optimistic review.
      if (value.phase === "under_review") {
        setOverlay(true);
        window.clearTimeout(overlayTimer.current);
        overlayTimer.current = window.setTimeout(() => {
          if (alive.current) setOverlay(false);
        }, 2200);
      }
    } catch {
      if (alive.current) {
        setFile(null);
        setMessage("uncertain");
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      haptic("select");
      toast(t("copied"));
    } catch {
      toast(text);
    }
  };
  const group = (digits: string) => digits.replace(/\d{4}(?=\d)/g, "$& ");
  const activeStep = !state
    ? 0
    : state.phase === "awaiting_receipt"
      ? 1
      : state.phase === "paid" || state.phase === "closed"
        ? 3
        : 2;
  const destinations = state ? checkoutAccounts(accounts, checkoutAmount, state) : [];
  return (
    <section aria-busy={loading || busy}>
      {overlay && <Overlay title={t("receipt_sent_title")} text={t("receipt_sent_text")} />}
      <Trail items={[t("step_amount"), t("step_transfer"), t("step_receipt")]} active={activeStep} />
      {message && (
        <div className="app-note" data-tone={message === "saved" ? "soft" : "danger"} role="status">
          <Icon name={message === "saved" ? "check" : "help"} size={20} />
          <span>{c(message)}</span>
        </div>
      )}
      {loading && <p role="status" className="app-hint mb-4 text-sm">{c("loading")}</p>}
      {state && (
        <div className="app-card mb-4 p-4">
          <p className="mb-2 font-bold">
            {c(
              (canSend(state, now)
                ? state.phase
                : state.can_upload
                  ? "deadline_passed"
                  : state.phase) as CopyKey,
            )}
          </p>
          <div className="app-ln">
            <span className="app-hint">{c("amount")}</span>
            <span className="num font-semibold">
              {money(state.amount, locale)} {state.currency}
            </span>
          </div>
          {state.expires_at && (
            <div className="app-ln">
              <span className="app-hint">{c("deadline")}</span>
              <time className="num font-semibold" dateTime={state.expires_at}>
                {new Date(state.expires_at).toLocaleString(locale)}
              </time>
            </div>
          )}
          {state.correction && (
            <div className="app-note mt-3" data-tone="danger">
              <Icon name="help" size={20} />
              <span className="app-grow">
                <b className="block">{c(state.correction.kind === "amount" ? "amountFix" : "cardFix")}</b>
                <span className="mt-1 block">
                  {c("note")}:{" "}
                  <span className="whitespace-pre-wrap break-words">{state.correction.note || c("noNote")}</span>
                </span>
                <span className="mt-2 block">{c("warning")}</span>
              </span>
            </div>
          )}
        </div>
      )}
      {state?.phase === "awaiting_receipt" && !blocked && (
        <div className="app-card p-4">
          <p className="app-hint text-sm">{c("initial")}</p>
          {destinations.length ? (
            destinations.map((account) => (
              <div key={account.number} className="app-cc mt-3">
                <span className="app-chip">{t("card_target")}</span>
                <b className="num select-all" dir="ltr">
                  {group(account.number)}
                </b>
                <div className="flex items-center justify-between gap-2">
                  <small>
                    {account.holder} · {account.bank}
                  </small>
                  <button
                    type="button"
                    className="app-copy app-rip-host"
                    aria-label={t("copied")}
                    onClick={() => void copy(account.number)}
                  >
                    <Icon name="copy" size={14} />
                  </button>
                </div>
              </div>
            ))
          ) : (
            <p className="mt-2 text-sm font-semibold text-[var(--danger)]">{c("missingAccounts")}</p>
          )}
        </div>
      )}
      {state && canSend(state, now) && !blocked && (
        <>
          <label className="app-up mt-4">
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp"
              disabled={busy || loading}
              className="sr-only"
              onChange={(event) => {
                const picked = event.target.files?.[0];
                setAck(false);
                if (picked && validateFile(picked)) {
                  setFile(picked);
                  setMessage(null);
                } else {
                  setFile(null);
                  setMessage("invalid");
                }
                event.target.value = "";
              }}
            />
            {preview ? <img src={preview} alt={c("preview")} /> : <Icon name="up" size={26} />}
            <span>{c("choose")}</span>
            {file && <small className="break-all">{file.name}</small>}
          </label>
          {file && (
            <>
              <label className="app-note mt-4" data-tone="soft">
                <input
                  type="checkbox"
                  checked={ack}
                  disabled={busy}
                  className="mt-1"
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>{c("ack")}</span>
              </label>
              <ActionBar
                action={c("send")}
                busy={busy}
                disabled={!ack || busy || !canSend(state, now)}
                onClick={() => void send()}
              />
            </>
          )}
        </>
      )}
      <Button tone="quiet" disabled={busy || loading} onClick={() => void refresh()}>
        {c("refresh")}
      </Button>
    </section>
  );
}
