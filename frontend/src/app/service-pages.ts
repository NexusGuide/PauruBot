import type { Service } from "./api";

export const SERVICE_PAGE_SIZE = 20;
export const SERVICE_STATUSES = ["pending", "active", "on_hold", "limited", "expired", "disabled"] as const;
export type ServiceFilter = "" | typeof SERVICE_STATUSES[number];
export interface ServicePage { items: Service[]; next_before: number | null }

export function serviceFilter(value: string | null): ServiceFilter {
  return SERVICE_STATUSES.includes(value as typeof SERVICE_STATUSES[number]) ? value as ServiceFilter : "";
}

/** A location group of the services list (F-19): a location id, 0 for "no location", or null for all. */
export function serviceGroup(value: string | null): number | null {
  if (value === null || !/^\d{1,10}$/.test(value)) return null;
  const n = Number(value);
  return Number.isSafeInteger(n) && n <= 2 ** 31 - 1 ? n : null;
}

export function servicePageUrl(q: string, status: ServiceFilter, before?: number, group: number | null = null) {
  const params = new URLSearchParams({ limit: String(SERVICE_PAGE_SIZE) });
  if (q.trim()) params.set("q", q.trim().slice(0, 120));
  if (status) params.set("status", status);
  if (group !== null) params.set("group", String(group));
  if (before !== undefined) {
    if (!Number.isSafeInteger(before) || before <= 0) throw Error("invalid_service_cursor");
    params.set("before", String(before));
  }
  return `/services?${params}`;
}

export function readServicePage(value: unknown, before?: number): ServicePage {
  if (!value || typeof value !== "object" || !("items" in value) || !("next_before" in value)) throw Error("invalid_service_page");
  const { items, next_before } = value;
  if (!Array.isArray(items) || items.length > SERVICE_PAGE_SIZE) throw Error("invalid_service_page");
  let ceiling = before ?? Infinity;
  for (const item of items) {
    if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 || item.id >= ceiling) throw Error("invalid_service_page");
    ceiling = item.id;
  }
  if (next_before !== null && (!items.length || next_before !== ceiling || items.length !== SERVICE_PAGE_SIZE)) throw Error("invalid_service_page");
  return { items: items as Service[], next_before: next_before as number | null };
}

/** Only a canonical local services-list route can become the detail's back target. */
export function servicesReturnTo(value: unknown): string {
  if (typeof value !== "string" || !/^\/services(?:\?|$)/.test(value)) return "/services";
  const url = new URL(value, "https://local.invalid");
  const q = (url.searchParams.get("q") ?? "").trim().slice(0, 120);
  const status = serviceFilter(url.searchParams.get("status"));
  const group = serviceGroup(url.searchParams.get("group"));
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (group !== null) params.set("group", String(group));
  return `/services${params.size ? `?${params}` : ""}`;
}

export interface ServiceGroup { id: number; title: string | null; flag: string | null; count: number }

/** Groups worth showing: only when the services live in two or more places (never for one service). */
export function readServiceGroups(value: unknown): ServiceGroup[] {
  if (!value || typeof value !== "object" || !("groups" in value) || !Array.isArray(value.groups)) return [];
  const groups = value.groups.filter((g): g is ServiceGroup =>
    Boolean(g) && Number.isSafeInteger(g.id) && g.id >= 0 && Number.isSafeInteger(g.count) && g.count > 0
    && (g.title === null || typeof g.title === "string") && (g.flag === null || typeof g.flag === "string"));
  return groups.length >= 2 ? groups : [];
}
