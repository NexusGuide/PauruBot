export const PRIORITIES = ["low", "normal", "high", "urgent"] as const;
export type Priority = typeof PRIORITIES[number];
export interface Ticket {
    id: number;
    subject: string;
    status: string;
    source: string;
    customer_id: number;
    department_id: number | null;
    priority: Priority;
    assigned_staff_id: number | null;
    last_customer_at: string | null;
    last_staff_at: string | null;
    created_at: string;
    updated_at: string;
    awaiting: boolean;
    customer?: string | null;
}
export interface Message {
    id: number;
    author_type: string;
    author_id: number | null;
    text: string | null;
    attachment_kind: string | null;
    has_attachment: boolean;
    is_internal: boolean;
    created_at: string;
}
export interface Detail {
    ticket: Ticket;
    customer: string | null;
    messages: Message[];
}
export interface Department {
    id: number;
    title: string;
    sort: number;
    is_active: boolean;
    log_chat_id: number | null;
    log_thread_id: number | null;
}
export interface Canned {
    id: number;
    title: string;
    text: string;
    department_id: number | null;
    sort: number;
}
export const points = (v: string): number => Array.from(v).length;
export const validText = (v: string, max = 4000): boolean => !!v.trim() && points(v) <= max;
export const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
export const positive = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const nullableId = (v: unknown) => v === null || positive(v);
const nullableString = (v: unknown) => v === null || typeof v === "string";
const iso = (v: unknown) => typeof v === "string" && Number.isFinite(Date.parse(v));
const nullableDate = (v: unknown) => v === null || iso(v);
const integer = (v: unknown) => typeof v === "number" && Number.isSafeInteger(v);
function invalid(): never { throw new Error("ticket_response_invalid"); }
export function parseTicket(v: unknown): Ticket {
    if (!record(v) || !positive(v.id) || typeof v.subject !== "string" || typeof v.status !== "string" || typeof v.source !== "string" || !positive(v.customer_id) || !nullableId(v.department_id) || !PRIORITIES.includes(v.priority as Priority) || !nullableId(v.assigned_staff_id) || !nullableDate(v.last_customer_at) || !nullableDate(v.last_staff_at) || !iso(v.created_at) || !iso(v.updated_at) || typeof v.awaiting !== "boolean" || (v.customer !== undefined && !nullableString(v.customer)))
        return invalid();
    return { id: v.id, subject: v.subject, status: v.status, source: v.source, customer_id: v.customer_id, department_id: v.department_id as number | null, priority: v.priority as Priority, assigned_staff_id: v.assigned_staff_id as number | null, last_customer_at: v.last_customer_at as string | null, last_staff_at: v.last_staff_at as string | null, created_at: v.created_at as string, updated_at: v.updated_at as string, awaiting: v.awaiting, customer: v.customer as string | null | undefined };
}
export function parseMessage(v: unknown): Message {
    if (!record(v) || !positive(v.id) || typeof v.author_type !== "string" || !nullableId(v.author_id) || !nullableString(v.text) || !nullableString(v.attachment_kind) || typeof v.has_attachment !== "boolean" || typeof v.is_internal !== "boolean" || !iso(v.created_at) || v.is_internal && v.author_type !== "staff")
        return invalid();
    return { id: v.id, author_type: v.author_type, author_id: v.author_id as number | null, text: v.text as string | null, attachment_kind: v.attachment_kind as string | null, has_attachment: v.has_attachment, is_internal: v.is_internal, created_at: v.created_at as string };
}
export function parseDetail(v: unknown): Detail {
    if (!record(v) || !nullableString(v.customer) || !Array.isArray(v.messages) || v.messages.length > 200)
        return invalid();
    const messages = v.messages.map(parseMessage);
    if (new Set(messages.map(m => m.id)).size !== messages.length)
        return invalid();
    return { ticket: parseTicket(v.ticket), customer: v.customer as string | null, messages };
}
export function parseList(v: unknown): {
    items: Ticket[];
    total: number;
} {
    if (!record(v) || !Array.isArray(v.items) || v.items.length > 25 || !integer(v.total) || (v.total as number) < v.items.length)
        return invalid();
    const items = v.items.map(parseTicket);
    if (new Set(items.map(t => t.id)).size !== items.length)
        return invalid();
    return { items, total: v.total as number };
}
export function parseDepartment(v: unknown): Department {
    if (!record(v) || !positive(v.id) || typeof v.title !== "string" || !integer(v.sort) || typeof v.is_active !== "boolean" || !(v.log_chat_id === null || integer(v.log_chat_id)) || !nullableId(v.log_thread_id))
        return invalid();
    return { id: v.id, title: v.title, sort: v.sort as number, is_active: v.is_active, log_chat_id: v.log_chat_id as number | null, log_thread_id: v.log_thread_id as number | null };
}
export function parseCanned(v: unknown): Canned {
    if (!record(v) || !positive(v.id) || typeof v.title !== "string" || typeof v.text !== "string" || !nullableId(v.department_id) || !integer(v.sort))
        return invalid();
    return { id: v.id, title: v.title, text: v.text, department_id: v.department_id as number | null, sort: v.sort as number };
}
export function collection<T>(value: unknown, parse: (v: unknown) => T): T[] { if (!record(value) || !Array.isArray(value.items))
    return invalid(); return value.items.map(parse); }
