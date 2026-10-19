import QRCode from "qrcode";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { date, daysLeft, money, size } from "@shared/format";
import { api, type Service } from "../api";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Icon } from "../icons";
import { Button, Chevron, Empty, Loading, Panel, ReadFailure, Ring, Row, Screen } from "../ui";
import { timeRatio, usageRatio } from "./Home";
import { useResource } from "../useResource";
import { AutoRenew } from "../autoRenew/AutoRenew";
import { UsageChart } from "../usage/UsageChart";
import { createActionGate, runServiceAction } from "../service-read";

/** Badge tone for a service status: green while healthy, saffron while settling, red once it is gone. */
function statusTone(status: string) {
  if (status === "active") return "green";
  if (status === "on_hold" || status === "pending") return "orange";
  if (status === "limited" || status === "expired") return "red";
  return "gray";
}

export function Services() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const { data, error, retry } = useResource(api.services);
  const items = data?.items ?? null;
  if (error) return <Screen title={t("my_services")}><ReadFailure error={error} retry={retry} /></Screen>;
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
      <ul className="flex flex-col">
        {items.map((service) => {
          const left = daysLeft(service.expire_at);
          return (
            <li key={service.id} className="app-card">
              <Row onClick={() => navigate(`/services/${service.id}`)}>
                <Ring size={54} outer={usageRatio(service)} inner={timeRatio(service)} />
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 font-bold">
                    <span className="truncate">{service.label || service.username}</span>
                    <span className="app-badge" data-tone={statusTone(service.status)}>
                      {t(`status_${service.status}`)}
                    </span>
                  </p>
                  <p className="num mt-0.5 text-sm text-[var(--hint)]">
                    {service.data_limit
                      ? t("data_left", { value: size(service.data_limit - service.used_bytes, locale, units) })
                      : t("unlimited_data")}
                    {" · "}
                    {left === null ? t("never_expires") : t("days_left", { days: money(left, locale) })}
                  </p>
                </div>
                <Chevron />
              </Row>
            </li>
          );
        })}
      </ul>
      <Button onClick={() => navigate("/buy")}>
        <span className="inline-flex items-center gap-2">
          <Icon name="bag" />
          {t("buy_new_service")}
        </span>
      </Button>
    </Screen>
  );
}

/** A QR failure never hides the copy/download fallback or escapes as an unhandled promise. */
function ServiceQr({ value, serviceId }: { value?: string; serviceId?: number }) {
  const { t } = useApp();
  const load = useCallback(async () => {
    const text = serviceId ? (await api.links(serviceId)).links[0] : value;
    return text ? await QRCode.toDataURL(text, { margin: 1, width: serviceId ? 420 : 360 }) : null;
  }, [value, serviceId]);
  const { data, error, loading, retry } = useResource(load);
  if (error) return <ReadFailure error={error} retry={retry} />;
  if (loading) return <p role="status" className="mb-3 text-sm text-[var(--hint)]">{t("service_qr_loading")}</p>;
  if (!data) return <p className="mb-3 text-sm text-[var(--hint)]">{t("service_qr_empty")}</p>;
  return <span className="app-qrb"><img src={data} alt={t(serviceId ? "wireguard_qr_alt" : "qr_alt")}
    className={serviceId ? "mx-auto mb-3 w-56 rounded-2xl bg-white p-2" : "w-44"} /></span>;
}

export function ServiceDetail() {
  const { id } = useParams();
  // A new resource must not inherit the old resource's links, QR, errors or action lock.
  return <ServiceDetailBody key={id} serviceId={Number(id)} />;
}

