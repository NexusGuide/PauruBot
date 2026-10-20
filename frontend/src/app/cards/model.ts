/** No card numbers, photos, provider calls or persistence in the status model. */
export type Card = { id: number; masked: string; status: "pending" | "approved" | "rejected" };
export type Verification = {
  enabled: boolean; min_amount: number; trusted: boolean; blocked: boolean;
  approved: boolean; can_submit: boolean; items: Card[];
};
export type Submission = { intent_key: string; number: string; image: string };
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("invalid_card_state");
  return value as Record<string, unknown>;
};
export function parseCard(value: unknown): Card {
  const row = object(value);
  if (!Number.isSafeInteger(row.id) || (row.id as number) <= 0 ||
      typeof row.masked !== "string" || !/^\d{4}-\d{2}\*{2}-\*{4}-\d{4}$/.test(row.masked) ||
      !["pending", "approved", "rejected"].includes(row.status as string)) throw Error("invalid_card_state");
  return { id: row.id as number, masked: row.masked, status: row.status as Card["status"] };
}
export function parseVerification(value: unknown): Verification {
  const row = object(value);
  for (const key of ["enabled", "trusted", "blocked", "approved", "can_submit"]) {
    if (typeof row[key] !== "boolean") throw Error("invalid_card_state");
  }
  if (!Number.isSafeInteger(row.min_amount) || (row.min_amount as number) < 0 ||
      !Array.isArray(row.items) || row.items.length > 20) throw Error("invalid_card_state");
  const items = row.items.map(parseCard);
  if (new Set(items.map((item) => item.id)).size !== items.length) throw Error("invalid_card_state");
  if (row.can_submit && (!row.enabled || row.trusted || row.blocked || row.approved ||
      items.some((item) => item.status === "pending"))) throw Error("invalid_card_state");
  return { enabled: row.enabled as boolean, min_amount: row.min_amount as number,
    trusted: row.trusted as boolean, blocked: row.blocked as boolean,
    approved: row.approved as boolean, can_submit: row.can_submit as boolean, items };
}
export const MAX_PHOTO = 3 * 1024 * 1024;
export function validFile(file: { size: number; type: string }): boolean {
  return file.size > 0 && file.size <= MAX_PHOTO && ["image/png", "image/jpeg"].includes(file.type);
}
export function normalizeNumber(text: string): string | null {
  const number = text.replace(/[۰-۹]/g, (ch) => String(ch.charCodeAt(0) - 1776))
    .replace(/[٠-٩]/g, (ch) => String(ch.charCodeAt(0) - 1632)).replace(/[ -]/g, "");
  if (!/^[0-9]{16}$/.test(number)) return null;
  const total = [...number].reverse().reduce((sum, ch, index) => {
    let digit = Number(ch) * (index % 2 ? 2 : 1);
    if (digit > 9) digit -= 9;
    return sum + digit;
  }, 0);
  return total % 10 === 0 ? number : null;
}
/** The first payload survives uncertain failures unchanged, in memory only. */
export function submissionGate() {
  let active = false;
  let payload: Submission | null = null;
  return {
    enter(next: Submission): Submission | null {
      if (active) return null;
      active = true;
      payload ??= next;
      return payload;
    },
    leave() { active = false; },
    clear() { payload = null; },
  };
}
/** Preserve only this customer's checkout URL; never follow an arbitrary return redirect. */
export function returnPath(value: unknown, customerId: number): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "/wallet";
  const state = value as Record<string, unknown>;
  return state.customerId === customerId && typeof state.returnTo === "string" &&
    state.returnTo.length <= 4096 && /^\/checkout\?[^\r\n]*$/.test(state.returnTo)
    ? state.returnTo : "/wallet";
}
