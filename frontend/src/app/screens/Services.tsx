import QRCode from "qrcode";
import { StockInventory } from "./StockInventory";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate, useNavigationType, useParams, useSearchParams } from "react-router-dom";
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
import { ServiceAccess } from "./ServiceAccess";
import { createActionGate, runServiceAction } from "../service-read";
import { normalizedServiceLabel } from "../service-label";
import {
  SERVICE_STATUSES, readServiceGroups, readServicePage, serviceFilter, serviceGroup, servicesReturnTo,
  type ServiceFilter, type ServiceGroup,
} from "../service-pages";

/** Badge tone for a service status: green while healthy, saffron while settling, red once it is gone. */
function statusTone(status: string) {
  if (status === "active") return "green";
  if (status === "on_hold" || status === "pending") return "orange";
  if (status === "limited" || status === "expired") return "red";
  return "gray";
}

export function Services() {
  const { t } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const editor = useId();
  const urlQuery = (params.get("q") ?? "").slice(0, 120);
  // BrowserRouter transitions are deferred. Keep the controlled input synchronous,
  // so a second keypress cannot be reset to the previous URL's value.
  const [q, setQuery] = useState(urlQuery);
  useEffect(() => {
    if (navigationType !== "REPLACE" || location.state?.serviceFilterEditor !== editor) setQuery(urlQuery);
  }, [urlQuery, location.key, location.state, navigationType, editor]);
  const status = serviceFilter(params.get("status"));
  const group = serviceGroup(params.get("group"));
  const query = q.trim();
  const returnTo = servicesReturnTo(`/services?${params}`);
  // Services by location (F-19): offered only when they live in two or more places.
  const [groups, setGroups] = useState<ServiceGroup[]>([]);
  useEffect(() => {
    const current = new AbortController();
    void api.serviceGroups(current.signal).then(value => { if (!current.signal.aborted) setGroups(readServiceGroups(value)); })
      .catch(() => { /* the list still works without its groups */ });
    return () => current.abort();
  }, []);
  const change = (nextQuery: string, nextStatus: ServiceFilter, nextGroup: number | null = group) => {
    setQuery(nextQuery);
    const next = new URLSearchParams();
    if (nextQuery) next.set("q", nextQuery);
    if (nextStatus) next.set("status", nextStatus);
    if (nextGroup !== null) next.set("group", String(nextGroup));
    setParams(next, { replace: true, state: { serviceFilterEditor: editor } });
  };
  return <Screen title={t("my_services")}>
    <div className="mb-4 grid gap-3">
      <label className="text-sm font-semibold" htmlFor="service-search">{t("services_search")}</label>
      <input id="service-search" type="search" maxLength={120} value={q}
        onChange={event => change(event.target.value, status)} placeholder={t("services_search_hint")}
        className="min-h-12 w-full rounded-2xl bg-[var(--surface)] px-4 text-[var(--text)] outline-none ring-1 ring-[var(--line)] focus:ring-[var(--accent)]" />
      <label className="text-sm font-semibold" htmlFor="service-status">{t("services_status")}</label>
      <select id="service-status" value={status} onChange={event => change(q, serviceFilter(event.target.value))}
        className="min-h-12 w-full rounded-2xl bg-[var(--surface)] px-4 text-[var(--text)] ring-1 ring-[var(--line)] focus:ring-[var(--accent)]">
        <option value="">{t("all")}</option>
        {SERVICE_STATUSES.map(value => <option key={value} value={value}>{t(`status_${value}`)}</option>)}
      </select>
      {(query || status || group !== null) && <Button tone="quiet" onClick={() => change("", "", null)}>{t("services_clear")}</Button>}
    </div>
    {groups.length > 0 && <div className="app-chips" role="group" aria-label={t("services_group")}>
      <button type="button" className="app-chip" aria-pressed={group === null} onClick={() => change(q, status, null)}>
        {t("services_group_all")}
      </button>
      {groups.map(g => <button key={g.id} type="button" className="app-chip" aria-pressed={group === g.id}
        onClick={() => change(q, status, g.id)}>
        {g.flag && <span aria-hidden="true">{g.flag}</span>}
        <span>{g.title || t("services_group_other")}</span>
        <span className="opacity-70">{g.count}</span>
      </button>)}
    </div>}
    <StockInventory />
    <ServiceResults key={JSON.stringify([query, status, group])} q={query} status={status} group={group} returnTo={returnTo} />
    <Button onClick={() => navigate("/buy")}>
      <span className="inline-flex items-center gap-2"><Icon name="bag" />{t("buy_new_service")}</span>
    </Button>
  </Screen>;
}

