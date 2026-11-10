import { ApiError, request, type RequestOptions } from "@shared/http";

let csrfToken = "";
const unauthorizedListeners = new Set<() => void>();

export function setCsrf(token: string): void {
  csrfToken = token;
}

export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

function readCookie(name: string): string {
  const match = document.cookie.split("; ").find((part) => part.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : "";
}

/** Every admin call: CSRF header on writes, and a global sign-out when the session is gone. */
export async function call<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? (options.body !== undefined || options.form ? "POST" : "GET")).toUpperCase();
  const headers: Record<string, string> = { ...options.headers };
  if (method !== "GET" && method !== "HEAD") {
    headers["x-csrf-token"] = csrfToken || readCookie("pb_csrf");
  }
  try {
    return await request<T>(path, { ...options, method, headers });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401 && !path.startsWith("/api/auth/login")) {
      unauthorizedListeners.forEach((listener) => listener());
    }
    throw err;
  }
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export interface Paged<T> {
  items: T[];
  total: number;
}

export interface Me {
  id: number;
  shop_id: number;
  username: string;
  display_name: string | null;
  role: string;
  permissions: string[];
  csrf_token: string;
}

export interface PanelHealth {
  id: number;
  name: string;
  kind: string;
  active: boolean;
  healthy: boolean;
  checked_at: string | null;
}

export interface Dashboard {
  customers?: number;
  customers_today?: number;
  active_services: number;
  revenue_today?: number;
  revenue_week?: number;
  revenue_month?: number;
  orders_month: number;
  spent_month?: number;
  pending_receipts?: number;
  open_tickets?: number;
  series?: { day: string; revenue: number; orders: number }[];
  panels?: PanelHealth[];
  reseller?: {
    balance: number;
    billing_mode: string;
    debt: number;
    credit_limit: number;
    suspended: boolean;
    discount_percent: number;
    expires_at: string | null;
    level: number;
    sub_resellers: number;
  };
}

export type InboxType = "payment" | "ticket" | "order" | "job" | "application";

export interface InboxItem {
  type: InboxType;
  id: number;
  at: string;
  amount?: number;
  customer?: string;
  duplicate_of?: number | null;
  subject?: string;
  error?: string | null;
  kind?: string;
}

export interface Customer {
  id: number;
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  phone: string | null;
  locale: string;
  is_banned: boolean;
  ban_reason: string | null;
  tags: string[];
  note: string | null;
  color: string | null;
  risk: string;
  card_trust?: string;
  hidden_panel_ids?: number[];
  created_at: string;
  last_seen_at: string | null;
  blocked_bot_at: string | null;
  referrer_id: number | null;
}

export interface Service {
  id: number;
  customer_id: number;
  panel_id: number;
  plan_id: number | null;
  panel_username: string;
  label: string | null;
  status: string;
  is_trial: boolean;
  data_limit: number;
  used_bytes: number;
  expire_at: string | null;
  subscription_url: string | null;
  branded_url: string | null;
  disabled_reason: string | null;
  staff_note?: string | null;
  synced_at: string | null;
  created_at: string;
  panel: string | null;
  customer: string | null;
}

export interface Order {
  id: number;
  customer_id: number;
  kind: string;
  status: string;
  plan_id: number | null;
  service_id: number | null;
  base_price: number;
  reseller_discount: number;
  code_discount: number;
  amount: number;
  source: string | null;
  error: string | null;
  created_at: string;
  paid_at: string | null;
  completed_at: string | null;
  customer: string;
  plan: string | null;
}

export interface Payment {
  id: number;
  customer_id: number;
  order_id: number | null;
  purpose: string;
  provider: string;
  status: string;
  amount: number;
  currency: string;
  provider_amount: number | null;
  provider_currency: string | null;
  provider_tx_id: string | null;
  receipt_text: string | null;
  duplicate_of_id: number | null;
  review_note: string | null;
  created_at: string;
  paid_at: string | null;
  fulfilled_at: string | null;
  reviewed_at: string | null;
  failure_reason: string | null;
  customer: string;
  has_receipt: boolean;
  reviewable: boolean;
  review_amount: number | null;
  voidable: boolean;
  void: PaymentVoid | null;
}

/** What a void did (REF-01), stored on the payment. */
export interface PaymentVoid {
  at: string;
  by: string;
  reason: string;
  note: string | null;
  order_id: number | null;
  service_id: number | null;
  kept: string[];
  block_card: boolean;
}