function ServiceDetailBody({ serviceId }: { serviceId: number }) {
  const { t, locale, me, toast } = useApp();
  const navigate = useNavigate();
  const load = useCallback(() => api.service(serviceId, true), [serviceId]);
  const { data, error, loading, retry } = useResource(load);
  const [changed, setService] = useState<Service | null>(null);
  const service = changed ?? data?.service ?? null;
  const [links, setLinks] = useState<string[] | null>(null);
  const [linksError, setLinksError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [qrVersion, setQrVersion] = useState(0);
  const gate = useRef(createActionGate());
  useEffect(() => {
    const current = createActionGate();
    gate.current = current;
    return () => current.close();
  }, []);

  if (error) return <Screen title={t("my_services")}><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !service || !me) return <Loading />;
  const units = t("units").split(",");
  const left = daysLeft(service.expire_at);
  const remaining = service.data_limit ? service.data_limit - service.used_bytes : null;

  const copy = async (text: string) => {
    const current = gate.current;
    try {
      await navigator.clipboard.writeText(text);
      if (!current.active) return;
      haptic("select");
      toast(t("copied"));
    } catch {
      if (current.active) toast(t("copy_failed"), "error");
    }
  };

  const revoke = () => {
    if (gate.current.busy || !window.confirm(t("revoke_confirm"))) return;
    return runServiceAction(gate.current, () => {
      setBusy("revoke");
      return api.revoke(service.id);
    }, result => {
      setService(result.service);
      setLinks(null);
      setLinksError(null);
      setQrVersion(value => value + 1);
      haptic("success");
      toast(t("revoked"));
    }, err => toast(errorText(t, err), "error"), () => setBusy(null));
  };

  const showLinks = () => runServiceAction(gate.current, () => {
    setBusy("links");
    setLinksError(null);
    return api.links(service.id);
  }, result => setLinks(result.links), setLinksError, () => setBusy(null));

  const can = (change: string) => !service.changes || service.changes.includes(change);

  const go = (kind: string) =>
    navigate(`/buy?kind=${kind}&service=${service.id}&panel=${service.panel_id}`);

  const actions: ReactNode[] = [];
  if (!service.is_trial && can("renew")) {
    actions.push(<Button key="renew" onClick={() => go("renew")}>{t("renew")}</Button>);
  }
  if (service.extras?.gb_price != null) {
    actions.push(
      <Button key="gb" tone="quiet" onClick={() => navigate(`/extra/${service.id}/gb`)}>{t("add_data")}</Button>,
    );
  }
  if (service.extras?.day_price != null) {
    actions.push(
      <Button key="days" tone="quiet" onClick={() => navigate(`/extra/${service.id}/days`)}>{t("add_time")}</Button>,
    );
  }
  if (me.extras.upgrade && can("upgrade")) {
    actions.push(<Button key="upgrade" tone="quiet" onClick={() => go("upgrade")}>{t("upgrade")}</Button>);
  }
  const showConfigs = !links && service.delivery !== "wireguard" && service.delivery !== "account";
  const canRevoke = can("revoke");

  return (
    <Screen title={service.label || service.username} subtitle={service.username}>
      <section className="app-card p-5 text-center">
        <Ring size={190} outer={usageRatio(service)} inner={timeRatio(service)}>
          <span className="num text-3xl font-extrabold">
            {remaining === null ? "∞" : size(Math.max(0, remaining), locale, units)}
          </span>
          <span className="mt-0.5 text-xs text-[var(--hint)]">{t("data_left_label")}</span>
          <span className="num mt-1 text-sm font-bold">
            {left === null ? "∞" : t("days_left", { days: money(left, locale) })}
          </span>
        </Ring>
        <p className="mt-3">
          <span className="app-badge" data-tone={statusTone(service.status)}>
            {t(`status_${service.status}`)}
          </span>
        </p>
        <div className="-mx-5 mt-2 border-t border-[var(--line)] px-5 text-start">
          <div className="app-ln lg">
            <small>{t("used")}</small>
            <b className="num">
              {size(service.used_bytes, locale, units)}
              {service.data_limit ? ` / ${size(service.data_limit, locale, units)}` : ""}
            </b>
          </div>
          <div className="app-ln lg border-t border-[var(--line)]">
            <small>{t("expires")}</small>
            <b className="num">{service.expire_at ? date(service.expire_at, locale) : t("never")}</b>
          </div>
          {service.location && (
            <div className="app-ln lg border-t border-[var(--line)]">
              <small>{t("location")}</small>
              <b>{service.location}</b>
            </div>
          )}
        </div>
      </section>

      {actions.length > 0 && (
        <div className={`mb-3.5 grid gap-3 ${actions.length > 1 ? "grid-cols-2" : "grid-cols-1"}`}>
          {actions}
        </div>
      )}

      {service.id === serviceId && <AutoRenew key={`auto-${service.id}`} serviceId={service.id} />}

      {service.subscription_url && (
        <section className="app-card p-5 text-center">
          <p className="mb-3 text-start text-sm font-bold">{t("subscription_link")}</p>
          <ServiceQr key={service.subscription_url} value={service.subscription_url} />
          <button type="button" dir="ltr" className="app-lk"
            onClick={() => copy(service.subscription_url ?? "")}>
            {service.subscription_url}
          </button>
          <p className="mt-2 text-xs text-[var(--hint)]">{t("tap_to_copy")}</p>
          {(showConfigs || canRevoke) && (
            <div className={`mt-4 grid gap-3 ${showConfigs && canRevoke ? "grid-cols-2" : "grid-cols-1"}`}>
              {showConfigs && (
                <Button tone="quiet" disabled={busy !== null} busy={busy === "links"} onClick={showLinks}>{t("show_configs")}</Button>
              )}
              {canRevoke && (
                <Button tone="danger" disabled={busy !== null} busy={busy === "revoke"} onClick={revoke}>{t("new_link")}</Button>
              )}
            </div>
          )}
        </section>
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
          <ServiceQr key={`wg:${service.id}:${qrVersion}`} serviceId={service.id} />
          <p className="mb-3 text-sm text-[var(--hint)]">{t("wireguard_hint")}</p>
          {service.download_url && (
            <Button tone="quiet" onClick={() => (tg ? tg.openLink(service.download_url ?? "") : window.open(service.download_url ?? "", "_blank", "noopener"))}>
              {t("wireguard_download")}
            </Button>
          )}
        </Panel>
      )}

      {Boolean(linksError) && <Panel className="mb-4"><ReadFailure error={linksError} retry={() => { void showLinks(); }} /></Panel>}

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

      <UsageChart key={service.id} serviceId={service.id} />
    </Screen>
  );
}

export function Extra() {
  const { id, unit } = useParams();
  return <ExtraBody key={`${id}:${unit}`} serviceId={Number(id)} unit={unit} />;
}

function ExtraBody({ serviceId, unit }: { serviceId: number; unit?: string }) {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const load = useCallback(() => api.service(serviceId), [serviceId]);
  const { data, error, loading, retry } = useResource(load);
  const service = data?.service;
  if (error) return <Screen title={t(unit === "gb" ? "extra_data" : "extra_time")}><ReadFailure error={error} retry={retry} /></Screen>;
  if (loading || !service) return <Loading />;
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
              onClick={() => navigate(`/checkout?kind=${isGb ? "extra_volume" : "extra_days"}&service=${serviceId}&${isGb ? "gb" : "days"}=${value}`)}
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
