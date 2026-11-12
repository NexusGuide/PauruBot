import type { Announcement, AnnouncementPage } from "@shared/announcements";
import { ticketListPath, type TicketFilters } from "./ticket-pages";
import { request } from "@shared/http";
import type { Body as AutoRenewBody } from "./autoRenew/model";
import { tonpay } from "./tonpay/api";
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
  shop: {
    title: string;
    bot_username: string | null;
    support: string | null;
    languages: string[];
    /** The shop's own look (REF-08); missing from older servers. */
    brand?: { accent: string | null; logo: boolean; notice: string | null };
  };
  trial: { available: boolean; reason: string | null; days: number; gb: number };
  referral: {
    enabled: boolean;
    percent: number;
    count: number;
    /** False when invites earn a config only, no cash share; missing from older servers. */
    cash?: boolean;
    /** Every N counted invites earn a free config; null when that reward is off. */
    config?: ReferralConfig | null;
  };
  reseller: { active: boolean; discount_percent: number };
  panel_accounts: boolean;
  extras: { enabled: boolean; gb_price: number; day_price: number; upgrade: boolean };
  payment_methods: string[];
  /** Gateway names the shop renamed in its bot texts; the rest keep the app's own names. */
  payment_labels?: Record<string, string>;
  topup_bounds: [number, number];
  tonpay_custom_available?: boolean;
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
  service_id?: number | null;
  service_context?: { id: number; status: string; used_bytes: number; data_limit: number; expire_at: string | null } | null;
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
  attachment_expired?: boolean;
  created_at: string | null;
  /** The customer's rating of a support reply (F-22). */
  reaction?: "up" | "down" | null;
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
  /** Runs this dedicated bot on its own panel (OPB-4); missing from older servers. */
  own?: boolean;
}

/** A channel the dedicated bot's buyers must join before using it (GRW-07). */
export interface OwnChannel {
  id: number;
  title: string;
  url: string;
  is_active: boolean;
}

/** The reseller's own admin account its dedicated bot sells from. */
export interface OwnAccount {
  username: string;
  status: string;
  data_limit: number;
  used_bytes: number;
  users_count: number;
  max_users: number | null;
  expire_at: string | null;
}

export interface OwnExtra { enabled: boolean; price: number; min: number; max: number; bounds?: [number, number] }
export interface OwnFeatures {
  trial: { enabled: boolean; gb: number; days: number; daily_quota: number };
  renew: { enabled: boolean; carry: boolean };
  extra_gb: OwnExtra;
  extra_days: OwnExtra;
}
export interface OwnCard { number: string; holder: string }

export interface OwnShop {
  title: string;
  bot_username: string | null;
  main_bot: string | null;
  account: OwnAccount | null;
  /** "" while selling, else inactive | expired | traffic_full | users_full. */
  paused: string;
  hold: boolean;
  /** Until when the main shop lets this bot sell (ISO); null: no end. Missing on older servers. */
  expires_at?: string | null;
  figures: { sales_today: number; customers: number; active_services: number; pending_receipts: number };
  plans: { active: number; total: number };
  card: OwnCard | null;
  features: OwnFeatures;
}

export interface OwnPlan {
  id: number;
  title: string;
  data_gb: number;
  days: number;
  price: number;
  group_ids: string[];
  is_active: boolean;
  renewable: boolean;
  sold: number;
}
export type OwnPlanInput = Omit<OwnPlan, "id" | "sold">;
export interface OwnPlans { items: OwnPlan[]; groups: { id: string; title: string }[]; groups_ok: boolean }

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
  /** Missing from older servers. */
  category_id?: number | null;
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
  /** The categories the plans sit in (e.g. Tunnel / Direct); missing from older servers. */
  categories?: { id: number; title: string }[];
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
  /** 0 when extra days are not sold for this account. */
  day_price: number;
  login_url: string;
  /** How a renewal is sold: the plan itself, ready-made packages and/or a custom size. */
  renew: AccountRenewal | null;
  /** Auto-renewal from the wallet; a choice is {} (the plan itself) or {package: i}. */
  auto_renew: { on: boolean; choice: { package?: number }; choices: { package?: number }[]; stopped?: string | null };
}