export const parseDepartments = (v: unknown): Department[] => collection(v, parseDepartment);
export const parseCannedList = (v: unknown): Canned[] => collection(v, parseCanned);
export function ok(v: unknown): void { if (!record(v) || v.ok !== true)
    invalid(); }
export function newId(v: unknown): number { if (!record(v) || !positive(v.id))
    return invalid(); return v.id; }
export function renderCanned(text: string, name: string, ticket: number): string { return Array.from(text.split('{name}').join(name).split('{ticket}').join(String(ticket))).slice(0, 4000).join(''); }
export function numeric(value: string, { positiveOnly = false }: {
    positiveOnly?: boolean;
} = {}): number | null {
    if (value === "")
        return null;
    if (!/^-?\d+$/.test(value))
        throw new Error("number");
    const n = Number(value);
    if (!Number.isSafeInteger(n) || (positiveOnly && n <= 0))
        throw new Error("number");
    return n;
}
export function ticketPatch(row: Ticket, department: number | null, priority: Priority, assignee: number | null): Record<string, unknown> {
    if (!nullableId(department) || !nullableId(assignee) || !PRIORITIES.includes(priority))
        throw new Error("metadata");
    const body: Record<string, unknown> = {};
    if (department !== row.department_id)
        body.department_id = department;
    if (priority !== row.priority)
        body.priority = priority;
    if (assignee !== row.assigned_staff_id)
        body.assigned_staff_id = assignee;
    return body;
}
export function listPath(params: URLSearchParams): {
    path: string;
    page: number;
} {
    const q = new URLSearchParams();
    const raw = Number(params.get('page') || 0);
    const page = Number.isSafeInteger(raw) && raw >= 0 && raw < 1000000 ? raw : 0;
    const status = params.get('status'), priority = params.get('priority'), department = params.get('department_id'), assigned = params.get('assigned'), awaiting = params.get('awaiting');
    if (status && ['open', 'answered', 'closed'].includes(status))
        q.set('status', status);
    if (priority && PRIORITIES.includes(priority as Priority))
        q.set('priority', priority);
    if (department && /^\d+$/.test(department) && Number.isSafeInteger(Number(department)))
        q.set('department_id', String(Number(department)));
    if (assigned && ['me', 'none'].includes(assigned))
        q.set('assigned', assigned);
    if (awaiting === 'true' || awaiting === 'false')
        q.set('awaiting', awaiting);
    q.set('limit', '25');
    q.set('offset', String(page * 25));
    return { path: '/api/admin/tickets?' + q.toString(), page };
}
