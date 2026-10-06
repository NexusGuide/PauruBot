import { useState } from "react";
import { money } from "@shared/format";
import { call, type CatalogData, type PanelRow } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Button,
  Checkbox,
  CopyText,
  Empty,
  Facts,
  Field,
  Input,
  NumberInput,
  Page,
  Section,
  Select,
  Sheet,
  Toggle,
  When,
} from "../ui";

interface Draft {
  id?: number;
  name: string;
  kind: string;
  base_url: string;
  username: string;
  password: string;
  api_token: string;
  location_id: number | null;
  capacity: number | null;
  username_method: string;
  username_prefix: string;
  is_active: boolean;
  allow_private_address: boolean;
  options: Record<string, unknown>;
}

const USERNAME_METHODS = ["telegram_id", "telegram_username", "random", "sequential", "custom_label"];

function toDraft(panel?: PanelRow): Draft {
  return {
    id: panel?.id,
    name: panel?.name ?? "",
    kind: panel?.kind ?? "pasarguard",
    base_url: panel?.base_url ?? "https://",
    username: panel?.username ?? "",
    password: "",
    api_token: "",
    location_id: panel?.location_id ?? null,
    capacity: panel?.capacity ?? null,
    username_method: panel?.username_method ?? "telegram_id",
    username_prefix: panel?.username_prefix ?? "",
    is_active: panel?.is_active ?? true,
    allow_private_address: panel?.allow_private_address ?? false,
    options: { ...(panel?.options ?? {}) },
  };
}

interface TestResult {
  ok: boolean;
  error?: string;
  status?: number | null;
  detail?: string;
  users?: number | null;
  version?: string | null;
  options?: { id: string; title: string }[];
}

