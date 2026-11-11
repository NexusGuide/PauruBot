import { type ReactNode, useState } from "react";
import { money } from "@shared/format";
import { call, type CatalogData, type Category, type Location, type PanelRow, type Plan } from "../api";
import { useAction, useLoad } from "../hooks";
import { OptionPicker } from "./Panels";
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
import { PlanPricesButton } from "./Tools";
import { ClonePlanButton } from "./ClockTools2";

type PlanDraft = Omit<Plan, "id" | "services" | "stock"> & { id?: number };

const BADGES = ["", "hot", "new", "best", "sale"];

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
  button_style: {},
  on_hold: false,
  username_prefix: null,
  panel_options: {},
  addons: {},
  delivery: "panel",
};

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
      <h3 className="text-[0.88rem] font-bold">{title}</h3>
      {children}
    </div>
  );
}

type PanelOptions = { ok: boolean; kind: string; error?: string; options: { id: string; title: string }[] };

function PlanPanelOptions({
  panelId,
  value,
  onChange,
}: {
  panelId: number;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const { t } = useSession();
  const state = useLoad<PanelOptions>(`/api/admin/plans/panel-options/${panelId}`);
  return (
    <Async {...state}>
      {(data) =>
        !data.ok ? (
          <p className="text-[0.82rem] text-[var(--hint)]">{t("plan_options_unreachable")}</p>
        ) : (
          <div className="flex flex-col gap-3">
            <OptionPicker kind={data.kind} options={data.options} value={value} onChange={onChange} />
            {!["pasarguard", "hiddify", "wgdashboard", "wgmate", "mikrotik", "ibsng"].includes(data.kind) && (
              <Field label={t("plan_flow")} hint={t("plan_flow_hint")}>
                {(id) => (
                  <Input
                    id={id}
                    dir="ltr"
                    maxLength={40}
                    value={String(value.flow ?? "")}
                    onChange={(e) => onChange({ ...value, flow: e.target.value.replace(/[^a-z0-9-]/g, "") || undefined })}
                  />
                )}
              </Field>
            )}
          </div>
        )
      }
    </Async>
  );
}

interface StockData {
  counts: { available: number; sold: number; disabled: number };
  items: {
    id: number;
    status: string;
    note: string | null;
    preview: string;
    created_at: string;
    sold_at: string | null;
    customer: string | null;
    customer_id: number | null;
  }[];
}

function StockEditor({ planId }: { planId: number }) {
  const { t, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const { data, reload } = useLoad<StockData>(`/api/admin/plans/${planId}/stock`);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [busy, run] = useAction();
  const [shown, setShown] = useState<{ id: number; payload: string } | null>(null);
  const n = (value: number) => money(value, locale);

  const add = async () => {
    try {
      const result = await run(() =>
        call<{ added: number; duplicates: number; too_long: number }>(`/api/admin/plans/${planId}/stock`, {
          method: "POST",
          body: { text, note: note.trim() || null },
        }),
      );
      if (result) {
        toast(t("stock_added", { added: n(result.added), duplicates: n(result.duplicates) }));
        setText("");
        await reload();
      }
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const remove = async (id: number) => {
    const ok = await confirm({ title: t("stock_delete_title"), body: t("stock_delete_body"), confirm: t("delete"), danger: true });
    if (!ok) return;
    try {
      await run(() => call(`/api/admin/stock/${id}`, { method: "DELETE" }));
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const reveal = async (id: number) => {
    try {
      const result = await run(() => call<{ id: number; payload: string }>(`/api/admin/stock/${id}`));
      if (result) setShown(result);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Group title={t("stock_title")}>
      {data && (
        <p className="text-[0.85rem]">{t("stock_counts", { available: n(data.counts.available), sold: n(data.counts.sold) })}</p>
      )}
      <Field label={t("stock_paste")} hint={t("stock_paste_hint")}>
        {(id) => <Textarea id={id} dir="ltr" rows={5} value={text} onChange={(e) => setText(e.target.value)} />}
      </Field>
      <Field label={t("stock_note")} hint={t("stock_note_hint")}>
        {(id) => <Input id={id} maxLength={128} value={note} onChange={(e) => setNote(e.target.value)} />}
      </Field>
      <Button busy={busy} disabled={!text.trim()} onClick={() => void add()}>
        {t("stock_add")}
      </Button>
      {data && data.items.length > 0 && (
        <ul className="divide-y divide-[var(--line)] rounded-xl ring-1 ring-[var(--line)]">
          {data.items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-[0.82rem]">
              <span className="min-w-0 flex-1 truncate font-mono" dir="ltr">
                {item.preview}
              </span>
              <Badge tone={item.status === "available" ? "health" : "neutral"}>{t(`stock_status_${item.status}`)}</Badge>
              {item.customer && <span className="text-[var(--hint)]">{item.customer}</span>}
              <Button small onClick={() => void reveal(item.id)}>
                {t("stock_show")}
              </Button>
              {item.status !== "sold" && (
                <Button small tone="danger" onClick={() => void remove(item.id)}>
                  {t("delete")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {shown && (
        <pre dir="ltr" className="max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-[var(--bg)] p-3 text-[0.78rem] ring-1 ring-[var(--line)]">
          {shown.payload}
        </pre>
      )}
    </Group>
  );
}

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
        <Button
          tone="primary"
          busy={busy}
          disabled={!form.title.trim() || (form.delivery === "stock" ? !form.location_id : !form.panel_id && !form.location_id)}
          onClick={() => void save()}
        >
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
        <Field label={t("plan_delivery")} hint={t(`plan_delivery_${form.delivery}_hint`)}>
          {(id) => (
            <Select id={id} value={form.delivery} onChange={(e) => set("delivery", e.target.value as PlanDraft["delivery"])}>
              <option value="panel">{t("plan_delivery_panel")}</option>
              <option value="stock">{t("plan_delivery_stock")}</option>
            </Select>
          )}
        </Field>
        {form.delivery === "panel" && (
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
        )}
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
        <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("on_sale")} />
        {form.delivery === "panel" && (
          <>
            <Toggle checked={form.renewable} onChange={(v) => set("renewable", v)} label={t("renewable")} />
            <Toggle checked={form.on_hold} onChange={(v) => set("on_hold", v)} label={t("plan_on_hold")} />
            <p className="-mt-2 text-[0.78rem] text-[var(--hint)]">{t("plan_on_hold_hint")}</p>
          </>
        )}
        {form.delivery === "stock" && form.id && <StockEditor planId={form.id} />}
        {form.delivery === "stock" && !form.id && (
          <p className="rounded-xl bg-[var(--bg)] px-3 py-2 text-[0.82rem] text-[var(--hint)]">{t("stock_save_first")}</p>
        )}

        <Group title={t("plan_look")}>
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("plan_emoji")}>
              {(id) => (
                <Input
                  id={id}
                  maxLength={8}
                  value={form.button_style.emoji ?? ""}
                  onChange={(e) => set("button_style", { ...form.button_style, emoji: e.target.value })}
                />
              )}
            </Field>
            <Field label={t("plan_badge")}>
              {(id) => (
                <Select
                  id={id}
                  value={form.button_style.badge ?? ""}
                  onChange={(e) => set("button_style", { ...form.button_style, badge: e.target.value })}
                >
                  {BADGES.map((badge) => (
                    <option key={badge} value={badge}>
                      {badge ? t(`badge_${badge}`) : "—"}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
          <Field label={t("username_prefix")} hint={t("plan_username_prefix_hint")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                maxLength={16}
                value={form.username_prefix ?? ""}
                onChange={(e) => set("username_prefix", e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "") || null)}
              />
            )}
          </Field>
          {form.delivery === "panel" && (
            <Field label={t("plan_renew_mode")} hint={t("plan_renew_mode_hint")}>
              {(id) => (
                <Select id={id} value={form.renew_mode ?? ""} onChange={(e) => set("renew_mode", e.target.value || null)}>
                  <option value="">{t("renew_mode_default")}</option>
                  {["extend_reset", "extend_carry", "restart_reset", "restart_carry"].map((mode) => (
                    <option key={mode} value={mode}>
                      {t(`renew_mode_${mode}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          )}
        </Group>

        {form.delivery === "panel" && (
        <Group title={t("plan_addons")}>
          <Toggle
            checked={form.addons.extras !== false}
            onChange={(v) => set("addons", { ...form.addons, extras: v })}
            label={t("plan_sell_extras")}
          />
          {form.addons.extras !== false && (
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("plan_extra_gb_price")} hint={t("plan_extra_default_hint")}>
                {(id) => (
                  <NumberInput
                    id={id}
                    allowEmpty
                    value={form.addons.extra_gb_price ?? null}
                    onChange={(v) => set("addons", { ...form.addons, extra_gb_price: v })}
                  />
                )}
              </Field>
              <Field label={t("plan_extra_day_price")} hint={t("plan_extra_default_hint")}>
                {(id) => (
                  <NumberInput
                    id={id}
                    allowEmpty
                    value={form.addons.extra_day_price ?? null}
                    onChange={(v) => set("addons", { ...form.addons, extra_day_price: v })}
                  />
                )}
              </Field>
            </div>
          )}
        </Group>
        )}

        {form.delivery === "panel" && form.panel_id && (
          <Group title={t("plan_panel_options")}>
            <p className="text-[0.78rem] text-[var(--hint)]">{t("plan_panel_options_hint")}</p>
            <PlanPanelOptions
              panelId={form.panel_id}
              value={form.panel_options}
              onChange={(value) => set("panel_options", value)}
            />
          </Group>
        )}
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
  const confirm = useConfirm();
  const [form, setForm] = useState<Simple>(draft);
  const [busy, run] = useAction();
  const remove = async () => {
    const ok = await confirm({
      title: t(kind === "locations" ? "delete_location" : "delete_category"),
      body: t(kind === "locations" ? "delete_location_body" : "delete_category_body", { title: draft.title }),
      confirm: t("delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await run(() => call(`/api/admin/${kind}/${draft.id}`, { method: "DELETE" }));
      toast(t("deleted_done"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
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
        <>
          {draft.id !== undefined && (
            <Button tone="danger" disabled={busy} onClick={() => void remove()}>
              {t("delete")}
            </Button>
          )}
          <Button tone="primary" busy={busy} disabled={!form.title.trim()} onClick={() => void save()}>
            {t("save")}
          </Button>
        </>
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
                <>
                  <PlanPricesButton onDone={() => void catalog.reload()} samplePrice={data.plans.find((p) => p.is_active)?.price} />
                  <Button tone="primary" icon="plus" onClick={() => setPlan({ ...EMPTY_PLAN })}>
                    {t("new_plan")}
                  </Button>
                </>
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
                          {row.stock && (
                            <Badge tone={row.stock.available ? "health" : "loss"}>
                              {t("stock_left", { n: money(row.stock.available, locale) })}
                            </Badge>
                          )}
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
                      {writable && <ClonePlanButton planId={row.id} onDone={() => void catalog.reload()} />}
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
