/** Read-only projections. Own-audit records never retain staff-only fields. */
export type Mode = "own" | "staff";
export interface Runway {
    balance: number;
    debt: number;
    daily_spend: number;
    days: number | null;
    hours: number | null;
    low: boolean;
    window_days: number;
}
export interface Event {
    at: string | null;
    type: string;
    kind: string;
    amount?: number;
    balance_after?: number;
    memo?: string | null;
    reference_type?: string | null;
    reference_id?: number | null;
    actor_type?: string | null;
    actor_id?: number | null;
    before?: Record<string, string | number | boolean | null>;
    after?: Record<string, string | number | boolean | null>;
}
export interface Hub {
    runway: Runway;
    timeline: Event[];
}
const object = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v);
const positive = (v: unknown): v is number => integer(v) && v > 0;
const nonnegative = (v: unknown): v is number => integer(v) && v >= 0;
function text(v: unknown, max = 4000): string { if (typeof v !== "string" || v.length > max)
    throw new Error("invalid text"); return v; }
function maybeText(v: unknown): string | null { return v === null || v === undefined ? null : text(v); }
function maybeId(v: unknown): number | null { if (v === null || v === undefined)
    return null; if (!positive(v))
    throw new Error("invalid id"); return v; }
function at(v: unknown): string | null { if (v === null)
    return null; const s = text(v, 80); if (!/^\d{4}-\d{2}-\d{2}T/.test(s) || !Number.isFinite(Date.parse(s)))
    throw new Error("invalid timestamp"); return s; }
// Deliberately narrow. Unknown, nested, credential, contact and free-text fields are omitted.
const safeFields = ["is_active", "billing_mode", "billing_debt", "credit_limit", "low_balance_threshold", "max_active_services", "max_purchases_per_day", "discount_percent", "discount_override", "gb_price", "hourly_price", "group_id", "parent_id", "max_subresellers"];
function account(v: unknown): Record<string, string | number | boolean | null> {
    const out: Record<string, string | number | boolean | null> = {};
    if (!object(v))
        return out;
    for (const k of safeFields) {
        const x = v[k];
        if (x === null || typeof x === "boolean" || integer(x))
            out[k] = x;
        else if (k === "billing_mode" && typeof x === "string" && ["prepaid", "postpaid", "metered"].includes(x))
            out[k] = x;
    }
    return out;
}
export function hubPath(mode: Mode, profileId?: number): string | null { return mode === "own" ? "/api/admin/my/hub" : positive(profileId) ? `/api/admin/resellers/${profileId}/hub` : null; }
export function mayRead(mode: Mode, role: string | undefined, allowed: boolean): boolean { return allowed && !!role && (mode === "own" ? role === "reseller" : role !== "reseller"); }
export function parseHub(raw: unknown, mode: Mode): Hub {
    if (!object(raw) || !object(raw.runway) || !Array.isArray(raw.timeline) || raw.timeline.length > 100)
        throw new Error("invalid hub");
    const r = raw.runway;
    if (!integer(r.balance) || !nonnegative(r.debt) || !nonnegative(r.daily_spend) || !positive(r.window_days) || typeof r.low !== "boolean")
        throw new Error("invalid runway");
    if (r.days !== null && (typeof r.days !== "number" || !Number.isFinite(r.days) || r.days < 0))
        throw new Error("invalid days");
    if (r.hours !== null && !nonnegative(r.hours))
        throw new Error("invalid hours");
    if ((r.daily_spend === 0 && (r.days !== null || r.hours !== null || r.low)) || (r.daily_spend > 0 && (r.days === null || r.hours === null)))
        throw new Error("inconsistent estimate");
    const runway: Runway = { balance: r.balance, debt: r.debt, daily_spend: r.daily_spend, days: r.days as number | null, hours: r.hours as number | null, low: r.low, window_days: r.window_days };
    const timeline = raw.timeline.map((v: unknown): Event => {
        if (!object(v))
            throw new Error("invalid event");
        const e: Event = { at: at(v.at), type: text(v.type, 64), kind: text(v.kind, 200) };
        if (e.type === "ledger") {
            if (!integer(v.amount) || !integer(v.balance_after))
                throw new Error("invalid ledger");
            return { ...e, amount: v.amount, balance_after: v.balance_after, memo: maybeText(v.memo), reference_type: maybeText(v.reference_type), reference_id: maybeId(v.reference_id) };
        }
        if (e.type === "audit" && mode === "staff")
            return { ...e, actor_type: maybeText(v.actor_type), actor_id: maybeId(v.actor_id), before: account(v.before), after: account(v.after) };
        return e;
    });
    return { runway, timeline };
}
