import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { date, daysLeft, money, size } from "@shared/format";
import { api, type Service } from "../api";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Button, Empty, Loading, Panel, Ring, Row, Screen, StatusDot } from "../ui";
import { timeRatio, usageRatio } from "./Home";
import { UsageChart } from "../usage/UsageChart";

export function Services() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [items, setItems] = useState<Service[] | null>(null);
  useEffect(() => {
    api.services().then((r) => setItems(r.items));
  }, []);
  if (!items) return <Loading />;
  const units = t("units").split(",");
  if (!items.length) {
    return (
      <Screen title={t("my_services")}>
        <Empty title={t("no_service_yet")} hint={t("no_service_hint")}
          action={<Button onClick={() => navigate("/buy")}>{t("buy_plan")}</Button>} />
      </Screen>
    );
  }
  return (
    <Screen title={t("my_services")}>
      <ul className="overflow-hidden rounded-3xl bg-[var(--surface)]">
        {items.map((service, index) => {
          const left = daysLeft(service.expire_at);
          return (
            <li key={service.id} className={index ? "border-t border-[var(--line)]" : ""}>
              <Row onClick={() => navigate(`/services/${service.id}`)}>
                <Ring size={46} outer={usageRatio(service)} inner={timeRatio(service)} />
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 truncate font-bold">
                    <StatusDot status={service.status} />
                    {service.label}
                  </p>
                  <p className="num mt-0.5 text-sm text-[var(--hint)]">
                    {service.data_limit
                      ? t("data_left", { value: size(service.data_limit - service.used_bytes, locale, units) })
                      : t("unlimited_data")}
                    {" · "}
                    {left === null ? t("never_expires") : t("days_left", { days: money(left, locale) })}
                  </p>
                </div>
              </Row>
            </li>
          );
        })}
      </ul>
    </Screen>
  );
}

