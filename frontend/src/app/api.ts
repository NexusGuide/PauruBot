import { ticketListPath, type TicketFilters } from "./ticket-pages";
import { request } from "@shared/http";
import type { Body as AutoRenewBody } from "./autoRenew/model";
import { initData } from "./telegram";
import { servicePageUrl, type ServiceFilter } from "./service-pages";

export interface ServiceAlertPolicy {
  warn_remaining_percent: number;
  warn_days: number;
}

export interface TransferPolicy { enabled: boolean; minimum: number; maximum: number; daily_maximum: number; currency: string }
export interface TransferQuote { token: string; amount: number; currency: string; expires_at: number; recipient: { telegram_id: number; name: string | null; username: string | null } }

export interface Me {
  id: number;
  telegram_id: number;
  name: string | null;
  username: string | null;
  locale: string;
  balance: number;
  currency: string;
  shop: { title: string; bot_username: string | null; support: string | null; languages: string[] };
  trial: { available: boolean; reason: string | null; days: number; gb: number };
  referral: { enabled: boolean; percent: number; count: number };
  reseller: { active: boolean; discount_percent: number };
  panel_accounts: boolean;
  extras: { enabled: boolean; gb_price: number; day_price: number; upgrade: boolean };
  payment_methods: string[];
  topup_bounds: [number, number];
  staff: StaffAccess | null;
  preferences: MarketingPreference;
  alerts_muted: boolean;
  /** Absent on older backends; never replace it with hard-coded UI thresholds. */
  alert_policy?: ServiceAlertPolicy | null;
  profile: { phone_masked: string | null; member_since: string };
}

export interface MarketingPreference {
  marketing_opt_out: boolean;
  changed_at: string | null;
}

export interface UsagePoint { at: string; used_bytes: number; break_before: boolean }
export interface UsageHistory {
  service_id: number;
  days: 7 | 30;
  resolution: "hourly_last_observation";
  from: string;
  to: string;
  last_observed_at: string | null;
  points: UsagePoint[];
}

