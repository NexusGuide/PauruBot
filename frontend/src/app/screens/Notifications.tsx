import { useCallback, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dateTime, money } from "@shared/format";
import { api } from "../api";
import { buildAlerts, markAlertsRead, readAlertIds } from "../alerts";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { Empty, Loading, ReadFailure, Screen } from "../ui";
import { useResource } from "../useResource";

type Filter = "all" | "unread" | "service" | "wallet";

export function Notifications() {
  const { t, locale, toast } = useApp();
  const navigate = useNavigate();
  const load = useCallback(async () => {
    const [services, history] = await Promise.all([api.services(), api.history()]);
    return { services: services.items, history: history.items };
  }, []);
  const { data, error, retry } = useResource(load);
  const [read, setRead] = useState<Set<string>>(readAlertIds);
  const [filter, setFilter] = useState<Filter>("all");

  const alerts = useMemo(() => (data ? buildAlerts(data.services, data.history, t, locale) : []), [data, t, locale]);
  const unread = alerts.filter((alert) => !read.has(alert.id));
  const visible =
    filter === "all" ? alerts : filter === "unread" ? unread : alerts.filter((alert) => alert.kind === filter);
  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: t("all") },
    { id: "unread", label: t("notif_unread") },
    { id: "service", label: t("notif_services") },
    { id: "wallet", label: t("notif_wallet") },
  ];

  const markAll = () => {
    markAlertsRead(alerts.map((alert) => alert.id));
    setRead(readAlertIds());
    haptic("success");
    toast(t("notif_marked"));
  };
  const open = (id: string, href: string | null) => {
    markAlertsRead([id]);
    setRead(readAlertIds());
    if (href) navigate(href);
  };

  return (
    <Screen
      title={t("notifications")}
      action={
        alerts.length > 0 ? (
          <button type="button" className="app-chip" onClick={markAll}>
            {t("notif_mark_all")}
          </button>
        ) : undefined
      }
    >
      {error ? (
        <ReadFailure error={error} retry={retry} />
      ) : !data ? (
        <Loading />
      ) : (
        <>
          <div className="app-chips" role="group" aria-label={t("notifications")}>
            {filters.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={filter === item.id}
                className="app-chip"
                onClick={() => {
                  haptic("select");
                  setFilter(item.id);
                }}
              >
                {item.label}
                {item.id === "unread" && unread.length > 0 && (
                  <span className="app-badge" data-tone="red">
                    {money(unread.length, locale)}
                  </span>
                )}
              </button>
            ))}
          </div>
          {visible.length === 0 ? (
            <Empty title={t("notif_empty")} hint={t("notif_empty_hint")} />
          ) : (
            <div className="app-card">
              {visible.map((alert) => (
                <button
                  key={alert.id}
                  type="button"
                  onClick={() => open(alert.id, alert.href)}
                  className="app-row app-rip-host flex w-full items-start gap-3 px-4 text-start"
                >
                  <span className="app-ico" data-tone={alert.tone}>
                    <Icon name={alert.icon} size={21} />
                  </span>
                  <span className="app-grow">
                    <span className="flex items-baseline justify-between gap-2">
                      <b className="text-[0.93rem] font-bold leading-7">{alert.title}</b>
                      {!read.has(alert.id) && (
                        <span className="h-2 w-2 shrink-0 rounded-full bg-[var(--accent)]" aria-hidden="true" />
                      )}
                    </span>
                    <span className="mt-0.5 block text-[0.82rem] font-medium leading-6 text-[var(--hint)]">
                      {alert.body}
                    </span>
                    {alert.at && (
                      <time className="num mt-1 block text-[0.72rem] font-medium text-[var(--hint)]">
                        {dateTime(alert.at, locale)}
                      </time>
                    )}
                  </span>
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </Screen>
  );
}
