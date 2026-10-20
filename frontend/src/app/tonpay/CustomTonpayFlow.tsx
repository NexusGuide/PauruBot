import { useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { useApp } from "../state";
import { actionGate, topupIntents } from "../topup-intents";
import { Button, Panel } from "../ui";
import { tonpay } from "./api";
import { canAct, validFile, type CustomPayment } from "./model";

const fa = {
  loading: "در حال دریافت وضعیت…", error: "دریافت یا ثبت نتیجه ممکن نشد. از تکرار انتقال وجه خودداری کن و وضعیت را بررسی کن.",
  refresh: "بررسی وضعیت پرداخت", credit: "مبلغ شارژ کیف پول", payable: "مبلغ نهایی انتقال با کارمزد", fee: "کارمزد",
  consent: "مبلغ نهایی و کارمزد را تأیید می‌کنم", number: "شماره کارت", holder: "به نام", change: "درخواست کارت جدید",
  receipt: "ارسال رسید", file: "انتخاب تصویر رسید (JPEG، PNG یا WebP؛ حداکثر ۱۰ مگابایت)",
  uncertain: "نتیجهٔ یک عملیات نامشخص است. انتقال یا ارسال دوباره انجام نده؛ پشتیبانی باید آن را بررسی کند.",
  providerOnly: "اعلام پرداخت توسط درگاه ثبت شده؛ شارژ کیف پول فقط پس از تأیید سرور و ثبت مالی انجام می‌شود.",
  noSettlement: "ارسال رسید به‌تنهایی تأیید پرداخت یا شارژ کیف پول نیست.", wait: "ارسال رسید هنوز مجاز نیست؛ پس از زمان انتظار، وضعیت را بررسی کن.",
  copy: "کپی شماره کارت", copied: "کپی شد", fileError: "نوع یا اندازهٔ تصویر مجاز نیست.", closed: "وضعیت سفارش پرداخت",
};
const en: typeof fa = {
  loading: "Loading payment…", error: "The result could not be confirmed. Do not transfer again; check status or contact support.",
  refresh: "Check payment status", credit: "Wallet credit", payable: "Total to transfer including fee", fee: "Fee",
  consent: "I accept this exact total and fee", number: "Card number", holder: "Cardholder", change: "Request another card",
  receipt: "Upload receipt", file: "Receipt image (JPEG, PNG or WebP; maximum 10 MiB)",
  uncertain: "An operation has an unknown outcome. Do not transfer or submit again; support must review it.",
  providerOnly: "The provider reports paid. Wallet credit still requires server verification and financial posting.",
  noSettlement: "A receipt upload alone does not confirm payment or credit your wallet.", wait: "Receipt upload is not ready. Check again after the waiting period.",
  copy: "Copy card number", copied: "Copied", fileError: "Unsupported image type or size.", closed: "Payment status",
};

/** Parent keys this subtree by authenticated owner + payment ID. No card is stored in sessionStorage. */
export function CustomTonpayFlow({ paymentId }: { paymentId: number }) {
  const { me, locale, toast } = useApp();
  const text = locale === "fa" ? fa : en;
  const [value, setValue] = useState<CustomPayment | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const gate = useRef(actionGate());
  const alive = useRef(true);
  const revision = useRef(0);
  useEffect(() => {
    alive.current = true;
    const seq = ++revision.current;
    tonpay.view(paymentId).then(p => { if (alive.current && revision.current === seq) setValue(p); })
      .catch(() => { if (alive.current && revision.current === seq) setError(true); });
    return () => { alive.current = false; revision.current++; };
  }, [paymentId]);
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
  return <>
    {error && <Panel><p role="alert">{text.error}</p></Panel>}
    {!value && !error && <Panel><p role="status">{text.loading}</p></Panel>}
    {value && <Panel>
      <p>{text.credit}: <b className="num">{money(value.request_amount, locale)}</b></p>
      {value.payable_amount !== null && <>
        <p>{text.fee}: <b className="num">{money(value.payable_amount - value.request_amount, locale)}</b></p>
        <p>{text.payable}: <b className="num">{money(value.payable_amount, locale)}</b></p>
      </>}
      <p>{text.closed}: {value.status}</p>
      <p className="app-hint">{text.noSettlement}</p>
      {value.uncertain && <p role="alert">{text.uncertain}</p>}
      {value.provider_paid && <p role="status">{text.providerOnly}</p>}
      {!value.consented && canAct(value, "consent", busy) && <Button onClick={() => void run("consent")} disabled={busy}>{text.consent}</Button>}
      {value.card && <div>
        <p>{text.number}: <b dir="ltr">{value.card.number}</b></p><p>{text.holder}: {value.card.holder}</p>
        <Button tone="quiet" disabled={busy} onClick={() => { const number = value.card?.number;
          if (number) void navigator.clipboard.writeText(number).then(() => { if (alive.current) toast(text.copied); }).catch(() => { if (alive.current) setError(true); }); }}>{text.copy}</Button>
      </div>}
      <Button tone="quiet" disabled={!canAct(value, "change", busy)} onClick={() => void run("change")}>{text.change}</Button>
      <label>{text.file}<input type="file" accept="image/jpeg,image/png,image/webp" disabled={!canAct(value, "receipt", busy)} onChange={e => {
        const picked = e.target.files?.[0] ?? null;
        if (picked && !validFile(picked)) { setFile(null); toast(text.fileError, "error"); e.target.value = ""; }
        else setFile(picked);
      }} /></label>
      <Button disabled={!file || !canAct(value, "receipt", busy)} onClick={() => void run("receipt")}>{text.receipt}</Button>
      {!value.can_receipt && value.consented && !value.uncertain && !value.provider_paid && value.status === "pending" && <p>{text.wait}</p>}
    </Panel>}
    <Button tone="quiet" busy={busy} onClick={() => void run("read")}>{text.refresh}</Button>
  </>;
}
