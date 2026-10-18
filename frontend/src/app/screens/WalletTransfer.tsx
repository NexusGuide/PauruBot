import { useEffect, useRef, useState } from "react";
import { ApiError } from "@shared/http";
import { money } from "@shared/format";
import { api, type TransferPolicy } from "../api";
import { useApp } from "../state";
import { clearTransfer, readTransfer, saveTransfer, transferAmount, type TransferSession } from "../transfer-session";
import { Button } from "../ui";

type Props = { owner: number; disabled: boolean; acquire: () => boolean; release: () => void; onDone: () => void };

export function WalletTransfer({ owner, disabled, acquire, release, onDone }: Props) {
  const { locale } = useApp();
  const fa = locale === "fa";
  const text = (persian: string, english: string) => fa ? persian : english;
  const [policy, setPolicy] = useState<TransferPolicy | null>(null);
  const [recipient, setRecipient] = useState("");
  const [amount, setAmount] = useState("");
  const [saved, setSaved] = useState<TransferSession | null>(null);
  const [message, setMessage] = useState("");
  const [storageReady, setStorageReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const alive = useRef(false);
  const ownGate = useRef(false);
  const load = async () => {
    setFailed(false);
    try { const result = await api.transferPolicy(); if (alive.current) setPolicy(result); }
    catch { if (alive.current) setFailed(true); }
  };
  useEffect(() => {
    alive.current = true;
    try { setSaved(readTransfer(owner)); setStorageReady(true); }
    catch { setMessage(text("حافظهٔ مرورگر در دسترس نیست؛ انتقال تا رفع مشکل غیرفعال است.", "Browser storage is unavailable; transfer is disabled until resolved.")); setFailed(true); }
    void load();
    return () => { alive.current = false; };
  }, [owner]);
  const begin = () => {
    if (ownGate.current || !acquire()) return false;
    ownGate.current = true; setBusy(true); return true;
  };
  const finish = () => { ownGate.current = false; release(); if (alive.current) setBusy(false); };
  const preview = async () => {
    const parsed = transferAmount(amount);
    if (!storageReady || parsed === null || saved || !begin()) return;
    setMessage("");
    try {
      const result = await api.transferQuote(recipient.trim(), parsed);
      // A quote has no financial effect; do not retain it after this owner unmounts.
      if (!alive.current) return;
      const next = { quote: result, attempted: false };
      saveTransfer(owner, next); setSaved(next);
    } catch (error) {
      if (alive.current) setMessage(describe(error));
    } finally { finish(); }
  };
  const describe = (error: unknown): string => {
    const labels: Record<string, string> = {
      transfer_off: text("انتقال برای این فروشگاه غیرفعال است.", "Transfers are disabled for this shop."),
      transfer_no_recipient: text("گیرنده معتبر یا در دسترس نیست.", "Recipient is unavailable."),
      transfer_self: text("انتقال به خود مجاز نیست.", "Cannot transfer to yourself."),
      transfer_daily_max: text("سقف انتقال روزانه کافی نیست.", "Daily transfer limit exceeded."),
      transfer_min: text("مبلغ از حداقل کمتر است.", "Amount is below the minimum."),
      insufficient_balance: text("موجودی کافی نیست.", "Insufficient balance."),
      transfer_quote_expired: text("تأیید منقضی شد؛ انتقالی با این تأیید ثبت نشده است.", "Consent expired; no transfer was posted for it."),
    };
    const label = error instanceof ApiError ? labels[error.code] : undefined;
    return label ??
      text("نتیجه مشخص نیست یا درخواست انجام نشد. همان تأیید را دوباره بررسی کن؛ انتقال تازه نساز.", "Result unknown or request failed. Retry this same confirmation; do not start another transfer.");
  };
  const confirm = async () => {
    if (!saved || !begin()) return;
    setMessage("");
    let success = false;
    try {
      const next = { ...saved, attempted: true };
      // Fail closed if the token cannot survive a reload before starting money movement.
      saveTransfer(owner, next); setSaved(next);
      const result = await api.transferConfirm(saved.quote.token);
      success = true;
      // A storage error after acknowledgement must not turn this into an uncertain payment.
      try { clearTransfer(owner); } catch { /* A retained token only replays the journal. */ }
      if (alive.current) {
        setSaved(null); setAmount(""); setRecipient("");
        setMessage(text(`انتقال ثبت شد؛ شمارهٔ پیگیری ${result.journal_id}`, `Transfer posted; reference ${result.journal_id}`));
        onDone();
      }
    } catch (error) {
      if (!alive.current) return;
      if (!success && error instanceof ApiError && error.code === "transfer_quote_expired") {
        try { clearTransfer(owner); setSaved(null); } catch { /* Keep the same safe token. */ }
      }
      setMessage(describe(error));
    } finally { finish(); }
  };
  const cancel = () => {
    if (!saved || saved.attempted || ownGate.current || disabled) return;
    try { clearTransfer(owner); setSaved(null); setMessage(""); }
    catch { setMessage(text("پاک‌کردن تأیید ممکن نشد.", "Could not clear confirmation.")); }
  };
  if (!policy && !failed) return <p role="status">{text("در حال دریافت شرایط انتقال…", "Loading transfer policy…")}</p>;
  if (!saved && policy && !policy.enabled) return null;
  return <section className="app-card p-4 mt-6" aria-label={text("انتقال کیف پول", "Wallet transfer")}>
    <h2>{text("انتقال کیف پول", "Wallet transfer")}</h2>
    {failed && <Button tone="quiet" disabled={disabled || busy} onClick={() => void load()}>{text("دریافت دوبارهٔ شرایط", "Retry policy read")}</Button>}
    {saved ? <>
      <p>{saved.quote.recipient.name} {saved.quote.recipient.username ? `@${saved.quote.recipient.username}` : ""}</p>
      <p dir="ltr">Telegram ID: {saved.quote.recipient.telegram_id}</p>
      <p>{money(saved.quote.amount, locale)} {saved.quote.currency}</p>
      <p>{text("پس از تأیید، مبلغ از کیف پول شما به این گیرنده منتقل می‌شود.", "Confirm to move this amount from your wallet to this recipient.")}</p>
      <p>{text("اعتبار تأیید:", "Consent expires:")} {new Date(saved.quote.expires_at * 1000).toLocaleString(locale)}</p>
      <Button busy={busy} disabled={disabled || busy} onClick={() => void confirm()}>{saved.attempted ? text("بررسی و تکرار همان تأیید", "Retry same confirmation") : text("تأیید انتقال", "Confirm transfer")}</Button>
      {!saved.attempted && <Button tone="quiet" disabled={disabled || busy} onClick={cancel}>{text("انصراف", "Cancel")}</Button>}
    </> : policy?.enabled && storageReady && <>
      <p>{text("فقط کاربران همین فروشگاه؛ حداقل", "Same shop only; minimum")} {money(policy.minimum, locale)} {policy.currency}</p>
      <p>{text("حداکثر هر انتقال", "Maximum per transfer")} {money(policy.maximum, locale)}؛ {text("سقف ۲۴ ساعت", "Rolling 24h limit")} {policy.daily_maximum ? money(policy.daily_maximum, locale) : text("بدون سقف روزانه", "No daily cap")}</p>
      <label>{text("آیدی عددی تلگرام یا @username گیرنده", "Recipient Telegram ID or @username")}<input dir="ltr" maxLength={64} disabled={disabled || busy} value={recipient} onChange={e => setRecipient(e.target.value)} /></label>
      <label>{text("مبلغ", "Amount")} ({policy.currency})<input inputMode="numeric" maxLength={12} disabled={disabled || busy} value={amount} onChange={e => setAmount(e.target.value)} /></label>
      <Button disabled={disabled || busy || !recipient.trim() || transferAmount(amount) === null} busy={busy} onClick={() => void preview()}>{text("بررسی گیرنده و مبلغ", "Preview recipient and amount")}</Button>
    </>}
    {message && <p role="status">{message}</p>}
  </section>;
}
