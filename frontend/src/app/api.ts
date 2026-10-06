import { request } from "@shared/http";
import { initData } from "./telegram";

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
  extras: { enabled: boolean; gb_price: number; day_price: number; upgrade: boolean };
  payment_methods: string[];
  topup_bounds: [number, number];
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
  extras: { gb_price: number; day_price: number } | null;
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

export interface HistoryItem {
  id: number;
  amount: number;
  balance_after: number;
  kind: string;
  created_at: string;
}

const auth = () => ({ "X-Telegram-Init-Data": initData() });
const get = <T>(path: string) => request<T>(`/api/webapp${path}`, { headers: auth() });
const post = <T>(path: string, body: unknown = {}) =>
  request<T>(`/api/webapp${path}`, { method: "POST", body, headers: auth() });

export const api = {
  me: () => get<Me>("/me"),
  setLocale: (locale: string) => post<{ locale: string }>("/me/locale", { locale }),
  catalog: () => get<Catalog>("/catalog"),
  createOrder: (body: {
    kind: string;
    plan_id?: number;
    service_id?: number;
    extra_gb?: number;
    extra_days?: number;
    code?: string;
    custom_gb?: number;
    custom_days?: number;
  }) => post<{ order: Order; balance: number; due: number }>("/orders", body),
  order: (id: number) => get<{ order: Order }>(`/orders/${id}`),
  payOrder: (id: number, method: string) =>
    post<{ order: Order; payment?: PaymentStart }>(`/orders/${id}/pay`, { method }),
  cancelOrder: (id: number) => post<{ ok: boolean }>(`/orders/${id}/cancel`),
  topup: (amount: number, method: string) => post<{ payment: PaymentStart }>("/wallet/topup", { amount, method }),
  payment: (id: number) => get<{ id: number; status: string; amount: number; fulfilled: boolean }>(`/payments/${id}`),
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
  service: (id: number, refresh = false) => get<{ service: Service }>(`/services/${id}${refresh ? "?refresh=true" : ""}`),
  links: (id: number) => get<{ links: string[] }>(`/services/${id}/links`),
  revoke: (id: number) => post<{ service: Service }>(`/services/${id}/revoke`),
  rename: (id: number, label: string) => post<{ service: Service }>(`/services/${id}/rename`, { label }),
  trial: () => post<{ order: Order }>("/trial"),
};
