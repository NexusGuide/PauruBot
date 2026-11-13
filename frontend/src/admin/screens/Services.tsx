import { useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { ApiError, call, type PanelRow, type Service } from "../api";
import { PAGE_SIZE, useAction, useList, useLoad, useSelection } from "../hooks";
import { useSession } from "../session";
import { ServiceClaims } from "./Tools";
import {
  Async,
  Badge,
  BulkBar,
  Bytes,
  Button,
  Chips,
  CopyText,
  Empty,
  Field,
  Icon,
  Input,
  NumberInput,
  Page,
  Pager,
  SearchBox,
  Section,
  Select,
  Sheet,
  Status,
  Textarea,
  Table,
  Usage,
  When,
  useTypedConfirm,
  type Column,
  Toggle,
} from "../ui";

const SERVICE_STATUSES = ["active", "on_hold", "limited", "expired", "disabled", "pending", "deleted"];

function useServiceColumns(): Column<Service>[] {
  const { t } = useSession();
  return [
    {
      key: "name",
      label: t("col_service"),
      mobile: "title",
      render: (s) => (
        <span className="flex flex-col">
          <span className="font-semibold" dir="auto">
            {s.label || s.panel_username}
            {s.is_trial && (
              <span className="ms-2">
                <Badge tone="brand">{t("trial")}</Badge>
              </span>
            )}
          </span>
          {s.label && (
            <span className="text-[0.75rem] text-[var(--hint)]" dir="ltr">
              {s.panel_username}
            </span>
          )}
        </span>
      ),
    },
    { key: "customer", label: t("col_customer"), mobile: "meta", render: (s) => <bdi>{s.customer ?? "—"}</bdi> },
    { key: "panel", label: t("col_panel"), mobile: "meta", render: (s) => s.panel ?? "—" },
    { key: "status", label: t("col_status"), sort: "status", mobile: "end", render: (s) => <Status value={s.status} /> },
    {
      key: "usage",
      label: t("col_usage"),
      sort: "used_bytes",
      mobile: "meta",
      render: (s) => <Usage used={s.used_bytes} limit={s.data_limit} />,
    },
    {
      key: "expire",
      label: t("col_expires"),
      sort: "expire_at",
      mobile: "end",
      render: (s) => <Expiry s={s} />,
    },
  ];
}

/** Days left for a live service (amber in the last three), the date for anything else. */
function Expiry({ s }: { s: Service }) {
  const { t, locale } = useSession();
  if (!s.expire_at) return <span className="text-[0.82rem] text-[var(--hint)]">{t("never")}</span>;
  if (s.status !== "active") return <When iso={s.expire_at} />;
  const left = Math.max(0, Math.ceil((new Date(s.expire_at).getTime() - Date.now()) / 86_400_000));
  return (
    <span className={`text-[0.82rem] font-bold ${left <= 3 ? "text-saffron-700" : "text-[var(--text)]"}`} title={s.expire_at}>
      {t("cust_days_left", { n: money(left, locale) })}
    </span>
  );
}

/** Read-only service list used inside other pages (a customer's services). */
export function ServiceTable({ rows }: { rows: Service[] }) {
  const columns = useServiceColumns().filter((c) => c.key !== "customer");
  const [open, setOpen] = useState<Service | null>(null);
  return (
    <>
      <Table columns={columns} rows={rows} onRow={setOpen} />
      {open && <ServiceSheet service={open} onClose={() => setOpen(null)} onChange={setOpen} />}
    </>
  );
}

function ServiceSheet({
  service,
  onClose,
  onChange,
}: {
  service: Service;
  onClose: () => void;
  onChange: (service: Service) => void;
}) {
  const { t, can, me, locale, toast, errorText } = useSession();
  const typedConfirm = useTypedConfirm();
  const [busy, run] = useAction();
  const [days, setDays] = useState<number | null>(null);
  const [gb, setGb] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [notify, setNotify] = useState(true);
  const [moveTo, setMoveTo] = useState("");
  const writable = can("services.write");
  const panels = useLoad<{ items: PanelRow[] }>(writable && can("panels.read") ? "/api/admin/panels" : null);

  const move = async () => {
    try {
      await run(() =>
        call(`/api/admin/services/${service.id}/move`, { method: "POST", body: { panel_id: Number(moveTo) } }),
      );
      toast(t("svc_move_queued"));
      setMoveTo("");
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const act = async (action: string, extra: Record<string, unknown> = {}) => {
    try {
      const result = await run(async () => {
        const fields = action === "delete" ? await typedConfirm({
          title: t("delete_service_title"),
          body: t("delete_service_body", { name: service.label || service.panel_username }),
          confirm: t("delete"),
          target: { action: "service.delete", id: service.id },
        }) : {};
        if (fields === null) return null;
        return call<{ service: Service }>(`/api/admin/services/${service.id}/action`, {
          method: "POST",
          body: { action, ...extra, ...fields },
        });
      });
      if (!result) return;
      if (result) onChange(result.service);
      toast(t(`service_done_${action}`));
    } catch (err) {
      toast(err instanceof ApiError && err.status === 428 ? t("err_confirmation_required") : errorText(err), "error");
    }
  };

  const refresh = async () => {
    try {
      const result = await run(() => call<{ service: Service }>(`/api/admin/services/${service.id}?refresh=true`));
      if (result) onChange(result.service);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const remove = async () => {
    await act("delete", { reason: reason.trim() || null, notify });
  };

  const left = service.expire_at ? Math.max(0, Math.ceil((new Date(service.expire_at).getTime() - Date.now()) / 86_400_000)) : null;
  const ratio = service.data_limit > 0 ? Math.min(1, service.used_bytes / service.data_limit) : 0;
  const tile =
    "flex min-h-[4.25rem] flex-col items-center justify-center gap-1.5 rounded-2xl bg-[var(--surface-2)] px-1 text-[0.76rem] font-semibold ring-1 ring-[var(--line)] transition-colors hover:brightness-125 disabled:opacity-50";
  const fold = (title: string, children: ReactNode, danger = false) => (
    <details className={`group rounded-2xl ring-1 ${danger ? "ring-anar-100" : "ring-[var(--line)]"}`}>
      <summary className={`flex min-h-12 cursor-pointer list-none items-center justify-between gap-2 px-4 font-bold ${danger ? "text-anar-700" : ""}`}>
        {title}
        <Icon name="down" size={18} className="text-[var(--hint)] transition-transform group-open:rotate-180" />
      </summary>
      <div className="flex flex-col gap-3 border-t border-[var(--line)] p-4">{children}</div>
    </details>
  );
  return (
    <Sheet open onClose={onClose} title={service.label || service.panel_username} wide>
      <div className="flex flex-col gap-5">
        <section className="flex flex-col gap-3 rounded-2xl bg-[var(--bg)] p-4">
          <div className="flex flex-wrap items-center gap-2">
            <Status value={service.status} />
            {service.is_trial && <Badge tone="brand">{t("trial")}</Badge>}
            {service.status === "disabled" && service.disabled_reason && (
              <span className="text-[0.78rem] text-[var(--hint)]">{t(`disabled_by_${service.disabled_reason}`)}</span>
            )}
            <span className="flex-1" />
            <span className="text-[0.8rem] text-[var(--hint)]">{service.panel}</span>
          </div>
          <div className="flex items-end justify-between gap-3">
            <div className="flex flex-col">
              <span className="text-[0.75rem] text-[var(--hint)]">{t("col_usage")}</span>
              <span className="text-[1.05rem] font-extrabold">
                <Bytes value={service.used_bytes} />
                {service.data_limit > 0 && (
                  <span className="text-[0.8rem] font-semibold text-[var(--hint)]">
                    {" "}
                    / <Bytes value={service.data_limit} />
                  </span>
                )}
              </span>
            </div>
            <div className="flex flex-col items-end">
              <span className="text-[0.75rem] text-[var(--hint)]">{t("col_expires")}</span>
              <span className={`text-[1.05rem] font-extrabold ${left !== null && left <= 3 ? "text-saffron-700" : ""}`}>
                {left === null ? t("never") : service.status === "active" ? t("cust_days_left", { n: money(left, locale) }) : <When iso={service.expire_at} />}
              </span>
            </div>
          </div>
          {service.data_limit > 0 && (
            <div className="h-2 overflow-hidden rounded-full bg-[var(--surface-2)]">
              <div
                className={`h-full rounded-full ${ratio >= 1 ? "bg-anar-500" : ratio >= 0.85 ? "bg-saffron-500" : "bg-[var(--accent)]"}`}
                style={{ width: `${ratio * 100}%` }}
              />
            </div>
          )}
          <p className="flex flex-wrap gap-x-3 gap-y-1 text-[0.74rem] text-[var(--hint)]">
            <span>
              <Link className="font-semibold text-firouzeh-700" to={`/customers/${service.customer_id}`}>
                <bdi>{service.customer ?? `#${service.customer_id}`}</bdi>
              </Link>
            </span>
            <span dir="ltr">{service.panel_username}</span>
            <span>
              {t("synced_at")}: <When iso={service.synced_at} time />
            </span>
          </p>
        </section>

        <section className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <button type="button" className={tile} disabled={busy} onClick={() => void refresh()}>
            <Icon name="refresh" size={19} className="text-[var(--accent)]" />
            {t("refresh_from_panel")}
          </button>
          {writable && (
            <>
              {service.status === "disabled" ? (
                <button type="button" className={tile} disabled={busy} onClick={() => void act("enable")}>
                  <Icon name="check" size={19} className="text-pista-700" />
                  {t("enable")}
                </button>
              ) : (
                <button type="button" className={tile} disabled={busy} onClick={() => void act("disable", { reason: reason.trim() || null, notify })}>
                  <Icon name="close" size={19} className="text-saffron-700" />
                  {t("disable")}
                </button>
              )}
              <button type="button" className={tile} disabled={busy} onClick={() => void act("revoke")}>
                <Icon name="link" size={19} className="text-[var(--accent)]" />
                {t("revoke_link")}
              </button>
              <button type="button" className={tile} disabled={busy} onClick={() => void act("reset_usage")}>
                <Icon name="trend" size={19} className="text-[var(--accent)]" />
                {t("reset_usage")}
              </button>
            </>
          )}
        </section>

        {(service.branded_url || service.subscription_url) && (
          <section className="flex flex-col gap-2 rounded-2xl p-1">
            {service.branded_url && (
              <div className="flex flex-col gap-1">
                <p className="text-[0.76rem] font-semibold text-[var(--hint)]">{t("branded_link")}</p>
                <CopyText text={service.branded_url} short />
              </div>
            )}
            {service.subscription_url && (
              <div className="flex flex-col gap-1">
                <p className="text-[0.76rem] font-semibold text-[var(--hint)]">{t("subscription_link")}</p>
                <CopyText text={service.subscription_url} short />
              </div>
            )}
          </section>
        )}

        {writable &&
          me?.role !== "reseller" &&
          fold(
            t("gift_title"),
            <>
              <p className="text-[0.82rem] text-[var(--hint)]">{t("gift_hint")}</p>
              <div className="grid grid-cols-2 gap-3">
                <Field label={t("extra_days")}>{(id) => <NumberInput id={id} value={days} onChange={setDays} allowEmpty />}</Field>
                <Field label={t("extra_gb")}>{(id) => <NumberInput id={id} value={gb} onChange={setGb} allowEmpty />}</Field>
              </div>
              <Button tone="primary" busy={busy} disabled={!days && !gb} onClick={() => void act("extend", { days: days ?? 0, gb: gb ?? 0 })}>
                {t("apply_gift")}
              </Button>
            </>,
          )}
        {writable &&
          service.status !== "deleted" &&
          panels.data &&
          fold(
            t("svc_move_title"),
            <>
              <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("svc_move_hint")}</p>
              <Field label={t("svc_move_target")}>
                {(id) => (
                  <Select id={id} value={moveTo} onChange={(event) => setMoveTo(event.target.value)}>
                    <option value="">{t("svc_move_pick_panel")}</option>
                    {panels.data?.items
                      .filter((panel) => panel.id !== service.panel_id && panel.is_active)
                      .map((panel) => (
                        <option key={panel.id} value={panel.id}>
                          {panel.name}
                        </option>
                      ))}
                  </Select>
                )}
              </Field>
              <Button busy={busy} disabled={!moveTo} onClick={() => void move()}>
                {t("svc_move_start")}
              </Button>
            </>,
          )}

        {writable &&
          service.status !== "deleted" &&
          fold(t("svc_keep_title"), <ServiceKeep service={service} onChange={onChange} />)}

        <ServiceNote service={service} writable={writable} onChange={onChange} />

        {writable &&
          service.status !== "deleted" &&
          fold(
            t("delete_service"),
            <>
              <Field label={t("delete_reason")} hint={t("reason_shown_to_customer")}>
                {(id) => <Input id={id} value={reason} maxLength={255} onChange={(event) => setReason(event.target.value)} />}
              </Field>
              <Toggle checked={notify} onChange={setNotify} label={t("notify_customer")} hint={t("notify_customer_hint")} />
              <Button tone="danger" busy={busy} onClick={() => void remove()}>
                {t("delete_service")}
              </Button>
            </>,
            true,
          )}
      </div>
    </Sheet>
  );
}

/** A customer's services as cards: name, panel, status, a usage bar and the days left. */
export function ServiceCards({ rows }: { rows: Service[] }) {
  const { t, locale } = useSession();
  const [open, setOpen] = useState<Service | null>(null);
  if (rows.length === 0) return <Empty title={t("cust_no_service")} />;
  return (
    <>
      <ul className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {rows.map((s) => {
          const left = s.expire_at ? Math.max(0, Math.ceil((new Date(s.expire_at).getTime() - Date.now()) / 86_400_000)) : null;
          const ratio = s.data_limit > 0 ? Math.min(1, s.used_bytes / s.data_limit) : 0;
          return (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => setOpen(s)}
                className="flex w-full flex-col gap-3 rounded-[18px] bg-[var(--surface)] p-4 text-start ring-1 ring-[var(--line)] transition-colors hover:bg-[var(--surface-2)]"
              >
                <span className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-bold" dir="auto">
                    {s.label || s.panel_username}
                  </span>
                  {s.is_trial && <Badge tone="brand">{t("trial")}</Badge>}
                  <Status value={s.status} />
                </span>
                <span className="flex items-center justify-between text-[0.8rem]">
                  <span className="text-[var(--hint)]">
                    {s.panel ?? "—"} · <Bytes value={s.used_bytes} />
                    {s.data_limit > 0 && (
                      <>
                        {" / "}
                        <Bytes value={s.data_limit} />
                      </>
                    )}
                  </span>
                  <span className={`font-bold ${left !== null && left <= 3 && s.status === "active" ? "text-saffron-700" : ""}`}>
                    {left === null ? t("never") : s.status === "active" ? t("cust_days_left", { n: money(left, locale) }) : <When iso={s.expire_at} />}
                  </span>
                </span>
                {s.data_limit > 0 && (
                  <span className="block h-1.5 overflow-hidden rounded-full bg-[var(--surface-2)]">
                    <span
                      className={`block h-full rounded-full ${ratio >= 1 ? "bg-anar-500" : ratio >= 0.85 ? "bg-saffron-500" : "bg-[var(--accent)]"}`}
                      style={{ width: `${ratio * 100}%` }}
                    />
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      {open && <ServiceSheet service={open} onClose={() => setOpen(null)} onChange={setOpen} />}
    </>
  );
}

type BulkAction = "enable" | "disable" | "extend" | "delete" | "move";

export function Services() {
  const { t, can, me, locale, toast, errorText } = useSession();
  const typedConfirm = useTypedConfirm();
  const list = useList<Service>("/api/admin/services", ["q", "status", "panel_id", "plan_id", "customer_id"]);
  const [params] = useSearchParams();
  const panels = useLoad<{ items: PanelRow[] }>(can("panels.read") ? "/api/admin/panels" : null);
  const selection = useSelection(`${list.page}:${list.params.toString()}:${list.data?.total ?? 0}`);
  const columns = useServiceColumns();
  const [open, setOpen] = useState<Service | null>(null);
  const [bulkOpen, setBulkOpen] = useState<{ action: BulkAction; everything: boolean } | null>(null);
  const [days, setDays] = useState<number | null>(null);
  const [gb, setGb] = useState<number | null>(null);
  const [bulkReason, setBulkReason] = useState("");
  const [bulkNotify, setBulkNotify] = useState(true);
  const [bulkPanel, setBulkPanel] = useState("");
  const [busy, run] = useAction();

  const filter = {
    q: params.get("q") ?? "",
    status: params.get("status") || null,
    panel_id: params.get("panel_id") ? Number(params.get("panel_id")) : null,
    plan_id: params.get("plan_id") ? Number(params.get("plan_id")) : null,
    customer_id: params.get("customer_id") ? Number(params.get("customer_id")) : null,
  };

  const runBulk = async () => {
    if (!bulkOpen) return;
    const { action, everything } = bulkOpen;
    try {
      const body: Record<string, unknown> = {
        action,
        days: days ?? 0,
        gb: gb ?? 0,
        reason: bulkReason.trim() || null,
        notify: bulkNotify,
      };
      if (action === "move") body.panel_id = Number(bulkPanel);
      if (everything) body.filter = { ...filter };
      else body.ids = [...selection.selected];
      const result = await run(async () => {
        const fields = action === "delete" || action === "disable" ? await typedConfirm({
          title: t(`bulk_${action}_title`),
          body: t("typed_confirm_bulk_scope"),
          confirm: t(action === "delete" ? "delete" : "disable"),
          target: { action: action === "delete" ? "service.bulk_delete" : "service.bulk_disable", body },
        }) : {};
        if (fields === null) return null;
        return call<{ queued: number }>("/api/admin/services/bulk", { method: "POST", body: { ...body, ...fields } });
      });
      if (!result) return;
      toast(t(action === "move" ? "svc_move_bulk_queued" : "bulk_queued", { n: money(result?.queued ?? 0, locale) }));
      setBulkPanel("");
      setBulkOpen(null);
      setDays(null);
      setGb(null);
      selection.clear();
      window.setTimeout(() => void list.reload(), 2500);
    } catch (err) {
      toast(err instanceof ApiError && err.status === 428 ? t("err_confirmation_required") : errorText(err), "error");
    }
  };

  const writable = can("services.write");
  const canGift = writable && me?.role !== "reseller";

  return (
    <Page
      title={t("nav_services")}
      wide
      actions={
        canGift && (list.data?.total ?? 0) > 0 ? (
          <>
            {list.get("panel_id") && (
              <Button onClick={() => setBulkOpen({ action: "move", everything: true })}>
                {t("svc_move_all_matching", { n: money(list.data?.total ?? 0, locale) })}
              </Button>
            )}
            <Button onClick={() => setBulkOpen({ action: "extend", everything: true })}>
              {t("gift_all_matching", { n: money(list.data?.total ?? 0, locale) })}
            </Button>
          </>
        ) : null
      }
    >
      <ServiceClaims />
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <SearchBox value={list.get("q")} onChange={(q) => list.set({ q })} placeholder={t("search_services")} />
          {panels.data && (
            <Select
              className="w-auto"
              aria-label={t("col_panel")}
              value={list.get("panel_id")}
              onChange={(event) => list.set({ panel_id: event.target.value })}
            >
              <option value="">{t("filter_all_panels")}</option>
              {panels.data.items.map((panel) => (
                <option key={panel.id} value={panel.id}>
                  {panel.name}
                </option>
              ))}
            </Select>
          )}
        </div>
        <Chips
          label={t("filter_status")}
          value={list.get("status") || "all"}
          onChange={(status) => list.set({ status: status === "all" ? null : status })}
          options={[{ value: "all", label: t("filter_not_deleted") }, ...SERVICE_STATUSES.map((status) => ({ value: status, label: t(`status_${status}`) }))]}
        />
        {list.get("customer_id") && (
          <div>
            <Button small icon="close" onClick={() => list.set({ customer_id: null })}>
              {t("customer_filter", { id: list.get("customer_id") })}
            </Button>
          </div>
        )}
      </div>
      <Section flush>
        {writable && (
          <BulkBar count={selection.selected.size} onClear={selection.clear}>
            <Button small onClick={() => setBulkOpen({ action: "enable", everything: false })}>
              {t("enable")}
            </Button>
            <Button small onClick={() => setBulkOpen({ action: "disable", everything: false })}>
              {t("disable")}
            </Button>
            {canGift && (
              <Button small onClick={() => setBulkOpen({ action: "extend", everything: false })}>
                {t("gift_title")}
              </Button>
            )}
            {canGift && panels.data && (
              <Button small onClick={() => setBulkOpen({ action: "move", everything: false })}>
                {t("svc_move_title")}
              </Button>
            )}
            <Button small tone="danger" onClick={() => setBulkOpen({ action: "delete", everything: false })}>
              {t("delete")}
            </Button>
          </BulkBar>
        )}
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table
                columns={columns}
                rows={data.items}
                sort={list.sort}
                onSort={list.toggleSort}
                onRow={setOpen}
                selection={writable ? selection : undefined}
              />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>

      {open && (
        <ServiceSheet
          service={open}
          onClose={() => setOpen(null)}
          onChange={(service) => {
            setOpen(service);
            list.setData((prev) =>
              prev ? { ...prev, items: prev.items.map((row) => (row.id === service.id ? service : row)) } : prev,
            );
          }}
        />
      )}

      <Sheet
        open={bulkOpen !== null}
        onClose={() => setBulkOpen(null)}
        title={bulkOpen ? t(`bulk_${bulkOpen.action}_title`) : ""}
        footer={
          <Button
            tone={bulkOpen?.action === "delete" ? "danger" : "primary"}
            busy={busy}
            disabled={(bulkOpen?.action === "extend" && !days && !gb) || (bulkOpen?.action === "move" && !bulkPanel)}
            onClick={() => void runBulk()}
          >
            {t("apply_to_n", {
              n: money(bulkOpen?.everything ? list.data?.total ?? 0 : selection.selected.size, locale),
            })}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="leading-relaxed text-[var(--hint)]">
            {bulkOpen?.everything ? t("bulk_scope_filter") : t("bulk_scope_selected")}
          </p>
          {bulkOpen?.action === "extend" && (
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("extra_days")}>{(id) => <NumberInput id={id} value={days} onChange={setDays} allowEmpty />}</Field>
              <Field label={t("extra_gb")}>{(id) => <NumberInput id={id} value={gb} onChange={setGb} allowEmpty />}</Field>
            </div>
          )}
          {bulkOpen?.action === "move" && (
            <>
              <p className="text-[0.85rem] leading-relaxed">{t("svc_move_hint")}</p>
              <Field label={t("svc_move_target")}>
                {(id) => (
                  <Select id={id} value={bulkPanel} onChange={(event) => setBulkPanel(event.target.value)}>
                    <option value="">{t("svc_move_pick_panel")}</option>
                    {(panels.data?.items ?? [])
                      .filter((panel) => panel.is_active && String(panel.id) !== list.get("panel_id"))
                      .map((panel) => (
                        <option key={panel.id} value={panel.id}>
                          {panel.name}
                        </option>
                      ))}
                  </Select>
                )}
              </Field>
            </>
          )}
          {(bulkOpen?.action === "delete" || bulkOpen?.action === "disable") && (
            <>
              <Field label={t("delete_reason")} hint={t("reason_shown_to_customer")}>
                {(id) => <Input id={id} value={bulkReason} maxLength={255} onChange={(event) => setBulkReason(event.target.value)} />}
              </Field>
              <Toggle checked={bulkNotify} onChange={setBulkNotify} label={t("notify_customer")} hint={t("notify_customer_hint")} />
            </>
          )}
          <p className="text-[0.8rem] text-[var(--hint)]">{t("bulk_runs_in_background")}</p>
        </div>
      </Sheet>
    </Page>
  );
}

function ServiceKeep({ service, onChange }: { service: Service; onChange: (s: Service) => void }) {
  const { t, toast, errorText } = useSession();
  const [days, setDays] = useState<number | null>(30);
  const [busy, run] = useAction();
  const keep = async (body: { days?: number; forever?: boolean }) => {
    try {
      const result = await run(() => call<{ service: Service }>(`/api/admin/services/${service.id}/keep`, { method: "PUT", body }));
      if (result) onChange(result.service);
      toast(t("saved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const held = service.cleanup_exempt || (service.keep_until && new Date(service.keep_until).getTime() > Date.now());
  return (
    <>
      <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("svc_keep_hint")}</p>
      <p className="text-[0.85rem] font-semibold">
        {service.cleanup_exempt ? (
          t("svc_keep_forever_now")
        ) : held ? (
          <>
            {t("svc_keep_until_now")} <When iso={service.keep_until ?? null} />
          </>
        ) : (
          t("svc_keep_shop_rule")
        )}
      </p>
      {service.cleanup_warned_at && (
        <p className="text-[0.78rem] text-saffron-700">
          {t("svc_keep_warned")} <When iso={service.cleanup_warned_at} time />
        </p>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <Field label={t("svc_keep_days")}>{(id) => <NumberInput id={id} value={days} onChange={setDays} allowEmpty />}</Field>
        <Button busy={busy} disabled={!days || days < 1 || days > 3650} onClick={() => void keep({ days: days ?? 0 })}>
          {t("svc_keep_for_days")}
        </Button>
      </div>
      <div className="flex flex-wrap gap-2">
        {!service.cleanup_exempt && (
          <Button small busy={busy} onClick={() => void keep({ forever: true })}>
            {t("svc_keep_forever")}
          </Button>
        )}
        {held && (
          <Button small busy={busy} onClick={() => void keep({})}>
            {t("svc_keep_release")}
          </Button>
        )}
      </div>
    </>
  );
}

function ServiceNote({ service, writable, onChange }: { service: Service; writable: boolean; onChange: (s: Service) => void }) {
  const { t, toast, errorText } = useSession();
  const [note, setNote] = useState(service.staff_note ?? "");
  const [busy, run] = useAction();
  const save = async () => {
    try {
      const result = await run(() =>
        call<{ service: Service }>(`/api/admin/services/${service.id}/note`, { method: "PUT", body: { note: note.trim() || null } }),
      );
      if (result) onChange(result.service);
      toast(t("saved"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[0.78rem] text-[var(--hint)]">{t("service_staff_note")}</p>
      <Textarea value={note} maxLength={255} disabled={!writable} onChange={(e) => setNote(e.target.value)} />
      {writable && note !== (service.staff_note ?? "") && (
        <div>
          <Button small busy={busy} onClick={() => void save()}>
            {t("save")}
          </Button>
        </div>
      )}
    </div>
  );
}