export interface CustomerDetail {
  customer: Customer;
  balance: number;
  referrals: number;
  reseller: { id: number; group_id: number | null; active: boolean } | null;
  services: Service[];
  orders: { id: number; kind: string; status: string; amount: number; created_at: string; completed_at: string | null; error: string | null }[];
  payments: { id: number; provider: string; status: string; amount: number; created_at: string; paid_at: string | null }[];
  ledger: { id: number; amount: number; balance_after: number; kind: string; memo: string | null; at: string }[];
}

export interface Location {
  id: number;
  title: string;
  flag: string | null;
  sort: number;
  is_active: boolean;
}

export interface Category {
  id: number;
  title: string;
  sort: number;
  is_active: boolean;
}

export interface Plan {
  id: number;
  title: string;
  description: string | null;
  category_id: number | null;
  location_id: number | null;
  panel_id: number | null;
  price: number;
  days: number;
  data_gb: number;
  ip_limit: number;
  audience: string;
  renewable: boolean;
  sort: number;
  is_active: boolean;
  button_style: { emoji?: string; badge?: string };
  on_hold: boolean;
  username_prefix: string | null;
  panel_options: Record<string, unknown>;
  addons: { extras?: boolean; extra_gb_price?: number | null; extra_day_price?: number | null };
  delivery: "panel" | "stock";
  renew_mode?: string | null;
  services?: number;
  stock?: { available: number; sold: number; disabled: number };
}

export interface CatalogData {
  locations: Location[];
  categories: Category[];
  plans: Plan[];
}

export interface PanelRow {
  id: number;
  name: string;
  kind: string;
  base_url: string;
  username: string;
  location_id: number | null;
  capacity: number | null;
  username_method: string;
  username_prefix: string;
  is_active: boolean;
  is_healthy: boolean;
  health_checked_at: string | null;
  health_error: string | null;
  allow_private_address: boolean;
  options: Record<string, unknown>;
  has_api_token: boolean;
  tls_pinned: boolean;
  webhook_enabled: boolean;
  load?: number;
  stats: PanelStats;
  stats_at: string | null;
  down_since: string | null;
  uptime?: { day: number | null; week: number | null } | null;
}

export interface PanelStats {
  version?: string | null;
  users_total?: number | null;
  users_active?: number | null;
  users_online?: number | null;
  cpu_percent?: number | null;
  cpu_cores?: number | null;
  mem_used?: number | null;
  mem_total?: number | null;
  traffic_in?: number | null;
  traffic_out?: number | null;
  speed_in?: number | null;
  speed_out?: number | null;
  admin?: {
    username?: string;
    owner: boolean;
    role?: string | null;
    users?: number | null;
    max_users?: number | null;
    used_traffic?: number | null;
    data_limit?: number | null;
    disabled?: boolean;
  };
  nodes?: { id: number; name: string; status: string; message?: string | null; uplink?: number | null; downlink?: number | null; cpu_percent?: number | null; mem_used?: number | null; mem_total?: number | null }[];
}

export interface Discount {
  id: number;
  code: string;
  kind: string;
  value: number;
  max_discount: number | null;
  min_amount: number;
  max_uses: number | null;
  used_count: number;
  per_customer_limit: number;
  plan_ids: number[];
  first_purchase_only: boolean;
  sticky: boolean;
  customer_telegram_id: number | null;
  starts_at: string | null;
  expires_at: string | null;
  is_active: boolean;
  note: string | null;
  created_at: string;
}

export interface Broadcast {
  id: number;
  text: string;
  audience: Record<string, unknown>;
  status: string;
  total: number;
  sent: number;
  failed: number;
  created_at: string;
  finished_at: string | null;
}

export interface ResellerGroup {
  id: number;
  title: string;
  discount_percent: number;
  min_topup: number;
  max_active_services: number | null;
  is_active: boolean;
  hidden_panel_ids: number[];
}

export interface ResellerPrice {
  id: number;
  plan_id: number;
  group_id: number | null;
  profile_id: number | null;
  price: number;
}

export interface ResellerProfile {
  id: number;
  customer_id: number;
  group_id: number | null;
  permissions: string[];
  is_active: boolean;
  expires_at: string | null;
  max_purchases_per_day: number | null;
  parent_id: number | null;
  discount_override: number | null;
  max_subresellers: number | null;
  billing_mode: "prepaid" | "usage" | "hourly";
  gb_price: number;
  hourly_price: number;
  credit_limit: number;
  billing_debt: number;
  last_billed_at: string | null;
  low_balance_threshold: number | null;
  max_active_services: number | null;
  max_total_gb: number | null;
  suspended_at: string | null;
  suspend_reason: string | null;
  created_at: string;
  customer: string;
  telegram_id: number;
  balance: number;
  discount_percent: number;
  is_current: boolean;
}