export function OptionPicker({
  kind,
  options,
  value,
  onChange,
}: {
  kind: string;
  options: { id: string; title: string }[];
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const { t } = useSession();
  if (kind === "xui") {
    return (
      <Field label={t("panel_inbound")} hint={t("panel_inbound_hint")}>
        {(id) => (
          <Select
            id={id}
            value={String(value.inbound_id ?? "")}
            onChange={(e) => onChange({ ...value, inbound_id: e.target.value ? Number(e.target.value) : undefined })}
          >
            <option value="">—</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.title}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  const key = kind === "pasarguard" ? "group_ids" : "inbounds";
  const selected = new Set((value[key] as (string | number)[] | undefined)?.map(String) ?? []);
  const toggle = (id: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    const list = [...next];
    onChange({ ...value, [key]: kind === "pasarguard" ? list.map(Number) : list });
  };
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-[0.82rem] font-semibold">
        {kind === "pasarguard" ? t("panel_groups") : t("panel_inbounds")}
      </legend>
      {options.map((option) => (
        <label key={option.id} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox label={option.title} checked={selected.has(option.id)} onChange={(on) => toggle(option.id, on)} />
          <span dir="ltr">{option.title}</span>
        </label>
      ))}
      <p className="text-[0.78rem] text-[var(--hint)]">{t("panel_options_hint")}</p>
    </fieldset>
  );
}

function PanelSheet({
  panel,
  locations,
  onClose,
  onSaved,
}: {
  panel?: PanelRow;
  locations: CatalogData["locations"];
  onClose: () => void;
  onSaved: (panel: PanelRow) => void;
}) {
  const { t, can, toast, errorText } = useSession();
  const [form, setForm] = useState<Draft>(() => toDraft(panel));
  const [test, setTest] = useState<TestResult | null>(null);
  const [cert, setCert] = useState<{ pem: string; fingerprint: string } | null>(null);
  const [webhook, setWebhook] = useState<{ secret: string; url: string } | null>(null);
  const [busy, run] = useAction();
  const owner = can("panels.secrets");
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setForm((prev) => ({ ...prev, [key]: value }));
  const setOption = (key: string, value: unknown) =>
    setForm((prev) => {
      const options = { ...prev.options };
      if (value === "" || value === null || value === undefined) delete options[key];
      else options[key] = value;
      return { ...prev, options };
    });

  const save = async (): Promise<PanelRow | undefined> => {
    const { id, password, api_token, ...rest } = form;
    const body: Record<string, unknown> = { ...rest };
    if (password) body.password = password;
    if (api_token) body.api_token = api_token;
    try {
      const saved = await run(() =>
        call<PanelRow>(id ? `/api/admin/panels/${id}` : "/api/admin/panels", { method: id ? "PUT" : "POST", body }),
      );
      if (saved) {
        setForm((prev) => ({ ...prev, id: saved.id, password: "", api_token: "" }));
        onSaved(saved);
        toast(t("saved"));
      }
      return saved;
    } catch (err) {
      toast(errorText(err), "error");
      return undefined;
    }
  };

  const runTest = async () => {
    let id = form.id;
    if (!id) id = (await save())?.id;
    if (!id) return;
    try {
      setTest((await run(() => call<TestResult>(`/api/admin/panels/${id}/test`, { method: "POST" }))) ?? null);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const inspect = async () => {
    try {
      setCert(
        (await run(() =>
          call<{ pem: string; fingerprint: string }>("/api/admin/panels/certificate", {
            method: "POST",
            body: { url: form.base_url, allow_private: form.allow_private_address },
          }),
        )) ?? null,
      );
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const pin = async (pem: string | null) => {
    if (!form.id) return;
    try {
      const saved = await run(() => call<PanelRow>(`/api/admin/panels/${form.id}/pin`, { method: "POST", body: { pem } }));
      if (saved) onSaved(saved);
      setCert(null);
      toast(pem ? t("cert_pinned") : t("cert_unpinned"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const rotateWebhook = async () => {
    if (!form.id) return;
    try {
      setWebhook((await run(() => call<{ secret: string; url: string }>(`/api/admin/panels/${form.id}/webhook`, { method: "POST" }))) ?? null);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const editingSecrets = !form.id || owner;
  const canSave = form.name.trim() && form.base_url.startsWith("http") && form.username.trim() && (form.id || form.password);

  return (
    <Sheet
      open
      onClose={onClose}
      title={form.id ? form.name || t("panel") : t("new_panel")}
      wide
      footer={
        <>
          <Button busy={busy} onClick={() => void runTest()} disabled={!canSave}>
            {t("test_connection")}
          </Button>
          <Button tone="primary" busy={busy} disabled={!canSave} onClick={() => void save()}>
            {t("save")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("panel_name")}>
            {(id) => <Input id={id} value={form.name} maxLength={64} onChange={(e) => set("name", e.target.value)} />}
          </Field>
          <Field label={t("panel_kind")}>
            {(id) => (
              <Select id={id} value={form.kind} onChange={(e) => set("kind", e.target.value)} disabled={Boolean(form.id)}>
                <option value="pasarguard">PasarGuard</option>
                <option value="marzban">Marzban</option>
                <option value="xui">3x-ui (Sanaei)</option>
              </Select>
            )}
          </Field>
        </div>
        <Field label={t("panel_url")} hint={t("panel_url_hint")}>
          {(id) => (
            <Input id={id} dir="ltr" value={form.base_url} disabled={!editingSecrets} onChange={(e) => set("base_url", e.target.value)} />
          )}
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("panel_username")}>
            {(id) => <Input id={id} dir="ltr" autoComplete="off" value={form.username} onChange={(e) => set("username", e.target.value)} />}
          </Field>
          <Field label={t("password")} hint={form.id ? t("secret_unchanged_hint") : undefined}>
            {(id) => (
              <Input
                id={id}
                type="password"
                dir="ltr"
                autoComplete="new-password"
                disabled={!editingSecrets}
                value={form.password}
                placeholder={form.id ? "••••••••" : ""}
                onChange={(e) => set("password", e.target.value)}
              />
            )}
          </Field>
        </div>
        {form.kind === "xui" && (
          <>
            <Field label={t("panel_api_token")} hint={t("panel_api_token_hint")}>
              {(id) => (
                <Input
                  id={id}
                  type="password"
                  dir="ltr"
                  autoComplete="off"
                  disabled={!editingSecrets}
                  value={form.api_token}
                  placeholder={panel?.has_api_token ? "••••••••" : ""}
                  onChange={(e) => set("api_token", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("panel_sub_base")} hint={t("panel_sub_base_hint")}>
              {(id) => (
                <Input
                  id={id}
                  dir="ltr"
                  value={String(form.options.sub_base_url ?? "")}
                  onChange={(e) => setOption("sub_base_url", e.target.value)}
                />
              )}
            </Field>
          </>
        )}
        {form.kind !== "pasarguard" && (
          <Field label={t("panel_flow")} hint={t("panel_flow_hint")}>
            {(id) => (
              <Select id={id} value={String(form.options.flow ?? "")} onChange={(e) => setOption("flow", e.target.value)}>
                <option value="">—</option>
                <option value="xtls-rprx-vision">xtls-rprx-vision</option>
              </Select>
            )}
          </Field>
        )}

        {test && (
          <div className={`rounded-2xl p-4 ${test.ok ? "bg-pista-100 text-pista-700" : "bg-anar-100 text-anar-700"}`}>
            <p className="font-bold">{test.ok ? t("test_ok") : t("test_failed")}</p>
            <p className="mt-1 text-[0.85rem]" dir="auto">
              {test.ok
                ? [test.version && `v${test.version}`, test.users !== null && test.users !== undefined && t("users_n", { n: test.users })]
                    .filter(Boolean)
                    .join(" — ")
                : test.error}
            </p>
          </div>
        )}
        {test?.ok && test.options && test.options.length > 0 && (
          <OptionPicker kind={form.kind} options={test.options} value={form.options} onChange={(options) => set("options", options)} />
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("location")}>
            {(id) => (
              <Select id={id} value={form.location_id ?? ""} onChange={(e) => set("location_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">—</option>
                {locations.map((loc) => (
                  <option key={loc.id} value={loc.id}>
                    {loc.title}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("panel_capacity")} hint={t("panel_capacity_hint")}>
            {(id) => <NumberInput id={id} value={form.capacity} onChange={(v) => set("capacity", v && v > 0 ? v : null)} allowEmpty />}
          </Field>
          <Field label={t("username_method")}>
            {(id) => (
              <Select id={id} value={form.username_method} onChange={(e) => set("username_method", e.target.value)}>
                {USERNAME_METHODS.map((method) => (
                  <option key={method} value={method}>
                    {t(`username_method_${method}`)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("username_prefix")} hint={t("username_prefix_hint")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                maxLength={16}
                value={form.username_prefix}
                onChange={(e) => set("username_prefix", e.target.value.replace(/[^A-Za-z0-9_]/g, ""))}
              />
            )}
          </Field>
        </div>
        <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("panel_active")} hint={t("panel_active_hint")} />
        {owner && (
          <Toggle
            checked={form.allow_private_address}
            onChange={(v) => set("allow_private_address", v)}
            label={t("panel_private")}
            hint={t("panel_private_hint")}
          />
        )}

        {form.id && owner && (
          <section className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
            <h3 className="font-bold">{t("tls_title")}</h3>
            <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("tls_hint")}</p>
            <div className="flex flex-wrap gap-2">
              {panel?.tls_pinned ? (
                <>
                  <Badge tone="health">{t("cert_pinned")}</Badge>
                  <Button small busy={busy} onClick={() => void pin(null)}>
                    {t("cert_unpin")}
                  </Button>
                </>
              ) : (
                <Button small busy={busy} onClick={() => void inspect()}>
                  {t("cert_inspect")}
                </Button>
              )}
            </div>
            {cert && (
              <div className="flex flex-col gap-2 rounded-xl bg-[var(--bg)] p-3">
                <p className="text-[0.8rem] text-[var(--hint)]">{t("cert_fingerprint")}</p>
                <p className="break-all font-mono text-[0.78rem]" dir="ltr">
                  {cert.fingerprint}
                </p>
                <p className="text-[0.8rem] text-[var(--hint)]">{t("cert_compare_hint")}</p>
                <Button small tone="primary" busy={busy} onClick={() => void pin(cert.pem)}>
                  {t("cert_trust")}
                </Button>
              </div>
            )}
          </section>
        )}

        {form.id && owner && form.kind !== "xui" && (
          <section className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
            <h3 className="font-bold">{t("webhook_title")}</h3>
            <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("webhook_hint")}</p>
            {webhook ? (
              <div className="flex flex-col gap-2 rounded-xl bg-saffron-100 p-3 text-saffron-700">
                <p className="text-[0.82rem] font-semibold">{t("webhook_shown_once")}</p>
                <p className="text-[0.78rem]">{t("webhook_url")}</p>
                <CopyText text={webhook.url} />
                <p className="text-[0.78rem]">{t("webhook_secret")}</p>
                <CopyText text={webhook.secret} />
              </div>
            ) : (
              <Button small busy={busy} onClick={() => void rotateWebhook()}>
                {panel?.webhook_enabled ? t("webhook_rotate") : t("webhook_enable")}
              </Button>
            )}
          </section>
        )}
      </div>
    </Sheet>
  );
}

interface Node {
  id: number;
  name?: string;
  address?: string;
  status?: string;
  xray_version?: string;
  message?: string | null;
}

function NodesSheet({ panel, onClose }: { panel: PanelRow; onClose: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ data: Node[] | { nodes?: Node[] } }>(
    `/api/admin/panels/${panel.id}/pasarguard/nodes`,
  );
  const [busy, run] = useAction();
  const nodes: Node[] = Array.isArray(data?.data) ? data.data : (data?.data as { nodes?: Node[] } | undefined)?.nodes ?? [];
  const act = async (node: Node, action: "reconnect" | "sync") => {
    try {
      await run(() => call(`/api/admin/panels/${panel.id}/pasarguard/nodes/${node.id}/${action}`, { method: "POST" }));
      toast(t(`node_${action}_done`));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet open onClose={onClose} title={t("nodes_of", { name: panel.name })} wide>
      <Async data={data} error={error} reload={reload}>
        {() =>
          nodes.length === 0 ? (
            <Empty title={t("no_nodes")} />
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {nodes.map((node) => (
                <li key={node.id} className="flex flex-wrap items-center gap-3 py-3">
                  <span
                    className={`h-2.5 w-2.5 rounded-full ${node.status === "connected" ? "bg-pista-500" : "bg-anar-500"}`}
                  />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="font-semibold">{node.name ?? `#${node.id}`}</span>
                    <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                      {[node.address, node.status, node.xray_version].filter(Boolean).join(" ")}
                    </span>
                    {node.message && <span className="text-[0.78rem] text-anar-700" dir="auto">{node.message}</span>}
                  </span>
                  {can("panels.write") && (
                    <span className="flex gap-2">
                      <Button small busy={busy} onClick={() => void act(node, "sync")}>
                        {t("node_sync")}
                      </Button>
                      <Button small busy={busy} onClick={() => void act(node, "reconnect")}>
                        {t("node_reconnect")}
                      </Button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )
        }
      </Async>
    </Sheet>
  );
}

export function Panels() {
  const { t, can, locale } = useSession();
  const panels = useLoad<{ items: PanelRow[] }>("/api/admin/panels");
  const catalog = useLoad<CatalogData>(can("catalog.read") ? "/api/admin/catalog" : null);
  const [editing, setEditing] = useState<PanelRow | "new" | null>(null);
  const [nodes, setNodes] = useState<PanelRow | null>(null);
  const writable = can("panels.write");

  const upsert = (saved: PanelRow) =>
    panels.setData((prev) => {
      if (!prev) return prev;
      const exists = prev.items.some((p) => p.id === saved.id);
      const items = exists
        ? prev.items.map((p) => (p.id === saved.id ? { ...p, ...saved } : p))
        : [...prev.items, saved];
      return { items };
    });

  return (
    <Page
      title={t("nav_panels")}
      actions={
        writable && (
          <Button tone="primary" icon="plus" onClick={() => setEditing("new")}>
            {t("new_panel")}
          </Button>
        )
      }
    >
      <Async data={panels.data} error={panels.error} reload={panels.reload}>
        {(data) =>
          data.items.length === 0 ? (
            <Section>
              <Empty title={t("no_panels")} hint={t("no_panels_hint")} />
            </Section>
          ) : (
            <div className="grid gap-4 md:grid-cols-2">
              {data.items.map((panel) => (
                <Section
                  key={panel.id}
                  title={panel.name}
                  actions={
                    <>
                      {panel.kind === "pasarguard" && (
                        <Button small tone="ghost" onClick={() => setNodes(panel)}>
                          {t("nodes")}
                        </Button>
                      )}
                      {writable && (
                        <Button small onClick={() => setEditing(panel)}>
                          {t("edit")}
                        </Button>
                      )}
                    </>
                  }
                >
                  <div className="mb-3 flex flex-wrap gap-2">
                    <Badge tone={!panel.is_active ? "neutral" : panel.is_healthy ? "health" : "loss"}>
                      {!panel.is_active ? t("panel_off") : panel.is_healthy ? t("panel_ok") : t("panel_down")}
                    </Badge>
                    <Badge>{t(`panel_kind_${panel.kind}`)}</Badge>
                    {panel.tls_pinned && <Badge tone="brand">{t("cert_pinned")}</Badge>}
                    {panel.webhook_enabled && <Badge tone="brand">{t("webhook_on")}</Badge>}
                  </div>
                  <Facts
                    items={[
                      [t("panel_url"), <span key="u" className="break-all text-[0.8rem]" dir="ltr">{panel.base_url}</span>],
                      [
                        t("panel_load"),
                        panel.capacity
                          ? `${money(panel.load ?? 0, locale)} / ${money(panel.capacity, locale)}`
                          : money(panel.load ?? 0, locale),
                      ],
                      [t("health_checked"), <When key="h" iso={panel.health_checked_at} time />],
                    ]}
                  />
                  {panel.health_error && (
                    <p className="mt-3 rounded-xl bg-anar-100 px-3 py-2 text-[0.8rem] text-anar-700" dir="auto">
                      {panel.health_error}
                    </p>
                  )}
                </Section>
              ))}
            </div>
          )
        }
      </Async>
      {editing && (
        <PanelSheet
          panel={editing === "new" ? undefined : editing}
          locations={catalog.data?.locations ?? []}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            upsert(saved);
            if (editing === "new") setEditing(saved);
          }}
        />
      )}
      {nodes && <NodesSheet panel={nodes} onClose={() => setNodes(null)} />}
    </Page>
  );
}
