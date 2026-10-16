import { useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { api } from "../api";
import { useApp } from "../state";
import { Button, Panel } from "../ui";
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
  const { locale } = useApp(),
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
  const alive = useRef(true),
    inFlight = useRef(false),
    reading = useRef(false);
  useEffect(() => {
    alive.current = true;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
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
  return (
    <section className="flex flex-col gap-3" aria-busy={loading || busy}>
      {message && (
        <Panel>
          <p role="status">{c(message)}</p>
        </Panel>
      )}
      {loading && <p role="status">{c("loading")}</p>}
      {state && (
        <Panel>
          <p className="font-bold">
            {c(
              (canSend(state, now)
                ? state.phase
                : state.can_upload
                  ? "deadline_passed"
                  : state.phase) as CopyKey,
            )}
          </p>
          <p>
            {c("amount")}: {money(state.amount, locale)} {state.currency}
          </p>
          {state.expires_at && (
            <p>
              {c("deadline")}:{" "}
              <time dateTime={state.expires_at}>
                {new Date(state.expires_at).toLocaleString(locale)}
              </time>
            </p>
          )}
          {state.correction && (
            <div className="mt-3">
              <p className="font-bold">
                {c(
                  state.correction.kind === "amount" ? "amountFix" : "cardFix",
                )}
              </p>
              <p>
                {c("note")}:{" "}
                <span className="whitespace-pre-wrap break-words">
                  {state.correction.note || c("noNote")}
                </span>
              </p>
              <p className="mt-2 text-sm">{c("warning")}</p>
            </div>
          )}
        </Panel>
      )}
      {state?.phase === "awaiting_receipt" && !blocked && (
        <Panel>
          <p>{c("initial")}</p>
          {checkoutAccounts(accounts, checkoutAmount, state).length ? (
            checkoutAccounts(accounts, checkoutAmount, state).map((account) => (
              <p key={account.number} className="mt-2">
                <span dir="ltr" className="select-all font-mono">
                  {account.number}
                </span>{" "}
                · {account.holder} · {account.bank}
              </p>
            ))
          ) : (
            <p>{c("missingAccounts")}</p>
          )}
        </Panel>
      )}
      {state && canSend(state, now) && !blocked && (
        <>
          <label className="block">
            {c("choose")}
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp"
              disabled={busy || loading}
              className="mt-2 block w-full"
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
          </label>
          {preview && (
            <img
              src={preview}
              alt={c("preview")}
              className="max-h-80 rounded-2xl object-contain"
            />
          )}
          {file && (
            <>
              <p className="break-all">{file.name}</p>
              <label className="flex gap-2">
                <input
                  type="checkbox"
                  checked={ack}
                  disabled={busy}
                  onChange={(e) => setAck(e.target.checked)}
                />
                {c("ack")}
              </label>
              <Button
                busy={busy}
                disabled={!ack || busy || !canSend(state, now)}
                onClick={() => void send()}
              >
                {c("send")}
              </Button>
            </>
          )}
        </>
      )}
      <Button disabled={busy || loading} onClick={() => void refresh()}>
        {c("refresh")}
      </Button>
    </section>
  );
}
