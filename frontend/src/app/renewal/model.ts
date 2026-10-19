export interface Snapshot {
  expire_at: string | null;
  data_limit: number;
  used_bytes: number;
  remaining_bytes: number | null;
}
export interface Preview {
  order_id: number;
  service_id: number;
  plan_id: number;
  amount: number;
  computed_at: string;
  valid_until: string;
  basis: "stored_service_snapshot";
  timing: "immediate" | "queued";
  queued_until: string | null;
  policy: { time: "append" | "reset" | "keep"; volume: "append" | "reset" | "keep"; reset_usage: boolean };
  before: Snapshot;
  after: Snapshot | null;
  resets_usage: boolean;
}
type Expected = { id: number; service_id: number | null; plan_id: number | null; amount: number };
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const uint = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0;
const instant = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d\d-\d\dT/.test(v) && Number.isFinite(Date.parse(v));
function snapshot(v: unknown): v is Snapshot {
  if (!record(v) || !(v.expire_at === null || instant(v.expire_at)) || !uint(v.data_limit) || !uint(v.used_bytes)) return false;
  return v.data_limit === 0 ? v.remaining_bytes === null : v.remaining_bytes === Math.max(0, v.data_limit - v.used_bytes);
}
export function decodePreview(v: unknown, expected: Expected): Preview {
  if (!record(v) || v.order_id !== expected.id || v.service_id !== expected.service_id || v.plan_id !== expected.plan_id ||
    v.amount !== expected.amount || !uint(v.amount) || v.basis !== "stored_service_snapshot" ||
    !instant(v.computed_at) || !instant(v.valid_until) || Date.parse(v.valid_until) <= Date.parse(v.computed_at) ||
    Date.parse(v.valid_until) - Date.parse(v.computed_at) > 60_000 ||
    !record(v.policy) || !["append", "reset", "keep"].includes(String(v.policy.time)) ||
    !["append", "reset", "keep"].includes(String(v.policy.volume)) || typeof v.policy.reset_usage !== "boolean" ||
    typeof v.resets_usage !== "boolean" || !snapshot(v.before)) throw Error("renewal_preview_invalid");
  if (v.timing === "queued") {
    if (!instant(v.queued_until) || v.after !== null) throw Error("renewal_preview_invalid");
  } else if (v.timing === "immediate") {
    if (v.queued_until !== null || !snapshot(v.after) || v.after.used_bytes !== (v.resets_usage ? 0 : v.before.used_bytes))
      throw Error("renewal_preview_invalid");
  } else throw Error("renewal_preview_invalid");
  return v as unknown as Preview;
}
export function fresh(preview: Preview, now = Date.now()): boolean {
  return Date.parse(preview.valid_until) > now && Date.parse(preview.computed_at) <= now + 30_000;
}

/** Include year and device timezone; month/day alone can hide a year-long renewal. */
export function previewTime(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }).format(new Date(iso));
}
