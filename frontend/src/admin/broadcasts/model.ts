export const AUDIENCE_KINDS = ["all", "buyers", "resellers", "active", "inactive", "never_bought"] as const;
/** Foxima parity: the buyers of one panel, and people who have not used the bot for N days. */
export const EXTRA_KINDS = ["panel", "idle"] as const;
export const ALL_KINDS = [...AUDIENCE_KINDS, ...EXTRA_KINDS] as const;
export type AudienceKind = typeof ALL_KINDS[number];
/** Buttons that open a section of the bot instead of a link (same list as the server). */
export const BUTTON_ACTIONS = ["home", "buy", "services", "wallet", "trial", "referral", "tickets", "guide"] as const;
export type ButtonAction = typeof BUTTON_ACTIONS[number];
export const IDLE_DAYS_MAX = 365;
export const PIN_HOURS = [0, 24, 48, 72, 168] as const;
/** Missing legacy skipped counters are unknown, not zero. */
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
    skipped: number | null;
    scheduled_at: string | null;
    created_at: string;
    finished_at: string | null;
}
export interface DraftButton {
    text: string;
    url: string;
    action?: ButtonAction;
}
export interface Draft {
    text: string;
    buttons: DraftButton[];
    audience: AudienceKind;
    locale: "" | "fa" | "en";
    schedule: string;
    panelId?: number | null;
    idleDays?: number | null;
    pin?: boolean;
    pinHours?: number;
}
export interface Body {
    kind: AudienceKind;
    text: string;
    buttons: ({ text: string; url: string } | { text: string; action: ButtonAction })[];
    buyers_only: boolean;
    resellers_only: boolean;
    locale: string | null;
    scheduled_at: string | null;
    panel_id?: number;
    idle_days?: number;
    pin?: boolean;
    pin_hours?: number;
}
export type Action = "pause" | "resume" | "cancel";
export type Problem = "text" | "buttons" | "schedule" | "audience";
export const emptyDraft = (): Draft => ({ text: "", buttons: [], audience: "all", locale: "", schedule: "" });
export const codepoints = (value: string): number => Array.from(value).length;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const date = (v: unknown): v is string => typeof v === "string" && Number.isFinite(Date.parse(v));
export function parseRow(value: unknown): BroadcastRow {
    if (!record(value) || !count(value.id) || value.id === 0 || typeof value.text !== "string" ||
        typeof value.status !== "string" || !record(value.audience) ||
        ![value.total, value.sent, value.failed, value.blocked, value.deleted].every(count) ||
        (value.skipped != null && !count(value.skipped)) ||
        !date(value.created_at) || (value.scheduled_at !== null && !date(value.scheduled_at)) ||
        (value.finished_at !== null && !date(value.finished_at)))
        throw new Error("invalid_broadcast");
    // Project only known fields; never render arbitrary response properties as HTML.
    return { id: value.id, text: value.text, status: value.status, audience: value.audience,
        total: value.total as number, sent: value.sent as number, failed: value.failed as number,
        blocked: value.blocked as number, deleted: value.deleted as number, skipped: value.skipped == null ? null : value.skipped as number,
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
    if (draft.buttons.length > 6 || draft.buttons.some(b => !b.text.trim() ||
        (b.action ? !(BUTTON_ACTIONS as readonly string[]).includes(b.action) : !validUrl(b.url))))
        throw new Error("buttons" satisfies Problem);
    if (!(ALL_KINDS as readonly string[]).includes(draft.audience) || !["", "fa", "en"].includes(draft.locale))
        throw new Error("buttons");
    const body: Body = { kind: draft.audience, text: draft.text,
        buttons: draft.buttons.map(b => b.action ? { text: b.text.trim(), action: b.action } : { text: b.text.trim(), url: b.url }),
        buyers_only: draft.audience === "buyers", resellers_only: draft.audience === "resellers",
        locale: draft.locale || null, scheduled_at: scheduledIso(draft.schedule, now) };
    // Foxima parity: extra fields only when used, so the plain body stays exactly as before.
    if (draft.audience === "panel") {
        if (!Number.isSafeInteger(draft.panelId) || (draft.panelId ?? 0) <= 0) throw new Error("audience" satisfies Problem);
        body.panel_id = draft.panelId as number;
    }
    if (draft.audience === "idle") {
        const days = draft.idleDays ?? 0;
        if (!Number.isInteger(days) || days < 1 || days > IDLE_DAYS_MAX) throw new Error("audience" satisfies Problem);
        body.idle_days = days;
    }
    if (draft.pin) {
        const hours = draft.pinHours ?? 0;
        if (!(PIN_HOURS as readonly number[]).includes(hours)) throw new Error("audience" satisfies Problem);
        body.pin = true;
        body.pin_hours = hours;
    }
    return body;
}
export function actions(status: string): Action[] {
    if (status === "queued" || status === "running")
        return ["pause", "cancel"];
    if (status === "paused")
        return ["resume", "cancel"];
    return [];
}
/** Match server precedence, but never label an unsupported kind as "all". */
export function audienceKey(audience: Record<string, unknown>): AudienceKind | "unknownAudience" {
    if (audience.resellers_only === true) return "resellers";
    if ([audience.resellers_only, audience.buyers_only].some(v => v != null && typeof v !== "boolean"))
        return "unknownAudience";
    const kind = audience.kind == null || audience.kind === "" ? "all" : audience.kind;
    if (kind === "all" && audience.buyers_only === true) return "buyers";
    return typeof kind === "string" && (ALL_KINDS as readonly string[]).includes(kind)
        ? kind as AudienceKind : "unknownAudience";
}
export function audiencePreviewPath(draft: Pick<Draft, "audience" | "locale" | "panelId" | "idleDays">): string {
    const query = new URLSearchParams({ kind: draft.audience });
    if (draft.locale) query.set("locale", draft.locale);
    if (draft.audience === "panel" && draft.panelId) query.set("panel_id", String(draft.panelId));
    if (draft.audience === "idle" && draft.idleDays) query.set("idle_days", String(draft.idleDays));
    return `/api/admin/broadcasts/targeted/audience?${query}`;
}
export function previewReady(path: string, dataPath: string | null, loading: boolean, error: boolean, data: number | null): boolean {
    return path === dataPath && !loading && !error && data !== null;
}
export const submitPath = (kind: "test" | "create"): string => `/api/admin/broadcasts/${kind === "test" ? "test" : "targeted"}`;
