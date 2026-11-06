export interface CustomPayment {
  payment_id: number; status: string; request_amount: number;
  payable_amount: number | null; quote_revision: number | null;
  consented: boolean; uncertain: boolean; recovery: string | null;
  provider_status: string | null; provider_paid: boolean; expires_at: string | null;
  can_consent: boolean; can_receipt: boolean; can_change_card: boolean;
  receipt_ready_at: number | null;
  card?: { number: string; holder: string };
}
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
/** Fail closed on old/malformed DTOs, unsafe JS amounts or inappropriate card disclosure. */
export function payment(value: unknown): CustomPayment {
  if (!object(value) || !integer(value.payment_id) || !integer(value.request_amount) ||
      !["created", "pending", "verifying", "paid", "failed", "expired", "disputed", "refunded", "voided"].includes(String(value.status))) throw Error("invalid_tonpay_state");
  for (const key of ["consented", "uncertain", "provider_paid", "can_consent", "can_receipt", "can_change_card"])
    if (typeof value[key] !== "boolean") throw Error("invalid_tonpay_state");
  for (const key of ["payable_amount", "quote_revision", "receipt_ready_at"])
    if (value[key] !== null && !integer(value[key])) throw Error("invalid_tonpay_state");
  for (const key of ["recovery", "provider_status", "expires_at"])
    if (value[key] !== null && (typeof value[key] !== "string" || value[key].length > 80)) throw Error("invalid_tonpay_state");
  if (value.payable_amount !== null && (value.payable_amount as number) < value.request_amount) throw Error("invalid_tonpay_state");
  const actionable = value.can_consent || value.can_receipt || value.can_change_card;
  if (actionable && (value.uncertain || value.provider_paid || value.status !== "pending" ||
      value.quote_revision === null || value.payable_amount === null)) throw Error("invalid_tonpay_state");
  if (value.can_receipt && !value.consented) throw Error("invalid_tonpay_state");
  if (value.card !== undefined && (!object(value.card) || typeof value.card.number !== "string" || value.card.number.length > 40 ||
      typeof value.card.holder !== "string" || value.card.holder.length > 120 ||
      value.consented !== true || value.uncertain !== false || value.provider_paid !== false || value.status !== "pending" || value.can_consent !== true)) throw Error("invalid_tonpay_state");
  return value as unknown as CustomPayment;
}
export function canAct(p: CustomPayment, action: "consent" | "receipt" | "change", busy: boolean): boolean {
  if (busy || p.uncertain || p.status !== "pending" || p.provider_paid) return false;
  return action === "consent" ? p.can_consent : action === "receipt" ? p.can_receipt : p.can_change_card;
}
export function validFile(file: { size: number; type: string }): boolean {
  return file.size > 0 && file.size <= 10 * 1024 * 1024 && ["image/jpeg", "image/png", "image/webp"].includes(file.type);
}

/** "6037 9900 0000 0000": digits in groups of four, whatever separators the provider sent. */
export function groupCard(number: string): string {
  return number.replace(/\D/g, "").replace(/\d{4}(?=\d)/g, "$& ");
}

/** Where the buyer is: 0 confirm the total, 1 transfer, 2 send the receipt, 3 waiting or finished. */
export function stage(p: CustomPayment, nowSeconds: number): 0 | 1 | 2 | 3 {
  if (p.status !== "pending" || p.provider_paid) return 3;
  if (!p.consented) return 0;
  if (p.can_receipt) return 2;
  if (p.receipt_ready_at !== null && nowSeconds < p.receipt_ready_at) return 1;
  return 3;
}

/** The card is only ever read live, after consent; fetch it as soon as it may be shown. */
export function needsCard(p: CustomPayment): boolean {
  return p.consented && !p.card && p.status === "pending" && !p.uncertain && !p.provider_paid && p.can_consent;
}

/** A word for the payment's state instead of a provider code. */
export function statusKey(p: CustomPayment): string {
  if (p.status === "pending" && p.provider_paid) return "verifying";
  return p.status;
}
