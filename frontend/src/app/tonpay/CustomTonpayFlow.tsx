import { useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { actionGate, topupIntents } from "../topup-intents";
import { Button, Trail } from "../ui";
import { tonpay } from "./api";
import { canAct, groupCard, needsCard, stage, statusKey, validFile, type CustomPayment } from "./model";

const fa = {
  steps: ["تأیید مبلغ", "انتقال وجه", "ارسال رسید"],
  loading: "در حال دریافت وضعیت…", cardLoading: "در حال دریافت کارت مقصد…",
  error: "دریافت یا ثبت نتیجه ممکن نشد. از تکرار انتقال وجه خودداری کن و وضعیت را بررسی کن.",
  refresh: "بررسی وضعیت پرداخت", payable: "مبلغ قابل انتقال", credit: "شارژ کیف پول", fee: "کارمزد تون‌پی",
  currency: "تومان", tapCopy: "برای کپی بزن", deadline: "مهلت پرداخت تا",
  confirmNote: "این مبلغ، با کارمزد تون‌پی، دقیقاً همان مبلغی است که باید کارت‌به‌کارت کنی. با تأیید آن، کارت مقصد نمایش داده می‌شود.",
  consent: "مبلغ را تأیید می‌کنم، کارت را نشان بده", target: "کارت مقصد", holder: "به نام",
  transferNote: "دقیقاً مبلغ بالا را به این کارت واریز کن.",
  change: "کارت دیگری می‌خواهم", changeLater: "درخواست کارت دیگر فعلاً ممکن نیست",
  waitReceipt: "ارسال رسید {time} دیگر فعال می‌شود؛ تا آن موقع انتقال را انجام بده.",
  choose: "انتخاب تصویر رسید", chooseHint: "JPEG، PNG یا WebP، حداکثر ۱۰ مگابایت", send: "ارسال رسید",
  sent: "رسید ارسال شد. پس از تأیید تون‌پی، کیف پولت خودکار شارژ می‌شود؛ می‌توانی وضعیت را بررسی کنی.",
  done: "پرداخت تأیید شد و کیف پولت شارژ شد.",
  uncertain: "نتیجهٔ یک عملیات نامشخص است. انتقال یا ارسال دوباره انجام نده؛ پشتیبانی باید آن را بررسی کند.",
  providerOnly: "تون‌پی پرداخت را اعلام کرده؛ پس از تأیید سرور، کیف پول شارژ می‌شود.",
  noSettlement: "ارسال رسید به‌تنهایی کیف پول را شارژ نمی‌کند؛ شارژ پس از تأیید تون‌پی انجام می‌شود.",
  copied: "کپی شد", fileError: "نوع یا اندازهٔ تصویر مجاز نیست.",
  status: {
    created: "در حال ساخت فاکتور", pending: "در انتظار پرداخت", verifying: "در حال تأیید", paid: "پرداخت شد",
    failed: "ناموفق", expired: "منقضی شد", disputed: "نیازمند بررسی", refunded: "بازگشت داده شد", voided: "باطل شد",
  } as Record<string, string>,
};
const en: typeof fa = {
  steps: ["Confirm total", "Transfer", "Receipt"],
  loading: "Loading payment…", cardLoading: "Fetching the destination card…",
  error: "The result could not be confirmed. Do not transfer again; check status or contact support.",
  refresh: "Check payment status", payable: "Amount to transfer", credit: "Wallet credit", fee: "Tonpay fee",
  currency: "Toman", tapCopy: "Tap to copy", deadline: "Pay before",
  confirmNote: "This total, including Tonpay's fee, is exactly what you transfer. Confirm it to see the destination card.",
  consent: "I accept this total, show the card", target: "Destination card", holder: "Cardholder",
  transferNote: "Transfer exactly the amount above to this card.",
  change: "I need another card", changeLater: "Another card is not available right now",
  waitReceipt: "Receipt upload opens in {time}; make the transfer meanwhile.",
  choose: "Choose the receipt image", chooseHint: "JPEG, PNG or WebP, up to 10 MB", send: "Send receipt",
  sent: "Receipt sent. Once Tonpay confirms it your wallet is credited automatically; you can check the status.",
  done: "Payment confirmed and your wallet was credited.",
  uncertain: "An operation has an unknown outcome. Do not transfer or submit again; support must review it.",
  providerOnly: "Tonpay reports it paid; your wallet is credited once the server confirms it.",
  noSettlement: "Sending a receipt alone does not credit your wallet; that happens after Tonpay confirms it.",
  copied: "Copied", fileError: "Unsupported image type or size.",
  status: {
    created: "Creating invoice", pending: "Awaiting payment", verifying: "Being confirmed", paid: "Paid",
    failed: "Failed", expired: "Expired", disputed: "Under review", refunded: "Refunded", voided: "Voided",
  },
};

const clock = (seconds: number, locale: string) => {
  const s = Math.max(0, Math.ceil(seconds));
  const n = (v: number, min = 1) => v.toLocaleString(locale, { minimumIntegerDigits: min, useGrouping: false });
  return `${n(Math.floor(s / 60))}:${n(s % 60, 2)}`;
};

/** Parent keys this subtree by authenticated owner + payment ID. No card is stored in sessionStorage. */
export function CustomTonpayFlow({ paymentId }: { paymentId: number }) {
  const { me, locale, toast } = useApp();
  const text = locale === "fa" ? fa : en;
  const [value, setValue] = useState<CustomPayment | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const gate = useRef(actionGate());
  const alive = useRef(true);
  const revision = useRef(0);
  const fetchedFor = useRef<number | null>(null);
  useEffect(() => {
    alive.current = true;
    const seq = ++revision.current;
    tonpay.view(paymentId).then(p => { if (alive.current && revision.current === seq) setValue(p); })
      .catch(() => { if (alive.current && revision.current === seq) setError(true); });
    return () => { alive.current = false; revision.current++; };
  }, [paymentId]);
  useEffect(() => {
    if (!file) { setPreview(null); return; }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const run = async (action: "read" | "consent" | "change" | "receipt") => {
    if (!me || !gate.current.enter()) return;
    if (action !== "read" && (!value || !canAct(value, action, busy))) { gate.current.leave(); return; }
    const seq = ++revision.current;
    setBusy(true); setError(false);
    // Remove old instructions immediately on any read/mutation; failed reads never leave a stale card.
    setValue(current => current ? { ...current, card: undefined } : null);
    try {
      if (action === "read") {
        const next = await tonpay.refresh(paymentId);
        if (alive.current && revision.current === seq) setValue(next);
      } else if (value?.quote_revision && value.payable_amount) {
        let next: CustomPayment;
        if (action === "consent") {
          next = await tonpay.consent(paymentId, value.quote_revision, value.payable_amount);
        } else {
          if (action === "receipt" && (!file || !validFile(file))) throw Error("invalid_receipt");
          const digest = action === "receipt" && file ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()))).map(x => x.toString(16).padStart(2, "0")).join("") : "";
          if (!alive.current || revision.current !== seq) return;
          const method = `custom_${paymentId}_${action}_${value.quote_revision}_${digest}`;
          const key = topupIntents().get(me.id, value.quote_revision, method);
          next = action === "change" ? await tonpay.change(paymentId, value.quote_revision, key) : await tonpay.receipt(paymentId, value.quote_revision, key, file!);
          if (!next.uncertain) topupIntents().clear(me.id, value.quote_revision, method, key);
        }
        if (alive.current && revision.current === seq) { setValue(next); setFile(null); }
        // No automatic provider mutation retry or automatic paid claim. User explicitly checks status.
      }
    } catch {
      if (alive.current && revision.current === seq) { setError(true); setValue(null); }
    } finally {
      gate.current.leave();
      if (alive.current && revision.current === seq) setBusy(false);
    }
  };
  // The card is never stored: one authenticated read fetches it right after consent (or on reopening).
  useEffect(() => {
    if (!value || busy || error || !needsCard(value)) return;
    const quote = value.quote_revision ?? 0;
    if (fetchedFor.current === quote) return;
    fetchedFor.current = quote;
    void run("read");
  });
  const current = value ? stage(value, now) : 0;
  const waiting = value !== null && current === 1 && value.receipt_ready_at !== null;
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [waiting]);
  // When the receipt wait ends, read once so the upload opens without a manual check.
  const readyAt = value?.receipt_ready_at ?? null;
  const opened = useRef<number | null>(null);
  useEffect(() => {
    if (!value || busy || readyAt === null || !value.consented || value.can_receipt || now < readyAt) return;
    if (opened.current === readyAt || value.status !== "pending" || value.uncertain || value.provider_paid) return;
    opened.current = readyAt;
    void run("read");
  });
  const copy = (raw: string) => {
    haptic("select");
    void navigator.clipboard.writeText(raw).then(() => { if (alive.current) toast(text.copied); })
      .catch(() => { /* the number stays selectable */ });
  };

  if (error && !value) return <>
    <div className="app-note" data-tone="danger" role="alert"><Icon name="help" size={20} /><span>{text.error}</span></div>
    <Button tone="quiet" busy={busy} onClick={() => void run("read")}>{text.refresh}</Button>
  </>;
  if (!value) return <p role="status" className="app-hint text-sm">{text.loading}</p>;

  const pending = value.status === "pending" && !value.uncertain && !value.provider_paid;
  // Receipt sent and still pending: it is being confirmed, not awaiting payment.
  const key = current === 3 && pending ? "verifying" : statusKey(value);
  const label = text.status[key] ?? text.status.pending;
  const tone = value.status === "paid" || value.provider_paid ? "green" : pending ? "orange" : "red";
  return (
    <section aria-busy={busy}>
      <Trail items={text.steps} active={current} />
      <div className="mb-3 flex items-center justify-between gap-2">
        <span className="app-badge" data-tone={tone}>{label}</span>
        {value.expires_at && pending && (
          <small className="app-hint num">
            {text.deadline} {new Date(value.expires_at).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}
          </small>
        )}
      </div>

      {value.payable_amount !== null && (
        <button type="button" className="app-wcard mb-3 block w-full text-start" onClick={() => copy(String(value.payable_amount))}>
          <span className="block text-sm font-semibold opacity-90">{text.payable}</span>
          <b className="num mt-1 block text-3xl font-extrabold">
            {money(value.payable_amount, locale)} <small className="text-base font-semibold">{text.currency}</small>
          </b>
          <span className="app-wcard-note mt-2 block text-xs font-bold">{text.tapCopy}</span>
        </button>
      )}
      <div className="app-card mb-3 p-4">
        <div className="app-ln">
          <span className="app-hint">{text.credit}</span>
          <span className="num font-semibold">{money(value.request_amount, locale)} {text.currency}</span>
        </div>
        {value.payable_amount !== null && (
          <div className="app-ln">
            <span className="app-hint">{text.fee}</span>
            <span className="num font-semibold">{money(value.payable_amount - value.request_amount, locale)} {text.currency}</span>
          </div>
        )}
      </div>

      {value.uncertain && <div className="app-note mb-3" data-tone="danger" role="alert"><Icon name="help" size={20} /><span>{text.uncertain}</span></div>}
      {value.provider_paid && value.status === "pending" && <div className="app-note mb-3" data-tone="soft" role="status"><Icon name="check" size={20} /><span>{text.providerOnly}</span></div>}
      {error && <div className="app-note mb-3" data-tone="danger" role="alert"><Icon name="help" size={20} /><span>{text.error}</span></div>}

      {current === 0 && pending && (
        <div className="app-card mb-3 p-4">
          <p className="app-hint mb-3 text-sm">{text.confirmNote}</p>
          <Button busy={busy} disabled={!canAct(value, "consent", busy)} onClick={() => void run("consent")}>{text.consent}</Button>
        </div>
      )}

      {(current === 1 || current === 2) && pending && (
        <div className="app-card mb-3 p-4">
          {value.card ? (
            <div className="app-cc">
              <span className="app-chip">{text.target}</span>
              <b className="num select-all" dir="ltr">{groupCard(value.card.number)}</b>
              <div className="flex items-center justify-between gap-2">
                <small>{text.holder}: {value.card.holder}</small>
                <button type="button" className="app-copy app-rip-host" aria-label={text.copied} onClick={() => copy(value.card!.number.replace(/\D/g, ""))}>
                  <Icon name="copy" size={14} />
                </button>
              </div>
            </div>
          ) : (
            <p role="status" className="app-hint text-sm">{text.cardLoading}</p>
          )}
          <p className="app-hint mt-3 text-sm">{text.transferNote}</p>
          <button type="button" className="mt-2 text-sm font-semibold text-[var(--accent)] disabled:text-[var(--hint)]"
            disabled={!canAct(value, "change", busy)} onClick={() => void run("change")}>
            {canAct(value, "change", busy) || busy ? text.change : text.changeLater}
          </button>
        </div>
      )}

      {waiting && pending && readyAt !== null && (
        <div className="app-note mb-3" data-tone="soft" role="status">
          <Icon name="clock" size={20} />
          <span className="num">{text.waitReceipt.replace("{time}", clock(readyAt - now, locale))}</span>
        </div>
      )}

      {current === 2 && pending && (
        <>
          <label className="app-up mb-3">
            <input type="file" accept="image/jpeg,image/png,image/webp" className="sr-only"
              disabled={!canAct(value, "receipt", busy)}
              onChange={e => {
                const picked = e.target.files?.[0] ?? null;
                if (picked && !validFile(picked)) { setFile(null); toast(text.fileError, "error"); }
                else setFile(picked);
                e.target.value = "";
              }} />
            {preview ? <img src={preview} alt="" /> : <Icon name="up" size={26} />}
            <span>{text.choose}</span>
            {file ? <small className="break-all">{file.name}</small> : <small>{text.chooseHint}</small>}
          </label>
          {file && <Button className="mb-3" busy={busy} disabled={!canAct(value, "receipt", busy)} onClick={() => void run("receipt")}>{text.send}</Button>}
        </>
      )}

      {current === 3 && (
        <div className="app-note mb-3" data-tone="soft" role="status">
          <Icon name={value.status === "paid" ? "check" : "clock"} size={20} />
          <span>{value.status === "paid" ? text.done : pending ? text.sent : label}</span>
        </div>
      )}

      <Button tone="quiet" busy={busy} onClick={() => void run("read")}>{text.refresh}</Button>
      {pending && <p className="app-hint mt-3 text-center text-xs">{text.noSettlement}</p>}
    </section>
  );
}
