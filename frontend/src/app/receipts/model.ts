export interface ReceiptState {
  id: number;
  provider: string;
  status: string;
  amount: number;
  currency: string;
  order_id: number | null;
  fulfilled: boolean;
  expires_at: string | null;
  revision: string;
  correction: {
    kind: "amount" | "card";
    note: string | null;
    requested_at: string;
  } | null;
  receipt_submitted: boolean;
  phase: string;
  can_upload: boolean;
}
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw Error("invalid_receipt_state");
  return value as Record<string, unknown>;
};
const integer = (value: unknown, min = 0): number => {
  if (!Number.isSafeInteger(value) || (value as number) < min)
    throw Error("invalid_receipt_state");
  return value as number;
};
const string = (value: unknown): string => {
  if (typeof value !== "string" || !value) throw Error("invalid_receipt_state");
  return value;
};
const boolean = (value: unknown): boolean => {
  if (typeof value !== "boolean") throw Error("invalid_receipt_state");
  return value;
};
const date = (value: unknown): string => {
  const text = string(value);
  if (!/(Z|[+-]\d\d:\d\d)$/.test(text) || !Number.isFinite(Date.parse(text)))
    throw Error("invalid_receipt_state");
  return text;
};
export function parseReceipt(raw: unknown, expectedId?: number): ReceiptState {
  const v = object(raw),
    id = integer(v.id, 1);
  if (expectedId !== undefined && id !== expectedId)
    throw Error("invalid_receipt_state");
  const status = string(v.status),
    phase = string(v.phase),
    provider = string(v.provider);
  if (
    ![
      "created",
      "pending",
      "verifying",
      "paid",
      "failed",
      "expired",
      "refunded",
      "disputed",
    ].includes(status)
  )
    throw Error("invalid_receipt_state");
  if (
    ![
      "awaiting_receipt",
      "correction_required",
      "under_review",
      "paid",
      "closed",
      "deadline_passed",
      "not_card",
      "unknown",
    ].includes(phase)
  )
    throw Error("invalid_receipt_state");
  const revision = string(v.revision);
  if (!/^[a-f0-9]{64}$/.test(revision)) throw Error("invalid_receipt_state");
  let correction: ReceiptState["correction"] = null;
  if (v.correction !== null) {
    const c = object(v.correction);
    if (c.kind !== "amount" && c.kind !== "card")
      throw Error("invalid_receipt_state");
    if (c.note !== null && typeof c.note !== "string")
      throw Error("invalid_receipt_state");
    correction = {
      kind: c.kind,
      note: c.note as string | null,
      requested_at: date(c.requested_at),
    };
  }
  const submitted = boolean(v.receipt_submitted),
    canUpload = boolean(v.can_upload);
  const active =
    phase === "awaiting_receipt" || phase === "correction_required";
  if (
    active !== canUpload ||
    (active && (provider !== "card" || status !== "pending" || submitted))
  )
    throw Error("invalid_receipt_state");
  if (
    (phase === "correction_required" && !correction) ||
    (phase === "awaiting_receipt" && correction)
  )
    throw Error("invalid_receipt_state");
  if (
    phase === "under_review" &&
    (provider !== "card" || status !== "verifying")
  )
    throw Error("invalid_receipt_state");
  if (phase === "paid" && (provider !== "card" || status !== "paid"))
    throw Error("invalid_receipt_state");
  if (
    phase === "closed" &&
    !["failed", "expired", "refunded", "disputed"].includes(status)
  )
    throw Error("invalid_receipt_state");
  if ((provider !== "card") !== (phase === "not_card"))
    throw Error("invalid_receipt_state");
  if (
    phase === "deadline_passed" &&
    (status !== "pending" || submitted || v.expires_at === null)
  )
    throw Error("invalid_receipt_state");
  return {
    id,
    provider,
    status,
    phase,
    revision,
    correction,
    can_upload: canUpload,
    receipt_submitted: submitted,
    amount: integer(v.amount),
    currency: string(v.currency),
    order_id: v.order_id === null ? null : integer(v.order_id, 1),
    fulfilled: boolean(v.fulfilled),
    expires_at: v.expires_at === null ? null : date(v.expires_at),
  };
}
export function canSend(value: ReceiptState, now = Date.now()): boolean {
  return (
    value.can_upload &&
    (value.expires_at === null || Date.parse(value.expires_at) > now)
  );
}
export function validateFile(file: { size: number; type: string }): boolean {
  return (
    file.size > 0 &&
    file.size <= 8 * 1024 * 1024 &&
    ["image/jpeg", "image/png", "image/webp"].includes(file.type)
  );
}
export function confirmUpload(
  raw: unknown,
  before: ReceiptState,
): ReceiptState {
  const after = parseReceipt(raw, before.id);
  if (
    after.provider !== "card" ||
    after.revision === before.revision ||
    after.amount !== before.amount ||
    after.currency !== before.currency ||
    after.order_id !== before.order_id ||
    !after.receipt_submitted ||
    after.can_upload ||
    !["under_review", "paid", "closed"].includes(after.phase)
  )
    throw Error("uncertain_receipt_upload");
  return after;
}
export function parseInbox(
  raw: unknown,
  before?: number,
): { items: ReceiptState[]; next_before: number | null } {
  const value = object(raw);
  if (!Array.isArray(value.items) || value.items.length > 20)
    throw Error("invalid_receipt_inbox");
  const items = value.items.map((item) => parseReceipt(item));
  if (
    items.some(
      (item, index) =>
        item.provider !== "card" ||
        (before !== undefined && item.id >= before) ||
        (index > 0 && item.id >= (items[index - 1]?.id ?? 0)),
    )
  )
    throw Error("invalid_receipt_inbox");
  const cursor =
    value.next_before === null ? null : integer(value.next_before, 1);
  if (cursor !== null && (items.length !== 20 || cursor !== items[19]?.id))
    throw Error("invalid_receipt_inbox");
  return { items, next_before: cursor };
}

export function checkoutAccounts(
  raw: unknown,
  amount: unknown,
  state: ReceiptState,
): { number: string; holder: string; bank: string }[] {
  if (
    state.phase !== "awaiting_receipt" ||
    amount !== state.amount ||
    !Array.isArray(raw) ||
    raw.length > 20
  )
    return [];
  return raw
    .filter(
      (item) =>
        item &&
        typeof item === "object" &&
        typeof item.number === "string" &&
        /^\d{12,20}$/.test(item.number) &&
        typeof item.holder === "string" &&
        item.holder.length <= 255 &&
        typeof item.bank === "string" &&
        item.bank.length <= 255,
    )
    .map((item) => ({
      number: item.number,
      holder: item.holder,
      bank: item.bank,
    }));
}
