import type { Ticket, TicketList } from "./api";

export const TICKET_STATUSES = ["open", "answered", "closed"] as const;
export type TicketFilter = "" | Ticket["status"];
export interface TicketFilters { status?: TicketFilter; department?: number }

export function ticketStatus(value: string | null): TicketFilter {
  return TICKET_STATUSES.includes(value as Ticket["status"]) ? value as TicketFilter : "";
}
export function ticketNumber(value: string | null, allowZero = false): number | undefined {
  if (value === null || !/^\d+$/.test(value)) return undefined;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= (allowZero ? 0 : 1) ? number : undefined;
}
export function ticketListPath(before?: number, filters: TicketFilters = {}): string {
  const params = new URLSearchParams();
  if (before !== undefined) {
    if (!Number.isSafeInteger(before) || before <= 0) throw Error("invalid_ticket_cursor");
    params.set("before", String(before));
  }
  if (filters.status) {
    if (!TICKET_STATUSES.includes(filters.status)) throw Error("invalid_ticket_status");
    params.set("status", filters.status);
  }
  if (filters.department !== undefined) {
    if (!Number.isSafeInteger(filters.department) || filters.department < 0) throw Error("invalid_ticket_department");
    params.set("department_id", String(filters.department));
  }
  return `/tickets${params.size ? `?${params}` : ""}`;
}
export function ticketsReturnTo(value: unknown): string {
  if (typeof value !== "string" || !/^\/tickets(?:\?|$)/.test(value)) return "/tickets";
  const params = new URL(value, "https://local.invalid").searchParams;
  return ticketListPath(ticketNumber(params.get("before")), {
    status: ticketStatus(params.get("status")), department: ticketNumber(params.get("department_id"), true),
  });
}
export function readTicketPage(value: unknown, before?: number, filters: TicketFilters = {}): TicketList {
  if (!value || typeof value !== "object") throw Error("invalid_ticket_page");
  const page = value as TicketList;
  if (!Array.isArray(page.items) || page.items.length > 20) throw Error("invalid_ticket_page");
  // An older backend ignores unknown filter parameters. Do not present its
  // unfiltered page as successful filtered results during an out-of-order rollout.
  if ((filters.status || filters.department !== undefined) && !Array.isArray(page.filter_departments)) throw Error("unsupported_ticket_filters");
  let ceiling = before ?? Infinity;
  for (const item of page.items) {
    if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 || item.id >= ceiling ||
      typeof item.subject !== "string" || !TICKET_STATUSES.includes(item.status) ||
      (item.department_id !== null && (!Number.isSafeInteger(item.department_id) || item.department_id <= 0))) throw Error("invalid_ticket_page");
    if (filters.status && item.status !== filters.status) throw Error("invalid_ticket_page");
    if (filters.department !== undefined && item.department_id !== (filters.department || null)) throw Error("invalid_ticket_page");
    ceiling = item.id;
  }
  if (page.next_before !== null && (page.items.length !== 20 || page.next_before !== ceiling)) throw Error("invalid_ticket_page");
  for (const desks of [page.departments, page.filter_departments ?? page.departments]) {
    if (!Array.isArray(desks)) throw Error("invalid_ticket_page");
    const seen = new Set<number>();
    for (const desk of desks) {
      if (!desk || !Number.isSafeInteger(desk.id) || desk.id <= 0 || typeof desk.title !== "string" || seen.has(desk.id)) throw Error("invalid_ticket_page");
      seen.add(desk.id);
    }
  }
  return page;
}