export interface AccountRenewal {
  /** False when the plan is not renewable (or no longer sold). */
  allowed: boolean;
  plain: boolean;
  packages: { days: number; gb: number; price: number }[];
  custom: boolean;
  /** Whether the plan has a traffic quota, so a custom renewal also asks for GB. */
  quota: boolean;
  day_price: number;
  gb_price: number;
}

/** How much extra traffic or time one order may buy: [least, most]. */
export interface AccountLimits {
  gb: [number, number];
  days: [number, number];
}

export interface AccountList {
  available: boolean;
  items: PanelAccount[];
}

export interface Service {
  national_mode?: boolean;
  id: number;
  label: string;
  username: string;
  status: string;
  is_trial: boolean;
  data_limit: number;
  used_bytes: number;
  expire_at: string | null;
  /** When the data ran out (SEC-F15), when the server reports it. */
  limited_at?: string | null;
  subscription_url: string | null;
  location: string | null;
  plan_id: number | null;
  panel_id: number;
  extras: {
    gb_price: number | null;
    day_price: number | null;
    /** Amounts this shop offers; missing from older servers. */
    gb_choices?: number[];
    day_choices?: number[];
  } | null;
  delivery?: "subscription" | "wireguard" | "account" | "stock";
  account?: { username: string; password: string | null };
  download_url?: string | null;
  changes?: string[];
  /** What the customer may do to the service itself (F-03): "cancel", "move". */
  actions?: string[];
  /** The branded link in each client format the panel serves (F-32). */
  sub_formats?: { format: string; url: string }[];
  /** Last online, and the app that last fetched the subscription (F-11). */
  access?: ServiceAccess;
}

export interface ServiceAccess {
  online_at: string | null;
  client: string | null;
  client_at: string | null;
  /** Whether the panel can list the addresses connected right now. */
  devices: boolean;
}

/** One of the customer's own discount codes, and whether it fits the order at hand (F-33). */
export interface MyCode {
  code: string;
  kind: "percent" | "fixed";
  value: number;
  max_discount: number | null;
  min_amount: number;
  expires_at: string | null;
  first_purchase_only: boolean;
  sticky: boolean;
  fits: boolean | null;
  reason: string | null;
  saving: number | null;
}

export interface MyDiscounts {
  standing_percent: number;
  reseller: boolean;
  tier: { title: string; percent: number } | null;
  next_tier: { title: string; percent: number; remaining: number } | null;
  codes: MyCode[];
}

export interface ReferralConfig {
  every: number;
  counted: number;
  progress: number;
  available: number;
  counts: "purchase" | "join";
  reward: { plan_title: string | null; gb: number; days: number } | null;
}

export interface ServiceDevices { count: number; limit: number; addresses: string[] }

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
    national?: boolean;
    stock_plan_id?: number;
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
    days?: number;
    renew?: { days: number; gb: number; package?: number };
    /** An admin account order whose plan takes discount codes. */
    discounts?: boolean;
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
  uncertain?: boolean;
  /** The shop's own help for this payment method (Foxima parity), shown before paying. */
  help?: string;
}

export interface PendingPayment {
  id: number;
  provider: string;
  amount: number;
  status: string;
  order_id: number | null;
  expires_at: string | null;
  action: "resume" | "status" | "review";
}
export interface PendingPaymentList { items: PendingPayment[]; next_before: number | null }

export interface CryptoInstructions {
  network: string;
  label: string;
  address: string;
  amount: string;
  memo: string | null;
  expires_at: string | null;
}

