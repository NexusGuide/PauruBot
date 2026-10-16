/** F-22 client contracts. The server remains authoritative and consumes tokens once. */
export interface ConfirmFields {
  confirm_token: string;
  confirm_text: string;
}

export type ConfirmationTarget =
  | { action: "service.delete"; id: number }
  | { action: "order.refund"; id: number }
  | { action: "service.bulk_delete" | "service.bulk_disable"; body: Record<string, unknown> };

export interface ConfirmationChallenge {
  action: string;
  token: string;
  phrase: string;
  expires_in: number;
  required?: boolean;
  count?: number;
}

export function confirmationSpec(target: ConfirmationTarget) {
  if ("id" in target) {
    if (!Number.isSafeInteger(target.id) || target.id < 1) throw new Error("invalid_confirmation_target");
    return {
      action: target.action,
      path: target.action === "service.delete"
        ? `/api/admin/services/${target.id}/confirm-delete`
        : `/api/admin/orders/${target.id}/confirm-refund`,
      body: undefined,
    };
  }
  // A private snapshot: changes to a selection/filter while the dialog is open cannot change it.
  const body = JSON.parse(JSON.stringify(target.body)) as Record<string, unknown>;
  if (body.action !== target.action.replace("service.bulk_", "")) {
    throw new Error("invalid_confirmation_target");
  }
  return { action: target.action, path: "/api/admin/services/bulk/confirm", body };
}

export function validateChallenge(value: unknown, action: string): ConfirmationChallenge {
  const c = value as Partial<ConfirmationChallenge> | null;
  if (
    !c || c.action !== action
    || typeof c.token !== "string" || !c.token || c.token.length > 64
    || typeof c.phrase !== "string" || !c.phrase.trim() || c.phrase.length > 128
    || !Number.isInteger(c.expires_in) || c.expires_in! < 1 || c.expires_in! > 300
    || (c.required !== undefined && typeof c.required !== "boolean")
    || (c.count !== undefined && (!Number.isSafeInteger(c.count) || c.count < 1))
  ) throw new Error("invalid_confirmation_challenge");
  // Only refunds can explicitly opt out of typing below the server's amount threshold.
  if (action === "order.refund" && typeof c.required !== "boolean") {
    throw new Error("invalid_confirmation_challenge");
  }
  if (action.startsWith("service.bulk_") && String(c.count) !== c.phrase) {
    throw new Error("invalid_confirmation_challenge");
  }
  return c as ConfirmationChallenge;
}

export function normalizeConfirmation(text: string): string {
  return text
    .replace(/[۰-۹]/g, (ch) => String(ch.charCodeAt(0) - 0x06f0))
    .replace(/[٠-٩]/g, (ch) => String(ch.charCodeAt(0) - 0x0660))
    .trim().replace(/^@+/, "").toLowerCase()
    .replace(/[,٬]/g, "").replace(/\s+/g, " ").trim();
}

export function needsTyping(challenge: ConfirmationChallenge): boolean {
  return challenge.action !== "order.refund" || challenge.required !== false;
}

export function confirmationFields(
  challenge: ConfirmationChallenge, typed: string, deadline: number, now = Date.now(),
): ConfirmFields | null {
  if (now >= deadline || !Number.isFinite(deadline)) return null;
  if (needsTyping(challenge) && (
    typed.length > 128 || !normalizeConfirmation(typed)
    || normalizeConfirmation(typed) !== normalizeConfirmation(challenge.phrase)
  )) return null;
  return { confirm_token: challenge.token, confirm_text: needsTyping(challenge) ? typed : "" };
}