export interface Ticket {
  id: number;
  subject: string;
  status: "open" | "answered" | "closed";
  department_id: number | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface TicketMessage {
  id: number;
  from: "me" | "support";
  text: string | null;
  has_attachment: boolean;
  created_at: string | null;
}
export interface TicketList {
  items: Ticket[];
  departments: { id: number; title: string }[];
  filter_departments?: { id: number; title: string }[];
  next_before: number | null;
}
export interface TicketThreadData {
  ticket: Ticket;
  messages: TicketMessage[];
  next_before: number | null;
}

export interface HelpArticle { id: number; title: string; text: string; url: string | null }
export interface HelpApp { id: number; name: string; platform: string; store_url: string | null; release_url: string | null }
export interface CustomerHelp { articles: HelpArticle[]; apps: HelpApp[]; bot_url: string | null }

export interface StaffAccess {
  role: string;
  scoped: boolean;
  dashboard: boolean;
  review: boolean;
}

export interface StaffDashboard {
  customers?: number;
  customers_today?: number;
  active_services?: number;
  revenue_today?: number;
  revenue_month?: number;
  orders_month?: number;
  pending_receipts?: number;
  open_tickets?: number;
  spent_month?: number;
  panels?: { id: number; name: string; active: boolean; healthy: boolean | null }[];
  reseller?: { balance: number; debt: number; suspended: boolean; discount_percent: number; sub_resellers: number };
}

export interface Review {
  id: number;
  provider: string;
  amount: number;
  review_amount: number | null;
  customer: string | null;
  customer_id: number;
  created_at: string | null;
  has_receipt: boolean;
  receipt_text: string | null;
  duplicate_of_id: number | null;
}

export interface Plan {
  id: number;
  title: string;
  description: string | null;
  days: number;
  data_gb: number;
  price: number;
  base_price: number;
  category_id: number | null;
  location_id: number | null;
  panel_id: number | null;
  renewable: boolean;
  on_hold: boolean;
  emoji: string | null;
  badge: string | null;
}

export interface CustomOffer {
  min_gb: number;
  max_gb: number;
  min_days: number;
  max_days: number;
  gb_price: number;
  day_price: number;
  base_price: number;
  discount_percent: number;
}

export interface Catalog {
  locations: { id: number; title: string; flag: string | null }[];
  categories: { id: number; title: string }[];
  plans: Plan[];
  custom: CustomOffer | null;
}

/** A PasarGuard admin plan the customer can buy (TEN-14). */
export interface PanelPlan {
  id: number;
  title: string;
  description: string | null;
  data_gb: number;
  max_users: number | null;
  days: number;
  price: number;
  gb_price: number;
  user_price: number;
}

export interface AccountPlans {
  available: boolean;
  plans: PanelPlan[];
}

/** A panel admin account the customer owns: status, quota, users, expiry. */
export interface PanelAccount {
  id: number;
  username: string;
  status: "active" | "paused" | "expired";
  data_limit: number;
  used_bytes: number;
  max_users: number | null;
  users_count: number;
  expire_at: string | null;
  plan_title: string | null;
  price: number;
  gb_price: number;
  user_price: number;
  login_url: string;
}

export interface AccountList {
  available: boolean;
  items: PanelAccount[];
}

export interface Service {
  id: number;
  label: string;
  username: string;
  status: string;
  is_trial: boolean;
  data_limit: number;
  used_bytes: number;
  expire_at: string | null;
  subscription_url: string | null;
  location: string | null;
  plan_id: number | null;
  panel_id: number;
  extras: { gb_price: number | null; day_price: number | null } | null;
  delivery?: "subscription" | "wireguard" | "account";
  account?: { username: string; password: string | null };
  download_url?: string | null;
  changes?: string[];
}

export interface Order {
  id: number;
  kind: string;
  status: string;
  base_price: number;
  reseller_discount: number;
  code_discount: number;
  amount: number;
  service_id: number | null;
  plan_id: number | null;
  params: {
    extra_gb?: number;
    extra_days?: number;
    credit?: number;
    label?: string;
    custom?: { gb: number; days: number };
    queued?: boolean;
    queued_until?: string;
    account_plan_id?: number;
    account_id?: number;
    gb?: number;
    users?: number;
  };
  expires_at: string | null;
}

export interface PaymentStart {
  payment_id: number;
  provider: string;
  amount: number;
  accounts?: { number: string; holder: string; bank: string }[];
  redirect_url?: string;
  invoice_link?: string;
  crypto?: CryptoInstructions;
}

export interface CryptoInstructions {
  network: string;
  label: string;
  address: string;
  amount: string;
  memo: string | null;
  expires_at: string | null;
}

/** A channel the customer must join before the app answers anything else (GRW-07). */
export interface JoinChannel {
  title: string;
  url: string;
}

export interface HistoryItem {
  id: number;
  amount: number;
  balance_after: number;
  kind: string;
  created_at: string;
}

const auth = () => ({ "X-Telegram-Init-Data": initData() });
const get = <T>(path: string, signal?: AbortSignal) => request<T>(`/api/webapp${path}`, { headers: auth(), signal });
const post = <T>(path: string, body: unknown = {}) =>
  request<T>(`/api/webapp${path}`, { method: "POST", body, headers: auth() });

export const api = {
  transferPolicy: () => get<TransferPolicy>("/wallet/transfer-policy"),
  transferQuote: (recipient: string, amount: number) => post<TransferQuote>("/wallet/transfers/quote", { recipient, amount }),
  transferConfirm: (token: string) => post<{ journal_id: number; amount: number; replayed: boolean; balance: number }>("/wallet/transfers/confirm", { token }),
  me: () => get<Me>("/me"),
  join: () => get<{ channels: JoinChannel[] }>("/join"),
  checkJoin: () => post<{ ok: boolean; channels: JoinChannel[] }>("/join/check"),
  setAlerts: (muted: boolean) => post<{ alerts_muted: boolean }>("/me/alerts", { alerts_muted: muted }),
  setMarketing: (optOut: boolean) =>
    post<MarketingPreference>("/me/marketing", { marketing_opt_out: optOut }),
  setLocale: (locale: string) => post<{ locale: string }>("/me/locale", { locale }),
  help: (signal?: AbortSignal) => get<CustomerHelp>("/help", signal),
  tickets: (before?: number, signal?: AbortSignal, filters?: TicketFilters) => get<TicketList>(ticketListPath(before, filters), signal),
  ticket: (id: number, before?: number, signal?: AbortSignal) => get<TicketThreadData>(`/tickets/${id}${before ? `?before=${before}` : ""}`, signal),
  newTicket: (body: { subject: string; text: string; department_id: number | null }) => post<{ ticket: Ticket }>("/tickets", body),
  replyTicket: (id: number, text: string) => post<{ ticket: Ticket }>(`/tickets/${id}/reply`, { text }),
  closeTicket: (id: number) => post<{ ticket: Ticket }>(`/tickets/${id}/close`),
  catalog: () => get<Catalog>("/catalog"),
  accountPlans: (signal?: AbortSignal) => get<AccountPlans>("/account-plans", signal),
  accounts: (signal?: AbortSignal) => get<AccountList>("/accounts", signal),
  account: (id: number) => get<{ account: PanelAccount }>(`/accounts/${id}`),
  accountLogin: (id: number) => get<{ url: string; username: string; password: string }>(`/accounts/${id}/login`),
  refreshAccount: (id: number) => post<{ account: PanelAccount }>(`/accounts/${id}/refresh`),
  createOrder: (body: {
    intent_key?: string;
    kind: string;
    plan_id?: number;
    service_id?: number;
    extra_gb?: number;
    extra_days?: number;
    code?: string;
    custom_gb?: number;
    custom_days?: number;
    label?: string;
    account_plan_id?: number;
    account_id?: number;
    account_amount?: number;
  }) => post<{ order: Order; balance: number; due: number }>("/orders", body),
  resellerDesk: (days: number) => get<ResellerDesk>(`/reseller/desk?days=${days}`),
  renewMany: (ids: number[]) =>
    post<{ renewed: number[]; failed: { id: number; error: string }[]; balance: number }>("/reseller/renew", { service_ids: ids }),
  order: (id: number) => get<{ order: Order }>(`/orders/${id}`),
  payOrder: (id: number, method: string) =>
    post<{ order: Order; payment?: PaymentStart }>(`/orders/${id}/pay`, { method }),
  cancelOrder: (id: number) => post<{ ok: boolean }>(`/orders/${id}/cancel`),
  topup: (amount: number, method: string, intent_key?: string) => post<{ payment: PaymentStart }>("/wallet/topup", { amount, method, intent_key }),
  payment: (id: number) => get<{ id: number; status: string; amount: number; fulfilled: boolean; provider: string; order_id: number | null }>(`/payments/${id}`),
  receiptState: (id: number) => get<unknown>(`/payments/${id}/receipt-state`),
  receipts: (before?: number) => get<unknown>(`/receipts${before ? `?before=${before}` : ""}`),
  sendReceipt: (id: number, file: File, revision: string) => {
    const form = new FormData(); form.append("file", file); form.append("expected_revision", revision);
    return request<unknown>(`/api/webapp/payments/${id}/receipt-upload`, { method: "POST", form, headers: auth() });
  },
  uploadReceipt: (id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ status: string }>(`/api/webapp/payments/${id}/receipt`, {
      method: "POST",
      form,
      headers: auth(),
    });
  },
  redeemGift: (code: string) => post<{ amount: number; balance: number }>("/wallet/gift", { code }),
  history: (before?: number) => get<{ items: HistoryItem[] }>(`/wallet/history${before ? `?before=${before}` : ""}`),
  services: () => get<{ items: Service[] }>("/services"),
  servicePage: (q: string, status: ServiceFilter, before: number | undefined, signal: AbortSignal) =>
    request<unknown>(`/api/webapp${servicePageUrl(q, status, before)}`, { headers: auth(), signal }),
  service: (id: number, refresh = false) => get<{ service: Service }>(`/services/${id}${refresh ? "?refresh=true" : ""}`),
  usage: (id: number, days: 7 | 30, signal?: AbortSignal) =>
    request<UsageHistory>(`/api/webapp/services/${id}/usage?days=${days}`, { headers: auth(), signal }),
  autoRenew: (id: number) => get<unknown>(`/services/${id}/auto-renew`),
  setAutoRenew: (id: number, body: AutoRenewBody) => post<unknown>(`/services/${id}/auto-renew`, body),
  links: (id: number) => get<{ links: string[] }>(`/services/${id}/links`),
  revoke: (id: number) => post<{ service: Service }>(`/services/${id}/revoke`),
  rename: (id: number, label: string) => post<{ service: Service }>(`/services/${id}/rename`, { label }),
  trial: () => post<{ order: Order }>("/trial"),
  staffDashboard: () => get<StaffDashboard>("/staff/dashboard"),
  reviews: () => get<{ items: Review[] }>("/staff/reviews"),
  approve: (id: number) => post<{ status: string; order_settled: boolean }>(`/staff/reviews/${id}/approve`),
  reject: (id: number, reason: string) => post<{ status: string }>(`/staff/reviews/${id}/reject`, { reason }),
  receipt: async (id: number): Promise<string> => {
    const response = await fetch(`/api/webapp/staff/reviews/${id}/receipt`, { headers: auth() });
    if (!response.ok) throw new Error(`http_${response.status}`);
    return URL.createObjectURL(await response.blob());
  },
};

export interface DeskService {
  id: number;
  label: string | null;
  panel_username: string;
  status: string;
  expire_at: string | null;
  used_bytes: number;
  data_limit: number;
  days_left: number | null;
  gb_left: number | null;
  renew_price: number | null;
}

export interface ResellerDesk {
  figures: { active: number; expiring: number; month_orders: number; month_amount: number };
  expiring: DeskService[];
}