export interface BillingSnapshot {
  id: number;
  period_start: string;
  period_end: string;
  used_bytes: number;
  service_hours: number;
  active_services: number;
  amount: number;
  charged: number;
  debt_after: number;
}

export interface BillingView {
  billing_mode: string;
  debt: number;
  credit_limit: number;
  suspended_at: string | null;
  items: BillingSnapshot[];
}

export interface SubReseller {
  id: number;
  customer_id: number;
  customer: string | null;
  telegram_id: number | null;
  discount_override: number | null;
  discount_percent: number;
  is_active: boolean;
  expires_at: string | null;
  balance: number;
  created_at: string;
}

export interface ResellerData {
  groups: ResellerGroup[];
  profiles: ResellerProfile[];
  applications: { id: number; customer_id: number; customer: string; text: string | null; created_at: string }[];
  permissions: string[];
}

export interface StaffRow {
  id: number;
  role: string;
  username: string;
  telegram_id: number | null;
  display_name: string | null;
  is_active: boolean;
  reseller_profile_id: number | null;
  last_login_at: string | null;
  created_at: string;
}

export interface Ticket {
  id: number;
  subject: string;
  status: string;
  source: string;
  customer_id: number;
  created_at: string;
  updated_at: string;
  customer: string | null;
}

export interface TicketMessage {
  id: number;
  author_type: string;
  author_id: number | null;
  text: string | null;
  attachment_kind: string | null;
  has_attachment: boolean;
  created_at: string;
}

export interface SettingItem {
  key: string;
  type: string;
  secret: boolean;
  value: unknown;
}

export interface TextItem {
  key: string;
  variables: string[];
  defaults: Record<string, string>;
  overrides: Record<string, string>;
}

export interface Channels {
  forced: {
    id: number;
    chat_id: string;
    title: string;
    url: string;
    is_active: boolean;
    sort: number;
    warning: string | null;
  }[];
  trust: {
    id: number;
    chat_id: number;
    title: string;
    url: string;
    kinds: string[];
    is_active: boolean;
    sort: number;
    warning: string | null;
  }[];
  logs: { id: number; topic: string; chat_id: number; thread_id: number | null }[];
  contacts: { id: number; title: string; url: string; sort: number }[];
  topics: string[];
  trust_kinds: string[];
}

export interface AuditRow {
  id: number;
  actor_type: string;
  actor_id: number | null;
  action: string;
  entity: string | null;
  entity_id: number | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip: string | null;
  created_at: string;
}

export interface JobRow {
  id: number;
  queue: string;
  kind: string;
  status: string;
  attempts: number;
  max_attempts: number;
  run_at: string;
  last_error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface SalesReport {
  start: string;
  end: string;
  days: { day: string; orders: number; amount: number }[];
  plans: { plan: string | null; kind: string; orders: number; amount: number }[];
  providers?: { provider: string; payments: number; amount: number }[];
  totals: { orders: number; amount: number };
}

export { ApiError };

export interface AccountPlan {
  id: number;
  panel_id: number;
  /** One of the shop's categories; null: listed under "other plans". */
  category_id: number | null;
  title: string;
  description: string | null;
  role_id: number;
  data_gb: number;
  max_users: number | null;
  days: number;
  price: number;
  gb_price: number;
  user_price: number;
  day_price: number;
  /** null: the shop's range from Settings. */
  gb_min: number | null;
  gb_max: number | null;
  days_min: number | null;
  days_max: number | null;
  renew_packages_enabled: boolean;
  renew_packages: RenewPackage[];
  renew_custom_enabled: boolean;
  /** null: time added to what is left, a fresh quota, usage reset. */
  renew_mode: "extend_reset" | "extend_carry" | "restart_reset" | "restart_carry" | null;
  renewable: boolean;
  audience: "all" | "customers" | "resellers";
  discounts_allowed: boolean;
  reseller_discount: boolean;
  sort: number;
  is_active: boolean;
}

/** A ready-made renewal of an admin account plan; gb 0 means no traffic quota. */
export interface RenewPackage {
  days: number;
  gb: number;
  price: number;
}

export interface PanelAccountRow {
  id: number;
  customer_id: number;
  customer: string | null;
  telegram_id: number | null;
  panel_id: number;
  panel: string | null;
  plan_id: number | null;
  plan: string | null;
  username: string;
  data_limit: number;
  max_users: number | null;
  expire_at: string | null;
  status: "active" | "paused" | "expired" | "deleted";
  used_bytes: number;
  users_count: number;
  panel_status: string | null;
  synced_at: string | null;
  note: string | null;
  created_at: string;
}
