import type { TransferQuote } from "./api";

export interface TransferSession { quote: TransferQuote; attempted: boolean }
const key = (owner: number) => `paurubot:transfer:v1:${owner}`;
export function readTransfer(owner: number): TransferSession | null {
  const raw = sessionStorage.getItem(key(owner));
  if (!raw) return null;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object") throw new Error("transfer_storage_invalid");
  const v = value as TransferSession;
  if (!v.quote || typeof v.quote.token !== "string" || v.quote.token.length > 2048 ||
      typeof v.attempted !== "boolean" || !Number.isSafeInteger(v.quote.amount) || v.quote.amount <= 0 ||
      typeof v.quote.currency !== "string" || !Number.isSafeInteger(v.quote.expires_at) ||
      !v.quote.recipient || !Number.isSafeInteger(v.quote.recipient.telegram_id) ||
      (v.quote.recipient.name !== null && typeof v.quote.recipient.name !== "string") ||
      (v.quote.recipient.username !== null && typeof v.quote.recipient.username !== "string")) {
    throw new Error("transfer_storage_invalid");
  }
  return v;
}
export function saveTransfer(owner: number, value: TransferSession) {
  sessionStorage.setItem(key(owner), JSON.stringify(value));
}
export function clearTransfer(owner: number) { sessionStorage.removeItem(key(owner)); }
export function transferAmount(text: string): number | null {
  const latin = text.replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 0x06f0))
    .replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 0x0660)).trim();
  if (!/^\d+$/.test(latin)) return null;
  const n = Number(latin);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}
