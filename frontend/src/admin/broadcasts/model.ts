/** The admin API exposes delivery counters, not a filtered audience estimate or skipped count. */
export interface BroadcastRow {
    id: number;
    text: string;
    audience: Record<string, unknown>;
    status: string;
    total: number;
    sent: number;
    failed: number;
    blocked: number;
    deleted: number;
    scheduled_at: string | null;
    created_at: string;
    finished_at: string | null;
}
export interface Draft {
    text: string;
    buttons: {
        text: string;
        url: string;
    }[];
    audience: "all" | "buyers" | "resellers";
    locale: "" | "fa" | "en";
    schedule: string;
}
export interface Body {
    text: string;
    buttons: {
        text: string;
        url: string;
    }[];
    buyers_only: boolean;
    resellers_only: boolean;
    locale: string | null;
    scheduled_at: string | null;
}
export type Action = "pause" | "resume" | "cancel";
export type Problem = "text" | "buttons" | "schedule";
export const emptyDraft = (): Draft => ({ text: "", buttons: [], audience: "all", locale: "", schedule: "" });
export const codepoints = (value: string): number => Array.from(value).length;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const date = (v: unknown): v is string => typeof v === "string" && Number.isFinite(Date.parse(v));
export function parseRow(value: unknown): BroadcastRow {
    if (!record(value) || !count(value.id) || value.id === 0 || typeof value.text !== "string" ||
        typeof value.status !== "string" || !record(value.audience) ||
        ![value.total, value.sent, value.failed, value.blocked, value.deleted].every(count) ||
        !date(value.created_at) || (value.scheduled_at !== null && !date(value.scheduled_at)) ||
        (value.finished_at !== null && !date(value.finished_at)))
        throw new Error("invalid_broadcast");
    // Project only known fields; never render arbitrary response properties as HTML.
    return { id: value.id, text: value.text, status: value.status, audience: value.audience,
        total: value.total as number, sent: value.sent as number, failed: value.failed as number,
        blocked: value.blocked as number, deleted: value.deleted as number,
        created_at: value.created_at, scheduled_at: value.scheduled_at as string | null, finished_at: value.finished_at as string | null };
}
export function parsePage(value: unknown): {
    items: BroadcastRow[];
    total: number;
} {
    if (!record(value) || !Array.isArray(value.items) || !count(value.total) || value.items.length > 25)
        throw new Error("invalid_page");
    const items = value.items.map(parseRow);
    if (new Set(items.map(row => row.id)).size !== items.length || items.length > value.total)
        throw new Error("invalid_page");
    return { items, total: value.total };
}
export function parseReachable(value: unknown): number {
    if (!record(value) || !count(value.reachable))
        throw new Error("invalid_audience");
    return value.reachable;
}
export function validUrl(value: string): boolean {
    if (!/^(https:\/\/|tg:\/\/)/.test(value) || /[\s\x00-\x1f\x7f\\]/.test(value))
        return false;
    try {
        const u = new URL(value);
        return !!u.hostname && !u.username && !u.password && (u.protocol === "https:" || u.protocol === "tg:");
    }
    catch {
        return false;
    }
}
/** Reject invalid/nonexistent local dates instead of silently normalizing a DST gap. */
export function scheduledIso(value: string, now = Date.now()): string | null {
    if (!value)
        return null;
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
    if (!match)
        throw new Error("schedule");
    const [year, month, day, hour, minute] = match.slice(1).map(Number);
    const d = new Date(value);
    if (!Number.isFinite(d.getTime()) || d.getFullYear() !== year || d.getMonth() + 1 !== month ||
        d.getDate() !== day || d.getHours() !== hour || d.getMinutes() !== minute ||
        d.getTime() <= now || d.getTime() > now + 60 * 86400000)
        throw new Error("schedule");
    return d.toISOString();
}
export function buildBody(draft: Draft, now = Date.now()): Body {
    if (!draft.text.trim() || codepoints(draft.text) > 4000)
        throw new Error("text" satisfies Problem);
    if (draft.buttons.length > 6 || draft.buttons.some(b => !b.text.trim() || !validUrl(b.url)))
        throw new Error("buttons" satisfies Problem);
    if (!["all", "buyers", "resellers"].includes(draft.audience) || !["", "fa", "en"].includes(draft.locale))
        throw new Error("buttons");
    return { text: draft.text, buttons: draft.buttons.map(b => ({ text: b.text.trim(), url: b.url })),
        buyers_only: draft.audience === "buyers", resellers_only: draft.audience === "resellers",
        locale: draft.locale || null, scheduled_at: scheduledIso(draft.schedule, now) };
}
export function actions(status: string): Action[] {
    if (status === "queued" || status === "running")
        return ["pause", "cancel"];
    if (status === "paused")
        return ["resume", "cancel"];
    return [];
}
export function audienceKey(audience: Record<string, unknown>): "all" | "buyers" | "resellers" {
    return audience.resellers_only === true ? "resellers" : audience.buyers_only === true ? "buyers" : "all";
}