export function ServiceDetail() {
  const { t, locale, me, toast } = useApp();
  const navigate = useNavigate();
  const { id } = useParams();
  const serviceId = Number(id);
  const [service, setService] = useState<Service | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [links, setLinks] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [wgQr, setWgQr] = useState<string | null>(null);

  useEffect(() => {
    api.service(serviceId, true).then((r) => setService(r.service)).catch(() => navigate("/services"));
  }, [serviceId, navigate]);

  useEffect(() => {
    if (service?.subscription_url) {
      QRCode.toDataURL(service.subscription_url, { margin: 1, width: 360 }).then(setQr);
    }
  }, [service?.subscription_url]);

  useEffect(() => {
    // A WireGuard service is its .conf: show it as a QR the WireGuard app can scan.
    if (service?.delivery !== "wireguard") return;
    api.links(service.id).then((r) => {
      if (r.links[0]) QRCode.toDataURL(r.links[0], { margin: 1, width: 420 }).then(setWgQr);
    }).catch(() => setWgQr(null));
  }, [service?.id, service?.delivery]);

  if (!service || !me) return <Loading />;
  const units = t("units").split(",");
  const left = daysLeft(service.expire_at);
  const remaining = service.data_limit ? service.data_limit - service.used_bytes : null;

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      haptic("select");
      toast(t("copied"));
    } catch {
      toast(t("copy_failed"), "error");
    }
  };

  const revoke = async () => {
    if (!window.confirm(t("revoke_confirm"))) return;
    setBusy("revoke");
    try {
      const result = await api.revoke(service.id);
      setService(result.service);
      setLinks(null);
      haptic("success");
      toast(t("revoked"));
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const showLinks = async () => {
    setBusy("links");
    try {
      setLinks((await api.links(service.id)).links);
    } finally {
      setBusy(null);
    }
  };

  const can = (change: string) => !service.changes || service.changes.includes(change);

  const go = (kind: string) =>
    navigate(`/buy?kind=${kind}&service=${service.id}&panel=${service.panel_id}`);

  return (
    <Screen title={service.label}>
      <UsageChart key={service.id} serviceId={service.id} />
      <div className="mb-5 flex flex-col items-center">
        <Ring size={210} outer={usageRatio(service)} inner={timeRatio(service)}>
          <span className="num text-3xl font-extrabold">
            {remaining === null ? "∞" : size(Math.max(0, remaining), locale, units)}
          </span>
          <span className="text-xs text-[var(--hint)]">{t("data_left_label")}</span>
          <span className="num mt-2 text-lg font-bold">
            {left === null ? "∞" : t("days_left", { days: money(left, locale) })}
          </span>
        </Ring>
        <p className="mt-3 flex items-center gap-2 text-sm font-semibold">
          <StatusDot status={service.status} /> {t(`status_${service.status}`)}
        </p>
      </div>

      <Panel className="mb-4">
        <div className="flex justify-between py-1 text-sm">
          <span className="text-[var(--hint)]">{t("used")}</span>
          <span className="num">
            {size(service.used_bytes, locale, units)}
            {service.data_limit ? ` / ${size(service.data_limit, locale, units)}` : ""}
          </span>
        </div>
        <div className="flex justify-between py-1 text-sm">
          <span className="text-[var(--hint)]">{t("expires")}</span>
          <span className="num">{service.expire_at ? date(service.expire_at, locale) : t("never")}</span>
        </div>
        {service.location && (
          <div className="flex justify-between py-1 text-sm">
            <span className="text-[var(--hint)]">{t("location")}</span>
            <span>{service.location}</span>
          </div>
        )}
      </Panel>

      {service.subscription_url && (
        <Panel className="mb-4">
          <p className="mb-2 text-sm font-bold">{t("subscription_link")}</p>
          {qr && <img src={qr} alt={t("qr_alt")} className="mx-auto mb-3 w-48 rounded-2xl bg-white p-2" />}
          <button type="button" dir="ltr" onClick={() => copy(service.subscription_url ?? "")}
            className="w-full break-all rounded-2xl bg-[var(--bg)] p-3 text-start font-mono text-xs">
            {service.subscription_url}
          </button>
          <p className="mt-2 text-center text-xs text-[var(--hint)]">{t("tap_to_copy")}</p>
        </Panel>
      )}

      {service.delivery === "account" && service.account && (
        <Panel className="mb-4">
          <p className="mb-2 text-sm font-bold">{t("account_login")}</p>
          {([
            [t("account_username"), service.account.username],
            [t("account_password"), service.account.password ?? "-"],
          ] as [string, string][]).map(([label, value]) => (
            <button key={label} type="button" onClick={() => copy(value)}
              className="mb-2 flex w-full items-center justify-between gap-3 rounded-2xl bg-[var(--bg)] p-3 text-start">
              <span className="text-sm text-[var(--hint)]">{label}</span>
              <span dir="ltr" className="font-mono text-sm">{value}</span>
            </button>
          ))}
          <p className="text-center text-xs text-[var(--hint)]">{t("tap_to_copy")}</p>
        </Panel>
      )}

      {service.delivery === "wireguard" && (
        <Panel className="mb-4">
          <p className="mb-2 text-sm font-bold">{t("wireguard_title")}</p>
          {wgQr && <img src={wgQr} alt={t("qr_alt")} className="mx-auto mb-3 w-56 rounded-2xl bg-white p-2" />}
          <p className="mb-3 text-sm text-[var(--hint)]">{t("wireguard_hint")}</p>
          {service.download_url && (
            <Button tone="quiet" onClick={() => (tg ? tg.openLink(service.download_url ?? "") : window.open(service.download_url ?? "", "_blank", "noopener"))}>
              {t("wireguard_download")}
            </Button>
          )}
        </Panel>
      )}

      {links && service.delivery !== "wireguard" && (
        <Panel className="mb-4">
          {links.length === 0 && <p className="text-sm text-[var(--hint)]">{t("no_links")}</p>}
          <ul className="flex flex-col gap-2">
            {links.map((link) => (
              <li key={link}>
                <button type="button" dir="ltr" onClick={() => copy(link)}
                  className="w-full truncate rounded-xl bg-[var(--bg)] px-3 py-2 text-start font-mono text-xs">
                  {link}
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <div className="grid grid-cols-2 gap-3">
        {!service.is_trial && can("renew") && <Button onClick={() => go("renew")}>{t("renew")}</Button>}
        {me.extras.upgrade && can("upgrade") && <Button tone="quiet" onClick={() => go("upgrade")}>{t("upgrade")}</Button>}
        {service.extras?.gb_price != null && (
          <Button tone="quiet" onClick={() => navigate(`/extra/${service.id}/gb`)}>{t("extra_data")}</Button>
        )}
        {service.extras?.day_price != null && (
          <Button tone="quiet" onClick={() => navigate(`/extra/${service.id}/days`)}>{t("extra_time")}</Button>
        )}
        {!links && service.delivery !== "wireguard" && service.delivery !== "account" && (
          <Button tone="quiet" busy={busy === "links"} onClick={showLinks}>{t("show_configs")}</Button>
        )}
        {can("revoke") && <Button tone="danger" busy={busy === "revoke"} onClick={revoke}>{t("new_link")}</Button>}
      </div>
    </Screen>
  );
}

export function Extra() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const { id, unit } = useParams();
  const [service, setService] = useState<Service | null>(null);
  useEffect(() => {
    api.service(Number(id)).then((r) => setService(r.service)).catch(() => navigate("/services"));
  }, [id, navigate]);
  if (!service) return <Loading />;
  const isGb = unit === "gb";
  const options = isGb ? [5, 10, 20, 50, 100] : [7, 15, 30, 60, 90];
  const price = isGb ? service.extras?.gb_price : service.extras?.day_price;
  if (price == null) return <Empty title={t("extras_unavailable")} />;
  return (
    <Screen title={t(isGb ? "extra_data" : "extra_time")}>
      <ul className="grid grid-cols-2 gap-3">
        {options.map((value) => (
          <li key={value}>
            <button type="button"
              onClick={() => navigate(`/checkout?kind=${isGb ? "extra_volume" : "extra_days"}&service=${id}&${isGb ? "gb" : "days"}=${value}`)}
              className="w-full rounded-3xl bg-[var(--surface)] p-4 text-center">
              <p className="num text-2xl font-extrabold">{money(value, locale)}</p>
              <p className="text-xs text-[var(--hint)]">{t(isGb ? "gb" : "days")}</p>
              <p className="num mt-2 text-sm font-semibold">{money(value * price, locale)} {t("currency")}</p>
            </button>
          </li>
        ))}
      </ul>
    </Screen>
  );
}