/** What the shop still needs before the app answers anything else: phone, then terms (SEC-F11). */
export interface GateInfo {
  requirement: "phone_required" | "terms_required" | null;
  bot_username: string | null;
  terms?: { version: number; text: string };
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

const send = <T>(method: "PUT" | "DELETE", path: string, body?: unknown) =>
  request<T>(`/api/webapp${path}`, { method, body, headers: auth() });

export const api = {
  ownShop: () => get<OwnShop>("/own"),
  ownChannels: () => get<{ channels: OwnChannel[]; limit: number }>("/own/channels"),
  ownAddChannel: (channel: string) => post<{ channel: OwnChannel }>("/own/channels", { channel }),
  ownSetChannel: (id: number, is_active: boolean) => send<{ channel: OwnChannel }>("PUT", `/own/channels/${id}`, { is_active }),
  ownDeleteChannel: (id: number) => send<{ deleted: boolean }>("DELETE", `/own/channels/${id}`),
  ownPlans: () => get<OwnPlans>("/own/plans"),
  ownCreatePlan: (body: OwnPlanInput) => post<{ plan: OwnPlan }>("/own/plans", body),
  ownUpdatePlan: (id: number, body: OwnPlanInput) => send<{ plan: OwnPlan }>("PUT", `/own/plans/${id}`, body),
  ownDeletePlan: (id: number) => send<{ deleted: boolean }>("DELETE", `/own/plans/${id}`),
  ownPayment: () => get<{ card: OwnCard | null; order_expire_minutes: number }>("/own/payment"),
  ownSavePayment: (body: { number: string; holder: string; order_expire_minutes: number }) =>
    send<{ card: OwnCard; order_expire_minutes: number }>("PUT", "/own/payment", body),
  ownSaveFeatures: (body: OwnFeatures) => send<{ features: OwnFeatures }>("PUT", "/own/features", body),
  announcements: (before?: number, unread = false) => get<AnnouncementPage>(`/announcements?unread=${unread}${before ? `&before=${before}` : ""}`),
  announcementState: (id: number, action: "seen" | "read") => post<{ item: Announcement }>(`/announcements/${id}/state`, { action }),
  cardVerification: () => get<unknown>("/cards/verification"),
  submitCardVerification: (body: { intent_key: string; number: string; image: string }) =>
    request<unknown>("/api/webapp/cards/verification", {
      method: "POST", body, headers: auth(), signal: AbortSignal.timeout(25_000),
    }),
  transferPolicy: () => get<TransferPolicy>("/wallet/transfer-policy"),
  transferQuote: (recipient: string, amount: number) => post<TransferQuote>("/wallet/transfers/quote", { recipient, amount }),
  transferConfirm: (token: string) => post<{ journal_id: number; amount: number; replayed: boolean; balance: number }>("/wallet/transfers/confirm", { token }),
  me: () => get<Me>("/me"),
  join: () => get<{ channels: JoinChannel[] }>("/join"),
  checkJoin: () => post<{ ok: boolean; channels: JoinChannel[] }>("/join/check"),
  gate: (purpose?: "purchase") => get<GateInfo>(purpose ? `/gate?purpose=${purpose}` : "/gate"),
  acceptTerms: (version: number) => post<GateInfo>("/gate/terms", { version }),
  /** The signed contact from requestContact, checked and saved by the server (F-12). */
  sharePhone: (response: string) => post<GateInfo>("/gate/phone", { response }),
  setAlerts: (muted: boolean) => post<{ alerts_muted: boolean }>("/me/alerts", { alerts_muted: muted }),
  setMarketing: (optOut: boolean) =>
    post<MarketingPreference>("/me/marketing", { marketing_opt_out: optOut }),
  setLocale: (locale: string) => post<{ locale: string }>("/me/locale", { locale }),
  help: (signal?: AbortSignal) => get<CustomerHelp>("/help", signal),
  tickets: (before?: number, signal?: AbortSignal, filters?: TicketFilters) => get<TicketList>(ticketListPath(before, filters), signal),
  ticket: (id: number, before?: number, signal?: AbortSignal) => get<TicketThreadData>(`/tickets/${id}${before ? `?before=${before}` : ""}`, signal),
  newTicket: (body: { subject: string; text: string; department_id: number | null; service_id?: number | null; request_key?: string }) => post<{ ticket: Ticket }>("/tickets", body),
  replyTicket: (id: number, text: string, request_key?: string) => post<{ ticket: Ticket }>(`/tickets/${id}/reply`, { text, request_key }),
  reactTicket: (id: number, messageId: number, reaction: "up" | "down" | null) =>
    request<{ id: number; reaction: "up" | "down" | null }>(`/api/webapp/tickets/${id}/messages/${messageId}/reaction`, {
      method: "PUT", body: { reaction }, headers: auth(),
    }),
  closeTicket: (id: number) => post<{ ticket: Ticket }>(`/tickets/${id}/close`),
  catalog: () => get<Catalog>("/catalog"),
  accountPlans: (signal?: AbortSignal) => get<AccountPlans>("/account-plans", signal),
  accounts: (signal?: AbortSignal) => get<AccountList>("/accounts", signal),
  account: (id: number) => get<{ account: PanelAccount; limits: AccountLimits }>(`/accounts/${id}`),
  accountLogin: (id: number) => get<{ url: string; username: string; password: string }>(`/accounts/${id}/login`),
  refreshAccount: (id: number) => post<{ account: PanelAccount }>(`/accounts/${id}/refresh`),
  accountAutoRenew: (id: number, enabled: boolean, pkg?: number) =>
    post<{ account: PanelAccount }>(`/accounts/${id}/auto-renew`, { enabled, package: pkg }),
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
    renew_package?: number;
    location_id?: number;
  }) => post<{ order: Order; balance: number; due: number }>("/orders", body),
  resellerDesk: (days: number) => get<ResellerDesk>(`/reseller/desk?days=${days}`),
  renewMany: (ids: number[]) =>
    post<{ renewed: number[]; failed: { id: number; error: string }[]; balance: number }>("/reseller/renew", { service_ids: ids }),
  order: (id: number) => get<{ order: Order }>(`/orders/${id}`),
  renewalPreview: (id: number, signal?: AbortSignal) => get<unknown>(`/orders/${id}/renewal-preview`, signal),
  payOrder: (id: number, method: string) =>
    post<{ order: Order; payment?: PaymentStart }>(`/orders/${id}/pay`, { method }),
  cancelOrder: (id: number) => post<{ ok: boolean }>(`/orders/${id}/cancel`),
  topup: async (amount: number, method: string, intent_key?: string): Promise<{ payment: PaymentStart }> => {
    if (method === "tonpay_custom") {
      if (!intent_key) throw Error("payment_intent_invalid");
      const result = await tonpay.create(amount, intent_key);
      return { payment: { payment_id: result.payment_id, provider: method, amount: result.request_amount, uncertain: result.uncertain } };
    }
    return post<{ payment: PaymentStart }>("/wallet/topup", { amount, method, intent_key });
  },
  payment: (id: number) => get<{ id: number; status: string; amount: number; fulfilled: boolean; provider: string; order_id: number | null }>(`/payments/${id}`),
  pendingPayments: (before?: number) => get<PendingPaymentList>(`/payments${before ? `?before=${before}` : ""}`),
  resumePayment: (id: number) => get<{ payment: PaymentStart }>(`/payments/${id}/resume`),
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
  servicePage: (q: string, status: ServiceFilter, before: number | undefined, signal: AbortSignal, group: number | null = null) =>
    request<unknown>(`/api/webapp${servicePageUrl(q, status, before, group)}`, { headers: auth(), signal }),
  serviceGroups: (signal?: AbortSignal) => request<unknown>("/api/webapp/services/groups", { headers: auth(), signal }),
  service: (id: number, refresh = false) => get<{ service: Service }>(`/services/${id}${refresh ? "?refresh=true" : ""}`),
  usage: (id: number, days: 7 | 30, signal?: AbortSignal) =>
    request<UsageHistory>(`/api/webapp/services/${id}/usage?days=${days}`, { headers: auth(), signal }),
  autoRenew: (id: number) => get<unknown>(`/services/${id}/auto-renew`),
  setAutoRenew: (id: number, body: AutoRenewBody) => post<unknown>(`/services/${id}/auto-renew`, body),
  links: (id: number) => get<{ links: string[] }>(`/services/${id}/links`),
  revoke: (id: number) => post<{ service: Service }>(`/services/${id}/revoke`),
  myDiscounts: (orderId?: number) => get<MyDiscounts>(`/discounts${orderId ? `?order_id=${orderId}` : ""}`),
  claimReferral: () => post<{ order_id: number; cash: boolean; config: ReferralConfig | null }>("/referral/claim"),
  serviceDevices: (id: number) => get<ServiceDevices>(`/services/${id}/devices`),
  cancelQuote: (id: number) => get<{ refund: number }>(`/services/${id}/cancel`),
  cancelService: (id: number) => post<{ refund: number }>(`/services/${id}/cancel`),
  moveOptions: (id: number) =>
    get<{ price: number; targets: { location_id: number; title: string; flag: string | null }[] }>(`/services/${id}/move`),
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