function ServiceResults({ q, status, group, returnTo }: { q: string; status: ServiceFilter; group: number | null; returnTo: string }) {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [items, setItems] = useState<Service[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const active = useRef(true), busy = useRef(true), sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    active.current = true;
    busy.current = true;
    setLoading(true);
    setError(null);
    const id = ++sequence.current;
    const current = new AbortController();
    controller.current = current;
    const valid = () => active.current && id === sequence.current && !current.signal.aborted;
    const timer = window.setTimeout(() => {
      void api.servicePage(q, status, undefined, current.signal, group).then(value => readServicePage(value))
        .then(page => { if (valid()) { setItems(page.items); setNextBefore(page.next_before); } })
        .catch(reason => { if (valid()) setError(reason); })
        .finally(() => { if (valid()) { busy.current = false; setLoading(false); } });
    }, q ? 250 : 0);
    return () => { active.current = false; sequence.current++; window.clearTimeout(timer); current.abort(); controller.current?.abort(); };
  }, [q, status, group, attempt]);

  const more = async () => {
    if (!active.current || busy.current || nextBefore === null) return;
    busy.current = true;
    setLoading(true);
    setError(null);
    const id = sequence.current;
    const before = nextBefore;
    const current = new AbortController();
    controller.current = current;
    const valid = () => active.current && id === sequence.current && !current.signal.aborted;
    try {
      const page = readServicePage(await api.servicePage(q, status, before, current.signal, group), before);
      if (valid()) { setItems(previous => [...(previous ?? []), ...page.items]); setNextBefore(page.next_before); }
    } catch (reason) {
      if (valid()) setError(reason);
    } finally {
      if (valid()) { busy.current = false; setLoading(false); }
    }
  };
  const units = t("units").split(",");
  return <div aria-busy={loading}>
    {Boolean(error) && <ReadFailure error={error} retry={() => { if (items === null) setAttempt(value => value + 1); else void more(); }} />}
    {items === null && loading && <Loading />}
    {items?.length === 0 && <Empty title={t(q || status || group !== null ? "services_no_matches" : "no_service_yet")}
      hint={t(q || status || group !== null ? "services_no_matches_hint" : "no_service_hint")} />}
    {items && <>
      <ul className="flex flex-col">
        {items.map((service) => {
          const left = daysLeft(service.expire_at);
          return (
            <li key={service.id} className="app-card">
              <Row onClick={() => navigate(`/services/${service.id}`, { state: { servicesReturnTo: returnTo } })}>
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
      {nextBefore !== null && !error && <Button tone="quiet" busy={loading} onClick={() => { void more(); }}>{t("services_more")}</Button>}
    </>}
  </div>;
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
  const location = useLocation();
  const backTo = servicesReturnTo(location.state?.servicesReturnTo);
  // A new resource must not inherit the old resource's links, QR, errors or action lock.
  return <ServiceDetailBody key={id} serviceId={Number(id)} backTo={backTo} />;
}

function ServiceDetailBody({ serviceId, backTo }: { serviceId: number; backTo: string }) {
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
  const [renaming, setRenaming] = useState(false);
  const [label, setLabel] = useState("");
  const [renameError, setRenameError] = useState<unknown>(null);
  const [moving, setMoving] = useState<{ price: number; targets: { location_id: number; title: string; flag: string | null }[] } | null>(null);
  const [cancelling, setCancelling] = useState<number | null>(null);
  const gate = useRef(createActionGate());
  useEffect(() => {
    const current = createActionGate();
    gate.current = current;
    return () => current.close();
  }, []);

  if (error) return <Screen title={t("my_services")} backTo={backTo}><ReadFailure error={error} retry={retry} /></Screen>;
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

  const rename = () => {
    const normalized = normalizedServiceLabel(label);
    if (!normalized || normalized === service.label) return;
    return runServiceAction(gate.current, () => {
      setBusy("rename");
      setRenameError(null);
      return api.rename(service.id, normalized);
    }, result => {
      // Only display the label confirmed by the existing owned-resource endpoint.
      setService(result.service);
      setRenaming(false);
      haptic("success");
      toast(t("service_renamed"));
    }, setRenameError, () => setBusy(null));
  };

  const can = (change: string) => !service.changes || service.changes.includes(change);
  const allowed = (action: string) => Boolean(service.actions?.includes(action));

  // Changing the location (F-03): the destinations the shop offers, then the usual checkout.
  const openMove = () => runServiceAction(gate.current, () => {
    setBusy("move");
    return api.moveOptions(service.id);
  }, result => {
    if (result.targets.length === 0) toast(t("move_no_targets"), "error");
    else setMoving(result);
  }, err => toast(errorText(t, err), "error"), () => setBusy(null));

  // Deleting the service (F-03): what comes back first, then an explicit confirmation.
  const openCancel = () => runServiceAction(gate.current, () => {
    setBusy("cancel");
    return api.cancelQuote(service.id);
  }, result => setCancelling(result.refund), err => toast(errorText(t, err), "error"), () => setBusy(null));

  const confirmCancel = () => runServiceAction(gate.current, () => {
    setBusy("cancel_go");
    return api.cancelService(service.id);
  }, result => {
    haptic("success");
    toast(result.refund ? t("cancel_done_refund", { amount: money(result.refund, locale) }) : t("cancel_done"));
    navigate(backTo, { replace: true });
  }, err => toast(errorText(t, err), "error"), () => { setBusy(null); setCancelling(null); });

  const go = (kind: string) =>
    navigate(
      `/buy?kind=${kind}&service=${service.id}&panel=${service.panel_id}` +
        (kind === "upgrade" && service.plan_id ? `&current=${service.plan_id}` : ""),
    );

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
    <Screen title={service.label || service.username} subtitle={service.username} backTo={backTo}>
      {service.national_mode && <Panel className="mb-4"><p>{locale === "fa" ? "نت ملی فعال است. اطلاعات زیر مربوط به اشتراک اصلی است؛ تمدید کانفیگ جایگزین می‌دهد. فایل‌های جایگزین در «کانفیگ‌های آمادهٔ من» هستند." : "National-network mode is on. These figures describe the original service. Renewal delivers a replacement; find its files under My ready-made configurations."}</p></Panel>}
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

      <Panel className="mb-4">
        {renaming ? (
          <form aria-label={t("service_rename")} onSubmit={event => { event.preventDefault(); void rename(); }}>
            <label className="mb-2 block text-sm font-bold" htmlFor="service-label">{t("service_label")}</label>
            <input id="service-label" className="mb-2 w-full" dir="auto" autoFocus
              value={label} disabled={busy !== null} maxLength={48}
              aria-describedby="service-label-hint"
              onChange={event => { setLabel(event.target.value); setRenameError(null); }} />
            <p id="service-label-hint" className="mb-3 text-sm text-[var(--hint)]">{t("service_rename_hint")}</p>
            {Boolean(renameError) && <p role="alert">{errorText(t, renameError)}</p>}
            <div className="grid grid-cols-2 gap-3">
              <Button type="submit" busy={busy === "rename"}
                disabled={busy !== null || !normalizedServiceLabel(label) || normalizedServiceLabel(label) === service.label}>
                {t("service_rename_save")}
              </Button>
              <Button tone="quiet" disabled={busy != null} onClick={() => { setRenaming(false); setRenameError(null); }}>
                {t("cancel")}
              </Button>
            </div>
          </form>
        ) : (
          <Button tone="quiet" disabled={busy != null} onClick={() => {
            setLabel(service.label || service.username); setRenameError(null); setRenaming(true);
          }}>{t("service_rename")}</Button>
        )}
      </Panel>

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
          {(service.sub_formats?.length ?? 0) > 0 && (
            <div className="mt-4 text-start">
              <p className="text-sm font-bold">{t("sub_formats")}</p>
              <p className="mb-2 text-xs text-[var(--hint)]">{t("sub_formats_hint")}</p>
              <div className="app-chips" role="group" aria-label={t("sub_formats")}>
                {service.sub_formats?.map(item => (
                  <button key={item.format} type="button" className="app-chip" onClick={() => copy(item.url)}>
                    {t(`sub_format_${item.format}`)}
                  </button>
                ))}
              </div>
            </div>
          )}
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

      {service.id === serviceId && <Button tone="quiet" disabled={busy !== null} onClick={() => navigate(`/tickets/new?service=${service.id}`)}>{t("tickets_service_report")}</Button>}
      {service.id === serviceId && !service.national_mode && <ServiceAccess key={`access-${service.id}`} serviceId={service.id} access={service.access} />}
      <UsageChart key={service.id} serviceId={service.id} />

      {(allowed("move") || allowed("cancel")) && (
        <Panel className="mt-4">
          {moving ? (
            <div>
              <p className="mb-1 text-sm font-bold">{t("move_title")}</p>
              <p className="mb-3 text-sm text-[var(--hint)]">
                {moving.price ? t("move_price_note", { amount: money(moving.price, locale) }) : t("move_free_note")}
              </p>
              <ul className="mb-3 flex flex-col gap-2">
                {moving.targets.map(target => (
                  <li key={target.location_id}>
                    <Button tone="quiet" onClick={() => navigate(`/checkout?kind=move&service=${service.id}&loc=${target.location_id}`)}>
                      {target.flag ? `${target.flag} ` : ""}{target.title}
                    </Button>
                  </li>
                ))}
              </ul>
              <Button tone="quiet" onClick={() => setMoving(null)}>{t("cancel")}</Button>
            </div>
          ) : cancelling !== null ? (
            <div role="alertdialog" aria-labelledby="cancel-title">
              <p id="cancel-title" className="mb-1 text-sm font-bold">{t("cancel_title")}</p>
              <p className="mb-3 text-sm">
                {cancelling ? t("cancel_refund_note", { amount: money(cancelling, locale) }) : t("cancel_no_refund_note")}
              </p>
              <div className="grid grid-cols-2 gap-3">
                <Button tone="danger" busy={busy === "cancel_go"} disabled={busy !== null} onClick={confirmCancel}>{t("cancel_confirm")}</Button>
                <Button tone="quiet" disabled={busy !== null} onClick={() => setCancelling(null)}>{t("cancel")}</Button>
              </div>
            </div>
          ) : (
            <div className={`grid gap-3 ${allowed("move") && allowed("cancel") ? "grid-cols-2" : "grid-cols-1"}`}>
              {allowed("move") && (
                <Button tone="quiet" busy={busy === "move"} disabled={busy !== null} onClick={openMove}>{t("move_service")}</Button>
              )}
              {allowed("cancel") && (
                <Button tone="danger" busy={busy === "cancel"} disabled={busy !== null} onClick={openCancel}>{t("cancel_service")}</Button>
              )}
            </div>
          )}
        </Panel>
      )}
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
