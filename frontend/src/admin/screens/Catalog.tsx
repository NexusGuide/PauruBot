import { useState } from "react";
import { money } from "@shared/format";
import { call, type CatalogData, type Category, type Location, type PanelRow, type Plan } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Button,
  Empty,
  Field,
  Input,
  Money,
  NumberInput,
  Page,
  Section,
  Select,
  Sheet,
  Textarea,
  Toggle,
  useConfirm,
} from "../ui";

type PlanDraft = Omit<Plan, "id" | "services" | "button_style"> & { id?: number };

const EMPTY_PLAN: PlanDraft = {
  title: "",
  description: null,
  category_id: null,
  location_id: null,
  panel_id: null,
  price: 0,
  days: 30,
  data_gb: 50,
  ip_limit: 0,
  audience: "all",
  renewable: true,
  sort: 0,
  is_active: true,
};

function PlanSheet({
  draft,
  catalog,
  panels,
  onClose,
  onSaved,
}: {
  draft: PlanDraft;
  catalog: CatalogData;
  panels: PanelRow[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState<PlanDraft>(draft);
  const [busy, run] = useAction();
  const set = <K extends keyof PlanDraft>(key: K, value: PlanDraft[K]) => setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() =>
        call(id ? `/api/admin/plans/${id}` : "/api/admin/plans", { method: id ? "PUT" : "POST", body }),
      );
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const idOrNull = (value: string) => (value ? Number(value) : null);

  return (
    <Sheet
      open
      onClose={onClose}
      title={form.id ? t("edit_plan") : t("new_plan")}
      footer={
        <Button tone="primary" busy={busy} disabled={!form.title.trim() || (!form.panel_id && !form.location_id)} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("plan_title")}>
          {(id) => <Input id={id} value={form.title} maxLength={96} onChange={(e) => set("title", e.target.value)} />}
        </Field>
        <Field label={t("plan_description")} hint={t("plan_description_hint")}>
          {(id) => (
            <Textarea id={id} value={form.description ?? ""} maxLength={1000} onChange={(e) => set("description", e.target.value || null)} />
          )}
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label={t("price_toman")}>{(id) => <NumberInput id={id} value={form.price} onChange={(v) => set("price", v ?? 0)} />}</Field>
          <Field label={t("days")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.days} onChange={(v) => set("days", v ?? 0)} />}
          </Field>
          <Field label={t("gb")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.data_gb} onChange={(v) => set("data_gb", v ?? 0)} />}
          </Field>
        </div>
        <Field label={t("col_panel")} hint={t("plan_panel_hint")}>
          {(id) => (
            <Select id={id} value={form.panel_id ?? ""} onChange={(e) => set("panel_id", idOrNull(e.target.value))}>
              <option value="">{t("by_location")}</option>
              {panels.map((panel) => (
                <option key={panel.id} value={panel.id}>
                  {panel.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("location")}>
            {(id) => (
              <Select id={id} value={form.location_id ?? ""} onChange={(e) => set("location_id", idOrNull(e.target.value))}>
                <option value="">—</option>
                {catalog.locations.map((loc) => (
                  <option key={loc.id} value={loc.id}>
                    {loc.flag ? `${loc.flag} ` : ""}
                    {loc.title}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("category")}>
            {(id) => (
              <Select id={id} value={form.category_id ?? ""} onChange={(e) => set("category_id", idOrNull(e.target.value))}>
                <option value="">—</option>
                {catalog.categories.map((cat) => (
                  <option key={cat.id} value={cat.id}>
                    {cat.title}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("audience")}>
            {(id) => (
              <Select id={id} value={form.audience} onChange={(e) => set("audience", e.target.value)}>
                <option value="all">{t("audience_all")}</option>
                <option value="customers">{t("audience_customers")}</option>
                <option value="resellers">{t("audience_resellers")}</option>
              </Select>
            )}
          </Field>
          <Field label={t("ip_limit")} hint={t("zero_unlimited")}>
            {(id) => <NumberInput id={id} value={form.ip_limit} onChange={(v) => set("ip_limit", v ?? 0)} />}
          </Field>
        </div>
        <Field label={t("sort_order")} hint={t("sort_order_hint")}>
          {(id) => <NumberInput id={id} value={form.sort} onChange={(v) => set("sort", v ?? 0)} />}
        </Field>
        <Toggle checked={form.renewable} onChange={(v) => set("renewable", v)} label={t("renewable")} />
        <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("on_sale")} />
      </div>
    </Sheet>
  );
}

type Simple = { id?: number; title: string; sort: number; is_active: boolean; flag?: string | null };

function SimpleSheet({
  kind,
  draft,
  onClose,
  onSaved,
}: {
  kind: "locations" | "categories";
  draft: Simple;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState<Simple>(draft);
  const [busy, run] = useAction();
  const save = async () => {
    const { id, ...body } = form;
    if (kind === "categories") delete body.flag;
    try {
      await run(() => call(id ? `/api/admin/${kind}/${id}` : `/api/admin/${kind}`, { method: id ? "PUT" : "POST", body }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={kind === "locations" ? t("location") : t("category")}
      footer={
        <Button tone="primary" busy={busy} disabled={!form.title.trim()} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("title")}>
          {(id) => <Input id={id} value={form.title} maxLength={64} onChange={(e) => setForm({ ...form, title: e.target.value })} />}
        </Field>
        {kind === "locations" && (
          <Field label={t("flag")} hint={t("flag_hint")}>
            {(id) => (
              <Input id={id} value={form.flag ?? ""} maxLength={16} onChange={(e) => setForm({ ...form, flag: e.target.value || null })} />
            )}
          </Field>
        )}
        <Field label={t("sort_order")}>
          {(id) => <NumberInput id={id} value={form.sort} onChange={(v) => setForm({ ...form, sort: v ?? 0 })} />}
        </Field>
        <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("visible")} />
      </div>
    </Sheet>
  );
}

export function Catalog() {
  const { t, can, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const catalog = useLoad<CatalogData>("/api/admin/catalog");
  const panels = useLoad<{ items: PanelRow[] }>(can("panels.read") ? "/api/admin/panels" : null);
  const [plan, setPlan] = useState<PlanDraft | null>(null);
  const [simple, setSimple] = useState<{ kind: "locations" | "categories"; draft: Simple } | null>(null);
  const writable = can("catalog.write");

  const archive = async (row: Plan) => {
    const ok = await confirm({ title: t("archive_plan"), body: t("archive_plan_body", { title: row.title }), confirm: t("archive"), danger: true });
    if (!ok) return;
    try {
      await call(`/api/admin/plans/${row.id}`, { method: "DELETE" });
      toast(t("archived"));
      void catalog.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const panelName = (id: number | null) => panels.data?.items.find((p) => p.id === id)?.name;

  return (
    <Async data={catalog.data} error={catalog.error} reload={catalog.reload}>
      {(data) => {
        const byCategory = new Map<number | null, Plan[]>();
        for (const row of data.plans) {
          const list = byCategory.get(row.category_id) ?? [];
          list.push(row);
          byCategory.set(row.category_id, list);
        }
        const groups: { key: string; title: string; rows: Plan[] }[] = [
          ...data.categories.map((cat: Category) => ({ key: `c${cat.id}`, title: cat.title, rows: byCategory.get(cat.id) ?? [] })),
          { key: "none", title: t("uncategorised"), rows: byCategory.get(null) ?? [] },
        ].filter((group) => group.rows.length > 0);

        return (
          <Page
            title={t("nav_catalog")}
            actions={
              writable && (
                <Button tone="primary" icon="plus" onClick={() => setPlan({ ...EMPTY_PLAN })}>
                  {t("new_plan")}
                </Button>
              )
            }
          >
            {groups.length === 0 && (
              <Section>
                <Empty title={t("no_plans")} hint={t("no_plans_hint")} />
              </Section>
            )}
            {groups.map((group) => (
              <Section key={group.key} title={group.title} flush>
                <ul className="divide-y divide-[var(--line)]">
                  {group.rows.map((row) => (
                    <li key={row.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                      <button
                        type="button"
                        className="flex min-w-0 flex-1 flex-col gap-0.5 text-start disabled:cursor-default"
                        disabled={!writable}
                        onClick={() => setPlan({ ...row })}
                      >
                        <span className="flex flex-wrap items-center gap-2 font-semibold">
                          {row.title}
                          {!row.is_active && <Badge>{t("off_sale")}</Badge>}
                          {row.audience !== "all" && <Badge tone="brand">{t(`audience_${row.audience}`)}</Badge>}
                        </span>
                        <span className="text-[0.8rem] text-[var(--hint)]">
                          {t("plan_specs", {
                            days: row.days ? money(row.days, locale) : t("unlimited"),
                            gb: row.data_gb ? money(row.data_gb, locale) : t("unlimited"),
                          })}
                          {" — "}
                          {panelName(row.panel_id) ??
                            data.locations.find((l: Location) => l.id === row.location_id)?.title ??
                            "—"}
                          {" — "}
                          {t("services_n", { n: money(row.services ?? 0, locale) })}
                        </span>
                      </button>
                      <Money amount={row.price} className="font-bold" />
                      {writable && row.is_active && (
                        <Button small tone="ghost" onClick={() => void archive(row)}>
                          {t("archive")}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              </Section>
            ))}

            <div className="grid gap-6 md:grid-cols-2">
              {(["locations", "categories"] as const).map((kind) => (
                <Section
                  key={kind}
                  title={kind === "locations" ? t("locations") : t("categories")}
                  flush
                  actions={
                    writable && (
                      <Button small icon="plus" onClick={() => setSimple({ kind, draft: { title: "", sort: 0, is_active: true, flag: null } })}>
                        {t("add")}
                      </Button>
                    )
                  }
                >
                  <ul className="divide-y divide-[var(--line)]">
                    {(kind === "locations" ? data.locations : data.categories).length === 0 && (
                      <li className="px-4 py-6 text-center text-[0.85rem] text-[var(--hint)]">{t("empty_list")}</li>
                    )}
                    {(kind === "locations" ? data.locations : data.categories).map((row: Simple & { id: number }) => (
                      <li key={row.id}>
                        <button
                          type="button"
                          disabled={!writable}
                          onClick={() => setSimple({ kind, draft: { ...row } })}
                          className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-start hover:bg-[var(--bg)] disabled:hover:bg-transparent"
                        >
                          <span className="font-semibold">
                            {row.flag ? `${row.flag} ` : ""}
                            {row.title}
                          </span>
                          {!row.is_active && <Badge>{t("hidden")}</Badge>}
                        </button>
                      </li>
                    ))}
                  </ul>
                </Section>
              ))}
            </div>

            {plan && (
              <PlanSheet
                draft={plan}
                catalog={data}
                panels={panels.data?.items ?? []}
                onClose={() => setPlan(null)}
                onSaved={() => {
                  setPlan(null);
                  void catalog.reload();
                }}
              />
            )}
            {simple && (
              <SimpleSheet
                kind={simple.kind}
                draft={simple.draft}
                onClose={() => setSimple(null)}
                onSaved={() => {
                  setSimple(null);
                  void catalog.reload();
                }}
              />
            )}
          </Page>
        );
      }}
    </Async>
  );
}
