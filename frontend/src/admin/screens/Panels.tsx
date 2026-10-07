import { useState } from "react";
import { money, size } from "@shared/format";
import { call, type CatalogData, type PanelRow } from "../api";
import { useAction, useLoad, useSelection } from "../hooks";
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
  Status,
  Table,
  Toggle,
  Usage,
  When,
  useConfirm,
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

// Panels that can be reached with an API token alone.
const TOKEN_KINDS = ["remnawave", "rebecca", "guard", "hiddify", "wgdashboard", "wgmate"];

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
  if (kind === "mikrotik") {
    return (
      <Field label={t("panel_um_profile")} hint={t("panel_um_profile_hint")}>
        {(id) => (
          <Select id={id} value={String(value.profile ?? "")} onChange={(e) => onChange({ ...value, profile: e.target.value || undefined })}>
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
  if (kind === "ibsng") {
    return (
      <Field label={t("panel_ibsng_group")} hint={t("panel_ibsng_group_hint")}>
        {(id) => (
          <Input id={id} dir="ltr" value={String(value.group ?? "")} maxLength={64} onChange={(e) => onChange({ ...value, group: e.target.value || undefined })} />
        )}
      </Field>
    );
  }
  if (kind === "wgdashboard" || kind === "wgmate" || kind === "hiddify") return null;
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
  const services = kind === "marzneshin" || kind === "rebecca" || kind === "guard";
  const numeric = kind === "pasarguard" || services;
  const key = kind === "pasarguard" ? "group_ids" : services ? "service_ids" : kind === "remnawave" ? "squad_ids" : "inbounds";
  const selected = new Set((value[key] as (string | number)[] | undefined)?.map(String) ?? []);
  const toggle = (id: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    const list = [...next];
    onChange({ ...value, [key]: numeric ? list.map(Number) : list });
  };
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-[0.82rem] font-semibold">
        {kind === "pasarguard"
          ? t("panel_groups")
          : services
            ? t("panel_services")
            : kind === "remnawave"
              ? t("panel_squads")
              : t("panel_inbounds")}
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
  const tokenOnly = TOKEN_KINDS.includes(form.kind) && (form.api_token.trim() || panel?.has_api_token);
  const canSave =
    form.name.trim() && form.base_url.startsWith("http") && (tokenOnly || (form.username.trim() && (form.id || form.password)));

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
                <option value="marzneshin">Marzneshin</option>
                <option value="remnawave">Remnawave</option>
                <option value="rebecca">Rebecca</option>
                <option value="guard">Guard</option>
                <option value="hiddify">Hiddify</option>
                <option value="xui">3x-ui (Sanaei)</option>
                <option value="wgdashboard">WGDashboard (WireGuard)</option>
                <option value="wgmate">WG-Mate</option>
                <option value="mikrotik">MikroTik User Manager</option>
                <option value="ibsng">IBSng</option>
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
        {form.kind === "hiddify" && (
          <Field label={t("panel_sub_base")} hint={t("panel_sub_base_hint_hiddify")}>
            {(id) => (
              <Input id={id} dir="ltr" value={String(form.options.sub_base_url ?? "")} onChange={(e) => setOption("sub_base_url", e.target.value)} />
            )}
          </Field>
        )}
        {form.kind === "wgdashboard" && (
          <Field label={t("panel_wg_configuration")} hint={t("panel_wg_configuration_hint")}>
            {(id) => (
              <Input id={id} dir="ltr" value={String(form.options.configuration ?? "")} placeholder="wg0" maxLength={32} onChange={(e) => setOption("configuration", e.target.value)} />
            )}
          </Field>
        )}
        {form.kind === "ibsng" && (
          <Field label={t("panel_ibsng_group")} hint={t("panel_ibsng_group_hint")}>
            {(id) => (
              <Input id={id} dir="ltr" value={String(form.options.group ?? "")} maxLength={64} onChange={(e) => setOption("group", e.target.value)} />
            )}
          </Field>
        )}
        {form.kind === "wgmate" && (
          <Field label={t("panel_wgmate_template")} hint={t("panel_wgmate_template_hint")}>
            {(id) => (
              <Input id={id} dir="ltr" value={String(form.options.template_user ?? "")} maxLength={64} onChange={(e) => setOption("template_user", e.target.value)} />
            )}
          </Field>
        )}
        {form.kind === "guard" && (
          <Field label={t("panel_guard_version")}>
            {(id) => (
              <Select id={id} value={String(form.options.guard_version ?? "v1")} onChange={(e) => setOption("guard_version", e.target.value)}>
                <option value="v1">v1</option>
                <option value="v2">v2</option>
              </Select>
            )}
          </Field>
        )}
        {TOKEN_KINDS.includes(form.kind) && (
          <Field label={t("panel_api_token")} hint={t(`panel_api_token_hint_${form.kind}`)}>
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
        )}
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
        {["marzban", "marzneshin", "xui", "rebecca"].includes(form.kind) && (
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
        <section className="flex flex-col gap-3 border-t border-[var(--line)] pt-4">
          <h3 className="font-bold">{t("panel_sales_title")}</h3>
          <Toggle
            checked={form.options.sell_extra_volume !== false}
            onChange={(v) => setOption("sell_extra_volume", v ? undefined : false)}
            label={t("panel_sell_extra_volume")}
          />
          <Toggle
            checked={form.options.sell_extra_days !== false}
            onChange={(v) => setOption("sell_extra_days", v ? undefined : false)}
            label={t("panel_sell_extra_days")}
          />
          <Field label={t("panel_renew_mode")} hint={t("panel_renew_mode_hint")}>
            {(id) => (
              <Select
                id={id}
                value={form.options.renew_resets_usage === undefined ? "" : String(form.options.renew_resets_usage)}
                onChange={(e) => setOption("renew_resets_usage", e.target.value === "" ? undefined : e.target.value === "true")}
              >
                <option value="">{t("panel_renew_mode_default")}</option>
                <option value="true">{t("panel_renew_mode_reset")}</option>
                <option value="false">{t("panel_renew_mode_add")}</option>
              </Select>
            )}
          </Field>
        </section>
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

        {form.id && owner && ["pasarguard", "marzban"].includes(form.kind) && (
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

interface ImportRow {
  id: number;
  username: string;
  status: string;
  used_bytes: number;
  data_limit: number;
  expire_at: string | null;
  telegram_id: number | null;
  known_customer: boolean;
}

function TransferSheet({ panel, onClose }: { panel: PanelRow; onClose: () => void }) {
  const { t, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const admins = useLoad<{ admins: { username: string; users: number }[]; total: number }>(
    `/api/admin/panels/${panel.id}/admins`,
  );
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [busy, run] = useAction();
  const count = admins.data?.admins.find((a) => a.username === from)?.users ?? 0;
  const submit = async () => {
    const ok = await confirm({
      title: t("transfer_confirm_title"),
      body: t("transfer_confirm_body", { n: money(count, locale), from, to }),
      confirm: t("transfer_start"),
    });
    if (!ok) return;
    try {
      const result = await run(() =>
        call<{ queued: number }>(`/api/admin/panels/${panel.id}/transfer`, {
          method: "POST",
          body: { from_admin: from, to_admin: to },
        }),
      );
      toast(t("transfer_queued", { n: money(result?.queued ?? 0, locale) }));
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={t("transfer_title", { panel: panel.name })}
      footer={
        <Button tone="primary" busy={busy} disabled={!from || !to || from === to || !count} onClick={() => void submit()}>
          {t("transfer_start")}
        </Button>
      }
    >
      <Async {...admins}>
        {(data) => (
          <div className="flex flex-col gap-4">
            <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("transfer_intro")}</p>
            <ul className="divide-y divide-[var(--line)] rounded-xl ring-1 ring-[var(--line)]">
              {data.admins.map((admin) => (
                <li key={admin.username} className="flex items-center justify-between px-3 py-2 text-[0.85rem]">
                  <span dir="ltr">{admin.username}</span>
                  <span className="text-[var(--hint)]">{t("gauge_users", { n: money(admin.users, locale) })}</span>
                </li>
              ))}
            </ul>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("transfer_from")}>
                {(id) => (
                  <Select id={id} value={from} onChange={(e) => setFrom(e.target.value)}>
                    <option value="">—</option>
                    {data.admins.filter((a) => a.users > 0).map((a) => (
                      <option key={a.username} value={a.username}>
                        {a.username}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              <Field label={t("transfer_to")}>
                {(id) => (
                  <Select id={id} value={to} onChange={(e) => setTo(e.target.value)}>
                    <option value="">—</option>
                    {data.admins.filter((a) => a.username !== from).map((a) => (
                      <option key={a.username} value={a.username}>
                        {a.username}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            </div>
          </div>
        )}
      </Async>
    </Sheet>
  );
}

function ImportSheet({ panel, onClose }: { panel: PanelRow; onClose: () => void }) {
  const { t, can, toast, errorText } = useSession();
  const preview = useLoad<{ total: number; items: Omit<ImportRow, "id">[] }>(`/api/admin/panels/${panel.id}/import`);
  const resellers = useLoad<{ profiles: { customer_id: number; customer: string }[] }>(
    can("resellers.manage") ? "/api/admin/resellers" : null,
  );
  const selection = useSelection(`import-${panel.id}`);
  const [matchTelegram, setMatchTelegram] = useState(true);
  const [owner, setOwner] = useState<number | null>(null);
  const [busy, run] = useAction();
  const rows: ImportRow[] = (preview.data?.items ?? []).map((row, index) => ({ ...row, id: index + 1 }));
  const everything = selection.selected.size === 0;
  const submit = async () => {
    const usernames = everything ? null : rows.filter((r) => selection.selected.has(r.id)).map((r) => r.username);
    try {
      await run(() =>
        call(`/api/admin/panels/${panel.id}/import`, {
          method: "POST",
          body: { usernames, customer_id: owner, match_telegram: matchTelegram },
        }),
      );
      toast(t("import_queued"));
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      wide
      title={t("import_users_title", { panel: panel.name })}
      footer={
        <Button tone="primary" busy={busy} disabled={!rows.length || (!matchTelegram && owner === null)} onClick={() => void submit()}>
          {everything
            ? t("import_all", { n: preview.data?.total ?? 0 })
            : t("import_selected", { n: selection.selected.size })}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-[0.85rem] text-[var(--hint)]">{t("import_intro")}</p>
        <Toggle checked={matchTelegram} onChange={setMatchTelegram} label={t("import_match_telegram")} hint={t("import_match_telegram_hint")} />
        <Field label={t("import_owner")} hint={t(matchTelegram ? "import_owner_hint_fallback" : "import_owner_hint")}>
          {(id) =>
            resellers.data ? (
              <Select id={id} value={owner ?? ""} onChange={(e) => setOwner(e.target.value ? Number(e.target.value) : null)}>
                <option value="">{matchTelegram ? t("import_leave_unmatched") : "—"}</option>
                {resellers.data.profiles.map((p) => (
                  <option key={p.customer_id} value={p.customer_id}>
                    {p.customer}
                  </option>
                ))}
              </Select>
            ) : (
              <NumberInput id={id} allowEmpty placeholder={t("customer_number")} value={owner} onChange={setOwner} />
            )
          }
        </Field>
        <Async {...preview}>
          {(data) => (
            <>
              <p className="text-[0.82rem] text-[var(--hint)]">
                {t("import_found", { n: data.total })}
                {data.total > data.items.length && ` ${t("import_shown", { n: data.items.length })}`}
              </p>
              <Table
                rows={rows}
                selection={selection}
                empty={<Empty title={t("import_nothing")} hint={t("import_nothing_hint")} />}
                columns={[
                  { key: "user", label: t("panel_username"), mobile: "title", render: (r) => <span dir="ltr">{r.username}</span> },
                  {
                    key: "tg",
                    label: t("telegram_id"),
                    mobile: "meta",
                    render: (r) =>
                      r.telegram_id ? (
                        <span className="num">
                          {r.telegram_id}
                          {r.known_customer && <Badge tone="brand">{t("known_customer")}</Badge>}
                        </span>
                      ) : (
                        "—"
                      ),
                  },
                  { key: "usage", label: t("col_usage"), render: (r) => <Usage used={r.used_bytes} limit={r.data_limit} /> },
                  { key: "status", label: t("col_status"), mobile: "end", render: (r) => <Status value={r.status} /> },
                ]}
              />
            </>
          )}
        </Async>
      </div>
    </Sheet>
  );
}

function Meter({ label, value, max, text }: { label: string; value: number; max: number; text: string }) {
  const ratio = max > 0 ? Math.min(1, value / max) : 0;
  const tone = ratio >= 0.9 ? "bg-anar-500" : ratio >= 0.75 ? "bg-saffron-500" : "bg-firouzeh-500";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between text-[0.78rem]">
        <span className="text-[var(--hint)]">{label}</span>
        <span className="num font-semibold">{text}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-[var(--line)]" role="meter" aria-label={label} aria-valuenow={Math.round(ratio * 100)} aria-valuemin={0} aria-valuemax={100}>
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.round(ratio * 100)}%` }} />
      </div>
    </div>
  );
}

function PanelGauges({ panel }: { panel: PanelRow }) {
  const { t, locale } = useSession();
  const s = panel.stats ?? {};
  const units = t("size_units").split(",");
  const bytes = (v: number | null | undefined) => size(v ?? 0, locale, units);
  const admin = s.admin;
  const nodes = s.nodes ?? [];
  const online = nodes.filter((n) => ["connected", "online", "healthy"].includes(n.status.toLowerCase())).length;
  if (!panel.stats_at) return null;
  return (
    <div className="mb-3 flex flex-col gap-2.5 rounded-xl bg-[var(--bg)] p-3">
      {s.cpu_percent != null && (
        <Meter label={t("gauge_cpu")} value={s.cpu_percent} max={100} text={s.cpu_cores ? t("gauge_cpu_value", { percent: money(s.cpu_percent, locale), cores: money(s.cpu_cores, locale) }) : `${money(s.cpu_percent, locale)}٪`} />
      )}
      {s.mem_total ? <Meter label={t("gauge_ram")} value={s.mem_used ?? 0} max={s.mem_total} text={`${bytes(s.mem_used)} / ${bytes(s.mem_total)}`} /> : null}
      {admin && admin.max_users ? (
        <Meter label={t("gauge_admin_users")} value={admin.users ?? 0} max={admin.max_users} text={`${money(admin.users ?? 0, locale)} / ${money(admin.max_users, locale)}`} />
      ) : null}
      {admin && admin.data_limit ? (
        <Meter label={t("gauge_admin_traffic")} value={admin.used_traffic ?? 0} max={admin.data_limit} text={`${bytes(admin.used_traffic)} / ${bytes(admin.data_limit)}`} />
      ) : null}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.78rem] text-[var(--hint)]">
        {s.users_total != null && <span>{t("gauge_users", { n: money(s.users_total, locale) })}</span>}
        {s.users_online != null && <span>{t("gauge_online", { n: money(s.users_online, locale) })}</span>}
        {s.traffic_in != null && (
          <span>{t("gauge_traffic", { down: bytes(s.traffic_in), up: bytes(s.traffic_out) })}</span>
        )}
        {nodes.length > 0 && <span>{t("gauge_nodes", { online: money(online, locale), total: money(nodes.length, locale) })}</span>}
        {admin && <span>{admin.owner ? t("panel_admin_owner") : t("panel_admin_limited", { role: admin.role ?? "—" })}</span>}
        {s.version && <span dir="ltr">v{s.version}</span>}
      </div>
    </div>
  );
}

export function Panels() {
  const { t, can, locale, toast, errorText } = useSession();
  const panels = useLoad<{ items: PanelRow[] }>("/api/admin/panels");
  const catalog = useLoad<CatalogData>(can("catalog.read") ? "/api/admin/catalog" : null);
  const [editing, setEditing] = useState<PanelRow | "new" | null>(null);
  const [nodes, setNodes] = useState<PanelRow | null>(null);
  const [importing, setImporting] = useState<PanelRow | null>(null);
  const [transferring, setTransferring] = useState<PanelRow | null>(null);
  const [checking, setChecking] = useState<number | null>(null);
  const confirm = useConfirm();
  const checkNow = async (panel: PanelRow) => {
    setChecking(panel.id);
    try {
      const fresh = await call<PanelRow & { check: { ok: boolean; error: string | null } }>(`/api/admin/panels/${panel.id}/check`, { method: "POST" });
      if (fresh) upsert(fresh);
      if (fresh) toast(fresh.check.ok ? t("check_ok") : t("check_failed", { error: fresh.check.error ?? "" }), fresh.check.ok ? "ok" : "error");
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setChecking(null);
    }
  };
  const restart = async (panel: PanelRow) => {
    const ok = await confirm({
      title: panel.kind === "pasarguard" ? t("reconnect_nodes") : t("restart_core"),
      body: t("restart_confirm"),
      confirm: t("confirm"),
    });
    if (!ok) return;
    setChecking(-panel.id);
    try {
      await call(`/api/admin/panels/${panel.id}/restart`, { method: "POST" });
      toast(t("restart_sent"));
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setChecking(null);
    }
  };
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
                        <Button small tone="ghost" onClick={() => setImporting(panel)}>
                          {t("import_users")}
                        </Button>
                      )}
                      {writable && ["pasarguard", "marzban", "marzneshin"].includes(panel.kind) && (
                        <Button small tone="ghost" onClick={() => setTransferring(panel)}>
                          {t("transfer_users")}
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
                  <PanelGauges panel={panel} />
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
                      [
                        t("uptime"),
                        panel.uptime?.day == null
                          ? "—"
                          : t("uptime_value", { day: money(panel.uptime.day, locale), week: money(panel.uptime.week ?? 0, locale) }),
                      ],
                    ]}
                  />
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button small tone="ghost" icon="refresh" busy={checking === panel.id} onClick={() => void checkNow(panel)}>
                      {t("check_now")}
                    </Button>
                    {writable && (panel.kind === "marzban" || panel.kind === "pasarguard") && (
                      <Button small tone="ghost" busy={checking === -panel.id} onClick={() => void restart(panel)}>
                        {panel.kind === "pasarguard" ? t("reconnect_nodes") : t("restart_core")}
                      </Button>
                    )}
                  </div>
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
      {importing && <ImportSheet panel={importing} onClose={() => setImporting(null)} />}
      {transferring && <TransferSheet panel={transferring} onClose={() => setTransferring(null)} />}
    </Page>
  );
}
