import { daysLeft, money, size } from "@shared/format";
import type { Translate } from "@shared/i18n";
import type { HistoryItem, Service } from "./api";

/**
 * Client-derived notification feed.
 *
 * The backend has no notifications endpoint, so the feed is built from real data the
 * Mini App already reads: service data/expiry states and wallet history. Read state is
 * local to the device on purpose — it is a UI nicety, not a server contract.
 */
export interface AlertItem {
  id: string;
  kind: "service" | "wallet";
  tone: "red" | "orange" | "green" | "blue";
  icon: "gauge" | "clock" | "coin" | "wallet" | "gift" | "trash";
  title: string;
  body: string;
  at: string | null;
  href: string | null;
}

const READ_KEY = "pauru.alerts.read";

export function readAlertIds(): Set<string> {
  try {
    const raw = localStorage.getItem(READ_KEY);
    const list: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(list) ? list.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

export function markAlertsRead(ids: string[]): void {
  try {
    const merged = [...new Set([...readAlertIds(), ...ids])];
    localStorage.setItem(READ_KEY, JSON.stringify(merged.slice(-300)));
  } catch {
    // storage can be unavailable inside some Telegram clients; read state is optional
  }
}

const WALLET_KINDS: Record<string, { icon: AlertItem["icon"]; tone: AlertItem["tone"]; body: string }> = {
  topup: { icon: "coin", tone: "green", body: "alert_topup_body" },
  reward: { icon: "gift", tone: "green", body: "alert_reward_body" },
  commission: { icon: "gift", tone: "green", body: "alert_reward_body" },
  refund: { icon: "wallet", tone: "green", body: "alert_refund_body" },
  charge: { icon: "wallet", tone: "blue", body: "alert_charge_body" },
};

export function buildAlerts(services: Service[], history: HistoryItem[], t: Translate, locale: string): AlertItem[] {
  const units = t("units").split(",");
  const serviceAlerts: AlertItem[] = [];
  for (const service of services) {
    if (service.status === "deleted") continue;
    const label = service.label || service.username;
    const href = `/services/${service.id}`;
    const empty = service.data_limit > 0 && service.used_bytes >= service.data_limit;
    if (service.status === "limited" || empty) {
      serviceAlerts.push({
        id: `s${service.id}-data`, kind: "service", tone: "red", icon: "gauge", at: service.expire_at, href,
        title: t("alert_data_title"), body: t("alert_data_body", { label }),
      });
    } else if (service.status === "active" && service.data_limit > 0) {
      const left = service.data_limit - service.used_bytes;
      if (left / service.data_limit < 0.15) {
        serviceAlerts.push({
          id: `s${service.id}-low`, kind: "service", tone: "orange", icon: "gauge", at: service.expire_at, href,
          title: t("alert_low_data_title"), body: t("alert_low_data_body", { label, value: size(left, locale, units) }),
        });
      }
    }
    const days = daysLeft(service.expire_at);
    if (days !== null && days <= 0) {
      serviceAlerts.push({
        id: `s${service.id}-expired`, kind: "service", tone: "red", icon: "clock", at: service.expire_at, href,
        title: t("alert_expired_title"), body: t("alert_expired_body", { label }),
      });
    } else if (days !== null && days <= 3) {
      serviceAlerts.push({
        id: `s${service.id}-expiring`, kind: "service", tone: "orange", icon: "clock", at: service.expire_at, href,
        title: t("alert_expiring_title"), body: t("alert_expiring_body", { label, days: money(days, locale) }),
      });
    }
  }
  const walletAlerts = history.slice(0, 40).map<AlertItem>((item) => {
    const kind = item.kind.split(":").pop() ?? item.kind;
    const meta = WALLET_KINDS[kind];
    const amount = `${money(Math.abs(item.amount), locale)} ${t("currency")}`;
    return {
      id: `w${item.id}`, kind: "wallet",
      tone: meta?.tone ?? "blue", icon: meta?.icon ?? "wallet",
      at: item.created_at, href: "/wallet",
      title: t(meta ? `tx_${kind}` : "alert_tx_title"),
      body: t(meta?.body ?? "alert_tx_body", { amount }),
    };
  });
  return [...serviceAlerts, ...walletAlerts];
}
