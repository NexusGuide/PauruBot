import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { money } from "@shared/format";
import { call, type PanelRow, type Service } from "../api";
import { PAGE_SIZE, useAction, useList, useLoad, useSelection } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  BulkBar,
  Button,
  CopyText,
  Facts,
  Field,
  Input,
  NumberInput,
  Page,
  Pager,
  SearchBox,
  Section,
  Select,
  Sheet,
  Status,
  Table,
  Toolbar,
  Usage,
  When,
  useConfirm,
  type Column,
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
    { key: "customer", label: t("col_customer"), mobile: "meta", render: (s) => s.customer ?? "—" },
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
      render: (s) => (s.expire_at ? <When iso={s.expire_at} /> : <span className="text-[var(--hint)]">{t("never")}</span>),
    },
  ];
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
  const { t, can, me, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  const [days, setDays] = useState<number | null>(null);
  const [gb, setGb] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const writable = can("services.write");

  const act = async (action: string, extra: Record<string, unknown> = {}) => {
    try {
      const result = await run(() =>
        call<{ service: Service }>(`/api/admin/services/${service.id}/action`, {
          method: "POST",
          body: { action, ...extra },
        }),
      );
      if (result) onChange(result.service);
      toast(t(`service_done_${action}`));
    } catch (err) {
      toast(errorText(err), "error");
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
    const ok = await confirm({
      title: t("delete_service_title"),
      body: t("delete_service_body", { name: service.label || service.panel_username }),
      confirm: t("delete"),
      danger: true,
    });
    if (ok) await act("delete", { reason: reason.trim() || null });
  };

  return (
    <Sheet open onClose={onClose} title={service.label || service.panel_username} wide>
      <div className="flex flex-col gap-6">
        <Facts
          items={[
            [t("col_status"), <Status key="st" value={service.status} />],
            [t("col_customer"), <Link key="c" className="text-firouzeh-700" to={`/customers/${service.customer_id}`}>{service.customer ?? `#${service.customer_id}`}</Link>],
            [t("col_panel"), service.panel],
            [t("panel_username"), <span key="u" dir="ltr">{service.panel_username}</span>],
            [t("col_usage"), <Usage key="us" used={service.used_bytes} limit={service.data_limit} />],
            [t("col_expires"), service.expire_at ? <When key="e" iso={service.expire_at} time /> : t("never")],
            [t("synced_at"), <When key="sy" iso={service.synced_at} time />],
            [t("col_created"), <When key="cr" iso={service.created_at} time />],
          ]}
        />
        {service.subscription_url && (
          <div className="flex flex-col gap-1">
            <p className="text-[0.78rem] text-[var(--hint)]">{t("subscription_link")}</p>
            <CopyText text={service.subscription_url} />
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <Button icon="refresh" busy={busy} onClick={() => void refresh()}>
            {t("refresh_from_panel")}
          </Button>
          {writable && (
            <>
              {service.status === "disabled" ? (
                <Button busy={busy} onClick={() => void act("enable")}>
                  {t("enable")}
                </Button>
              ) : (
                <Button busy={busy} onClick={() => void act("disable")}>
                  {t("disable")}
                </Button>
              )}
              <Button busy={busy} onClick={() => void act("revoke")}>
                {t("revoke_link")}
              </Button>
              <Button busy={busy} onClick={() => void act("reset_usage")}>
                {t("reset_usage")}
              </Button>
            </>
          )}
        </div>
        {writable && me?.role !== "reseller" && (
          <section className="flex flex-col gap-3 rounded-2xl bg-[var(--bg)] p-4">
            <h3 className="font-bold">{t("gift_title")}</h3>
            <p className="text-[0.82rem] text-[var(--hint)]">{t("gift_hint")}</p>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("extra_days")}>{(id) => <NumberInput id={id} value={days} onChange={setDays} allowEmpty />}</Field>
              <Field label={t("extra_gb")}>{(id) => <NumberInput id={id} value={gb} onChange={setGb} allowEmpty />}</Field>
            </div>
            <Button
              tone="primary"
              busy={busy}
              disabled={!days && !gb}
              onClick={() => void act("extend", { days: days ?? 0, gb: gb ?? 0 })}
            >
              {t("apply_gift")}
            </Button>
          </section>
        )}
        {writable && service.status !== "deleted" && (
          <section className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
            <Field label={t("delete_reason")}>
              {(id) => <Input id={id} value={reason} maxLength={255} onChange={(event) => setReason(event.target.value)} />}
            </Field>
            <Button tone="danger" busy={busy} onClick={() => void remove()}>
              {t("delete_service")}
            </Button>
          </section>
        )}
      </div>
    </Sheet>
  );
}

type BulkAction = "enable" | "disable" | "extend" | "delete";

export function Services() {
  const { t, can, me, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const list = useList<Service>("/api/admin/services", ["q", "status", "panel_id", "plan_id", "customer_id"]);
  const [params] = useSearchParams();
  const panels = useLoad<{ items: PanelRow[] }>(can("panels.read") ? "/api/admin/panels" : null);
  const selection = useSelection(`${list.page}:${list.params.toString()}:${list.data?.total ?? 0}`);
  const columns = useServiceColumns();
  const [open, setOpen] = useState<Service | null>(null);
  const [bulkOpen, setBulkOpen] = useState<{ action: BulkAction; everything: boolean } | null>(null);
  const [days, setDays] = useState<number | null>(null);
  const [gb, setGb] = useState<number | null>(null);
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
    const count = everything ? list.data?.total ?? 0 : selection.selected.size;
    if (action === "delete" || action === "disable") {
      const ok = await confirm({
        title: t(`bulk_${action}_title`),
        body: t("bulk_confirm_body", { n: money(count, locale) }),
        confirm: t(action === "delete" ? "delete" : "disable"),
        danger: true,
      });
      if (!ok) return;
    }
    try {
      const body: Record<string, unknown> = { action, days: days ?? 0, gb: gb ?? 0 };
      if (everything) body.filter = filter;
      else body.ids = [...selection.selected];
      const result = await run(() => call<{ queued: number }>("/api/admin/services/bulk", { method: "POST", body }));
      toast(t("bulk_queued", { n: money(result?.queued ?? 0, locale) }));
      setBulkOpen(null);
      setDays(null);
      setGb(null);
      selection.clear();
      window.setTimeout(() => void list.reload(), 2500);
    } catch (err) {
      toast(errorText(err), "error");
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
          <Button onClick={() => setBulkOpen({ action: "extend", everything: true })}>
            {t("gift_all_matching", { n: money(list.data?.total ?? 0, locale) })}
          </Button>
        ) : null
      }
    >
      <Section flush>
        <Toolbar>
          <SearchBox value={list.get("q")} onChange={(q) => list.set({ q })} placeholder={t("search_services")} />
          <Select
            className="w-auto"
            aria-label={t("filter_status")}
            value={list.get("status")}
            onChange={(event) => list.set({ status: event.target.value })}
          >
            <option value="">{t("filter_not_deleted")}</option>
            {SERVICE_STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`status_${status}`)}
              </option>
            ))}
          </Select>
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
          {list.get("customer_id") && (
            <Button small onClick={() => list.set({ customer_id: null })}>
              {t("customer_filter", { id: list.get("customer_id") })} ✕
            </Button>
          )}
        </Toolbar>
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
            disabled={bulkOpen?.action === "extend" && !days && !gb}
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
          <p className="text-[0.8rem] text-[var(--hint)]">{t("bulk_runs_in_background")}</p>
        </div>
      </Sheet>
    </Page>
  );
}
