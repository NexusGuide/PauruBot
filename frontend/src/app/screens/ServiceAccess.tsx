import { useState } from "react";
import { dateTime, digits } from "@shared/format";
import { api, type ServiceAccess as Access, type ServiceDevices } from "../api";
import { errorText, useApp } from "../state";
import { Button, Panel } from "../ui";

/** When the service was last connected, from which app, and (on request) from where (F-11). */
export function ServiceAccess({ serviceId, access }: { serviceId: number; access: Access | undefined }) {
  const { t, locale } = useApp();
  const [devices, setDevices] = useState<ServiceDevices | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!access) return null;
  const show = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setDevices(await api.serviceDevices(serviceId));
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  };
  return <Panel className="mb-4">
    <p className="mb-1 text-sm font-bold">{t("access_title")}</p>
    <div className="app-ln">
      <small>{t("access_online")}</small>
      <b className="num">{access.online_at ? dateTime(access.online_at, locale) : t("access_never")}</b>
    </div>
    <div className="app-ln border-t border-[var(--line)]">
      <small>{t("access_client")}</small>
      <b dir="ltr">{access.client ?? t("access_unknown")}</b>
    </div>
    {access.client && access.client_at && <p className="text-xs text-[var(--hint)]">
      {t("access_client_at", { when: dateTime(access.client_at, locale) })}
    </p>}
    {access.devices && <div className="mt-3">
      {devices ? <>
        <p className="text-sm font-semibold">
          {devices.limit
            ? t("access_devices_of", { count: digits(devices.count, locale), limit: digits(devices.limit, locale) })
            : t("access_devices", { count: digits(devices.count, locale) })}
        </p>
        {devices.addresses.length > 0
          ? <ul className="mt-1 grid gap-1">{devices.addresses.map((address, i) =>
            <li key={`${address}-${i}`} className="num text-sm" dir="ltr">{address}</li>)}</ul>
          : <p className="text-sm text-[var(--hint)]">{t("access_devices_none")}</p>}
        <p className="mt-1 text-xs text-[var(--hint)]">{t("access_devices_hint")}</p>
      </> : null}
      {Boolean(error) && <p role="alert" className="text-sm text-[var(--danger)]">{errorText(t, error)}</p>}
      <Button tone="quiet" busy={busy} disabled={busy} onClick={() => { void show(); }}>
        {t(devices ? "access_devices_refresh" : "access_devices_show")}
      </Button>
    </div>}
  </Panel>;
}
