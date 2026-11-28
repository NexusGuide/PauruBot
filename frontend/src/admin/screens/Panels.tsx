import { useEffect, useState } from "react";
import { CapabilitySheet } from "../capabilities/CapabilitySheet";
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
  Field,
  Icon,
  Input,
  MoreSettings,
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
  options_error?: string | null;
}

// Panels that can be reached with an API token alone.
const TOKEN_KINDS = ["remnawave", "rebecca", "guard", "hiddify", "wgdashboard", "wgmate"];

export function OptionPicker({
  kind,
  options,
  value,
  onChange,
  mode = "default",
}: {
  kind: string;
  options: { id: string; title: string }[];
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  mode?: "default" | "trial";
}) {
  const { t } = useSession();
  const isTrial = mode === "trial";
  if (kind === "mikrotik") {
    if (isTrial) return null;
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
    if (isTrial) return null;
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
    const fieldKey = isTrial ? "trial_inbound_id" : "inbound_id";
    return (
      <Field
        label={t(isTrial ? "panel_trial_inbound" : "panel_inbound")}
        hint={t(isTrial ? "panel_trial_inbound_hint" : "panel_inbound_hint")}
      >
        {(id) => (
          <Select
            id={id}
            value={String(value[fieldKey] ?? "")}
            onChange={(e) => onChange({ ...value, [fieldKey]: e.target.value ? Number(e.target.value) : undefined })}
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
  const baseKey = kind === "pasarguard" ? "group_ids" : services ? "service_ids" : kind === "remnawave" ? "squad_ids" : "inbounds";
  const key = isTrial ? `trial_${baseKey}` : baseKey;
  const selected = new Set((value[key] as (string | number)[] | undefined)?.map(String) ?? []);
  const toggle = (id: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    const list = [...next];
    const updated = { ...value };
    if (isTrial && list.length === 0) {
      delete updated[key];
    } else {
      updated[key] = numeric ? list.map(Number) : list;
    }
    onChange(updated);
  };
  const legendKey =
    kind === "pasarguard"
      ? isTrial ? "panel_trial_groups" : "panel_groups"
      : services
        ? isTrial ? "panel_trial_services" : "panel_services"
        : kind === "remnawave"
          ? isTrial ? "panel_trial_squads" : "panel_squads"
          : isTrial ? "panel_trial_inbounds" : "panel_inbounds";
  return (
    <fieldset className={`flex flex-col gap-2 ${isTrial ? "rounded-2xl bg-[var(--bg)] p-3.5 ring-1 ring-[var(--line)]" : ""}`}>
      <legend className="mb-1 text-[0.82rem] font-semibold">{t(legendKey)}</legend>
      {options.map((option) => (
        <label key={option.id} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox label={option.title} checked={selected.has(option.id)} onChange={(on) => toggle(option.id, on)} />
          <span dir="ltr">{option.title}</span>
        </label>
      ))}
      <p className="text-[0.78rem] text-[var(--hint)]">{t(isTrial ? "panel_trial_options_hint" : "panel_options_hint")}</p>
    </fieldset>
  );
}

// PasarGuard group IDs typed by hand, for an admin that may not list groups (groups.read).
function GroupIds({
  value,
  onChange,
  fieldKey = "group_ids",
}: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  fieldKey?: "group_ids" | "trial_group_ids";
}) {
  const { t } = useSession();
  const isTrial = fieldKey === "trial_group_ids";
  const current = ((value[fieldKey] as (string | number)[] | undefined) ?? []).join(", ");
  const [text, setText] = useState(current);
  return (
    <Field
      label={t(isTrial ? "panel_trial_group_ids" : "panel_group_ids")}
      hint={t(isTrial ? "panel_trial_group_ids_hint" : "panel_group_ids_hint")}
    >
      {(id) => (
        <Input
          id={id}
          dir="ltr"
          inputMode="numeric"
          placeholder="1, 2"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            const ids = [...new Set(e.target.value.split(/[\s,،]+/).filter((part) => /^\d+$/.test(part)).map(Number))];
            const next = { ...value };
            if (ids.length) next[fieldKey] = ids;
            else delete next[fieldKey];
            onChange(next);
          }}
        />
      )}
    </Field>
  );
}

const PANEL_KINDS: [string, string][] = [
  ["pasarguard", "PasarGuard"],
  ["marzban", "Marzban"],
  ["marzneshin", "Marzneshin"],
  ["remnawave", "Remnawave"],
  ["rebecca", "Rebecca"],
  ["guard", "Guard"],
  ["hiddify", "Hiddify"],
  ["xui", "3x-ui (Sanaei)"],
  ["wgdashboard", "WGDashboard (WireGuard)"],
  ["wgmate", "WG-Mate"],
  ["mikrotik", "MikroTik User Manager"],
  ["ibsng", "IBSng"],
];

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
  const [liveOptions, setLiveOptions] = useState<{ id: string; title: string }[]>([]);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [loadingOptions, runOptions] = useAction();
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

  const loadOptions = async () => {
    if (!form.id) return;
    try {
      const result = await runOptions(() =>
        call<{ ok: boolean; options?: { id: string; title: string }[]; error?: string }>(
          `/api/admin/panels/${form.id}/options`,
        ),
      );
      setLiveOptions(result?.options ?? []);
      setOptionsError(result && !result.ok ? (result.error ?? t("panel_options_load_failed")) : null);
    } catch (err) {
      setLiveOptions([]);
      setOptionsError(errorText(err));
    }
  };

  useEffect(() => {
    if (panel?.id) void loadOptions();
    // Load the live groups/inbounds once so the trial picker shows saved values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panel?.id]);

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
  const panelOptions = test?.options?.length ? test.options : liveOptions;
  const supportsTrialOptions = !["mikrotik", "ibsng", "wgdashboard", "wgmate", "hiddify"].includes(form.kind);

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
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("panel_name")}>
            {(id) => <Input id={id} value={form.name} maxLength={64} onChange={(e) => set("name", e.target.value)} />}
          </Field>
          <Field label={t("panel_kind")}>
            {(id) => (
              <Select id={id} value={form.kind} onChange={(e) => set("kind", e.target.value)} disabled={Boolean(form.id)}>
                {PANEL_KINDS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        <Field label={t("panel_url")}>
          {(id) => (
            <Input
              id={id}
              dir="ltr"
              placeholder="https://panel.example.com"
              value={form.base_url}
              disabled={!editingSecrets}
              onChange={(e) => set("base_url", e.target.value)}
            />
          )}
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("panel_username")}>
            {(id) => <Input id={id} dir="ltr" autoComplete="off" value={form.username} onChange={(e) => set("username", e.target.value)} />}
          </Field>
          <Field label={t("password")}>
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
          <Field label={t("panel_sub_base")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                value={String(form.options.sub_base_url ?? "")}
                onChange={(e) => setOption("sub_base_url", e.target.value)}
              />
            )}
          </Field>
        )}
        {form.kind === "wgdashboard" && (
          <Field label={t("panel_wg_configuration")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                value={String(form.options.configuration ?? "")}
                placeholder="wg0"
                maxLength={32}
                onChange={(e) => setOption("configuration", e.target.value)}
              />
            )}
          </Field>
        )}
        {form.kind === "ibsng" && (
          <Field label={t("panel_ibsng_group")} hint={t("panel_ibsng_group_hint")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                value={String(form.options.group ?? "")}
                maxLength={64}
                onChange={(e) => setOption("group", e.target.value)}
              />
            )}
          </Field>
        )}
        {form.kind === "wgmate" && (
          <Field label={t("panel_wgmate_template")} hint={t("panel_wgmate_template_hint")}>
            {(id) => (
              <Input
                id={id}
                dir="ltr"
                value={String(form.options.template_user ?? "")}
                maxLength={64}
                onChange={(e) => setOption("template_user", e.target.value)}
              />
            )}
          </Field>
        )}
        {form.kind === "guard" && (
          <Field label={t("panel_guard_version")}>
            {(id) => (
              <Select
                id={id}
                value={String(form.options.guard_version ?? "v1")}
                onChange={(e) => setOption("guard_version", e.target.value)}
              >
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
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("location")}>
            {(id) => (
              <Select
                id={id}
                value={form.location_id ?? ""}
                onChange={(e) => set("location_id", e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">—</option>
                {locations.map((loc) => (
                  <option key={loc.id} value={loc.id}>
                    {loc.title}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("panel_capacity")}>
            {(id) => (
              <NumberInput
                id={id}
                value={form.capacity}
                placeholder={t("unlimited")}
                onChange={(v) => set("capacity", v && v > 0 ? v : null)}
                allowEmpty
              />
            )}
          </Field>
        </div>
        <div className="rounded-2xl bg-[var(--bg)] px-4 py-2 ring-1 ring-[var(--line)]">
          <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label={t("panel_active")} />
        </div>

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
        {test?.ok && test.options_error && (
          <p className="rounded-2xl bg-saffron-100 p-4 text-[0.85rem] text-saffron-700" dir="auto">
            {test.options_error}
          </p>
        )}

        <section className="flex flex-col gap-3 rounded-2xl bg-[var(--bg)] p-4 ring-1 ring-[var(--line)]">
          <div>
            <h4 className="text-[0.95rem] font-bold">{t("panel_trial_title")}</h4>
            <p className="mt-1 text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("panel_trial_hint")}</p>
          </div>
          <Toggle
            checked={form.options.allow_trials !== false}
            onChange={(v) => setOption("allow_trials", v ? undefined : false)}
            label={t("panel_allow_trials")}
            hint={t("panel_allow_trials_hint")}
          />
          {supportsTrialOptions && panelOptions.length > 0 && (
            <OptionPicker
              mode="trial"
              kind={form.kind}
              options={panelOptions}
              value={form.options}
              onChange={(options) => set("options", options)}
            />
          )}
          {supportsTrialOptions && panelOptions.length === 0 && form.kind === "pasarguard" && (
            <GroupIds fieldKey="trial_group_ids" value={form.options} onChange={(options) => set("options", options)} />
          )}
          {supportsTrialOptions && panelOptions.length === 0 && form.kind === "xui" && (
            <Field label={t("panel_trial_inbound")} hint={t("panel_trial_inbound_hint")}>
              {(id) => (
                <NumberInput
                  id={id}
                  value={typeof form.options.trial_inbound_id === "number" ? form.options.trial_inbound_id : null}
                  placeholder={t("panel_trial_inherit")}
                  onChange={(v) => setOption("trial_inbound_id", v && v > 0 ? v : null)}
                  allowEmpty
                />
              )}
            </Field>
          )}
          {supportsTrialOptions && panelOptions.length === 0 && form.kind !== "pasarguard" && form.kind !== "xui" && (
            <p className="text-[0.8rem] leading-relaxed text-[var(--hint)]" dir="auto">
              {optionsError ?? t("panel_trial_options_missing")}
            </p>
          )}
          {supportsTrialOptions && form.id && (
            <div className="flex flex-wrap items-center gap-2">
              <Button small busy={loadingOptions} onClick={() => void loadOptions()}>
                {t("panel_trial_load_options")}
              </Button>
              {panelOptions.length > 0 && optionsError && (
                <span className="text-[0.78rem] text-[var(--hint)]" dir="auto">
                  {optionsError}
                </span>
              )}
            </div>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label={t("panel_trial_days")}>
              {(id) => (
                <NumberInput
                  id={id}
                  value={typeof form.options.trial_days === "number" ? form.options.trial_days : null}
                  placeholder={t("panel_trial_inherit")}
                  onChange={(v) => setOption("trial_days", v && v > 0 ? Math.min(v, 3_650) : null)}
                  allowEmpty
                />
              )}
            </Field>
            <Field label={t("panel_trial_hours")}>
              {(id) => (
                <NumberInput
                  id={id}
                  value={typeof form.options.trial_hours === "number" ? form.options.trial_hours : null}
                  placeholder={t("panel_trial_inherit")}
                  onChange={(v) => setOption("trial_hours", v && v > 0 ? Math.min(v, 720) : null)}
                  allowEmpty
                />
              )}
            </Field>
            <Field label={t("panel_trial_gb")}>
              {(id) => (
                <NumberInput
                  id={id}
                  value={typeof form.options.trial_gb === "number" ? form.options.trial_gb : null}
                  placeholder={t("panel_trial_inherit")}
                  onChange={(v) => setOption("trial_gb", v && v > 0 ? Math.min(v, 102_400) : null)}
                  allowEmpty
                />
              )}
            </Field>
            <Field label={t("panel_trial_mb")}>
              {(id) => (
                <NumberInput
                  id={id}
                  value={typeof form.options.trial_mb === "number" ? form.options.trial_mb : null}
                  placeholder={t("panel_trial_inherit")}
                  onChange={(v) => setOption("trial_mb", v && v > 0 ? Math.min(v, 102_400) : null)}
                  allowEmpty
                />
              )}
            </Field>
          </div>
          <p className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{t("panel_trial_limits_hint")}</p>
          <Field label={t("panel_trial_quota")} hint={t("panel_trial_quota_hint")}>
            {(id) => (
              <NumberInput
                id={id}
                value={typeof form.options.trial_daily_quota === "number" ? form.options.trial_daily_quota : null}
                placeholder={t("unlimited")}
                onChange={(v) => setOption("trial_daily_quota", v && v > 0 ? Math.min(v, 1_000_000) : null)}
                allowEmpty
              />
            )}
          </Field>
        </section>

        <MoreSettings>
          {form.kind === "pasarguard" && (form.id || test) && (!test?.ok || !test.options?.length) && (
            <GroupIds value={form.options} onChange={(options) => set("options", options)} />
          )}
          {["marzban", "marzneshin", "xui", "rebecca"].includes(form.kind) && (
            <Field label={t("panel_flow")}>
              {(id) => (
                <Select id={id} value={String(form.options.flow ?? "")} onChange={(e) => setOption("flow", e.target.value)}>
                  <option value="">—</option>
                  <option value="xtls-rprx-vision">xtls-rprx-vision</option>
                </Select>
              )}
            </Field>
          )}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label={t("panel_renew_mode")}>
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
            <Field label={t("username_prefix")}>
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
          <div className="flex flex-col gap-1">
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
            {owner && (
              <Toggle
                checked={form.allow_private_address}
                onChange={(v) => set("allow_private_address", v)}
                label={t("panel_private")}
                hint={t("panel_private_hint")}
              />
            )}
          </div>
          {form.id && owner && (
            <section className="flex flex-col gap-3">
              <h4 className="text-[0.95rem] font-bold">{t("tls_title")}</h4>
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
          {form.id && owner && ["pasarguard", "marzban", "rebecca", "remnawave"].includes(form.kind) && (
            <section className="flex flex-col gap-3">
              <h4 className="text-[0.95rem] font-bold">{t("webhook_title")}</h4>
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
        </MoreSettings>
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
    <div className="flex flex-col gap-2.5 rounded-xl bg-[var(--bg)] p-3">
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


function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** One panel: health and name on top, three figures, its gauges, and edit / check / more. */
function PanelCard({
  panel,
  writable,
  checking,
  onEdit,
  onCheck,
  onMore,
}: {
  panel: PanelRow;
  writable: boolean;
  checking: number | null;
  onEdit: () => void;
  onCheck: () => void;
  onMore: () => void;
}) {
  const { t, locale } = useSession();
  const state = !panel.is_active ? "off" : panel.is_healthy ? "ok" : "down";
  const tint = { off: "bg-[var(--surface-2)] text-[var(--hint)]", ok: "bg-pista-100 text-pista-700", down: "bg-anar-100 text-anar-700" }[state];
  const load = panel.load ?? 0;
  const ratio = panel.capacity ? Math.min(1, load / panel.capacity) : 0;
  return (
    <article className="flex flex-col overflow-hidden rounded-[20px] bg-[var(--surface)] ring-1 ring-[var(--line)]">
      <header className="flex items-center gap-3 p-4">
        <span className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl ${tint}`}>
          <Icon name="panels" size={21} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h2 className="truncate text-[1rem] font-extrabold">{panel.name}</h2>
          <p className="flex min-w-0 items-center gap-1.5 text-[0.76rem] text-[var(--hint)]">
            <span className="shrink-0">{t(`panel_kind_${panel.kind}`)}</span>
            <span>·</span>
            <span className="truncate" dir="ltr">
              {host(panel.base_url)}
            </span>
          </p>
        </div>
        <Badge tone={state === "off" ? "neutral" : state === "ok" ? "health" : "loss"}>
          {state === "off" ? t("panel_off") : state === "ok" ? t("panel_ok") : t("panel_down")}
        </Badge>
      </header>

      <dl className="grid grid-cols-3 border-t border-[var(--line)] [&>div+div]:border-s [&>div+div]:border-[var(--line)]">
        <div className="flex flex-col items-center gap-0.5 px-2 py-3">
          <dd className="num text-[1.05rem] font-extrabold">
            {money(load, locale)}
            {panel.capacity ? <span className="text-[0.75rem] font-semibold text-[var(--hint)]"> / {money(panel.capacity, locale)}</span> : null}
          </dd>
          <dt className="text-[0.72rem] text-[var(--hint)]">{t("panel_load")}</dt>
        </div>
        <div className="flex flex-col items-center gap-0.5 px-2 py-3">
          <dd className="num text-[1.05rem] font-extrabold">{panel.uptime?.day == null ? "—" : `${money(panel.uptime.day, locale)}٪`}</dd>
          <dt className="text-[0.72rem] text-[var(--hint)]">{t("uptime")}</dt>
        </div>
        <div className="flex flex-col items-center gap-0.5 px-2 py-3">
          <dd className="text-[0.86rem] font-bold">
            <When iso={panel.health_checked_at} time />
          </dd>
          <dt className="text-[0.72rem] text-[var(--hint)]">{t("health_checked")}</dt>
        </div>
      </dl>

      {(panel.capacity || panel.stats_at || panel.tls_pinned || panel.webhook_enabled || panel.health_error) && (
      <div className="flex flex-col gap-3 border-t border-[var(--line)] p-4">
        {panel.capacity ? (
          <div className="h-1.5 overflow-hidden rounded-full bg-[var(--surface-2)]" role="meter" aria-label={t("panel_load")} aria-valuenow={Math.round(ratio * 100)} aria-valuemin={0} aria-valuemax={100}>
            <div className={`h-full rounded-full ${ratio >= 0.9 ? "bg-anar-500" : ratio >= 0.75 ? "bg-saffron-500" : "bg-pista-500"}`} style={{ width: `${Math.round(ratio * 100)}%` }} />
          </div>
        ) : null}
        <PanelGauges panel={panel} />
        {(panel.tls_pinned || panel.webhook_enabled) && (
          <div className="flex flex-wrap gap-1.5">
            {panel.tls_pinned && <Badge tone="brand">{t("cert_pinned")}</Badge>}
            {panel.webhook_enabled && <Badge tone="brand">{t("webhook_on")}</Badge>}
          </div>
        )}
        {panel.health_error && (
          <p className="rounded-xl bg-anar-100 px-3 py-2 text-[0.8rem] text-anar-700" dir="auto">
            {panel.health_error}
          </p>
        )}
      </div>
      )}

      <footer className="mt-auto flex gap-2 border-t border-[var(--line)] p-3">
        {writable && (
          <Button small tone="primary" icon="settings" onClick={onEdit}>
            {t("edit")}
          </Button>
        )}
        <Button small icon="refresh" busy={checking === panel.id} onClick={onCheck}>
          {t("check_now")}
        </Button>
        <span className="flex-1" />
        <Button small tone="ghost" icon="more" aria-label={t("cust_more_actions")} onClick={onMore}>
          {t("cust_more_actions")}
        </Button>
      </footer>
    </article>
  );
}

export function Panels() {
  const { t, can, toast, errorText } = useSession();
  const panels = useLoad<{ items: PanelRow[] }>("/api/admin/panels");
  const catalog = useLoad<CatalogData>(can("catalog.read") ? "/api/admin/catalog" : null);
  const [capabilities, setCapabilities] = useState<PanelRow | null>(null);
  const [editing, setEditing] = useState<PanelRow | "new" | null>(null);
  const [nodes, setNodes] = useState<PanelRow | null>(null);
  const [importing, setImporting] = useState<PanelRow | null>(null);
  const [transferring, setTransferring] = useState<PanelRow | null>(null);
  const [checking, setChecking] = useState<number | null>(null);
  const [actions, setActions] = useState<PanelRow | null>(null);
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
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {data.items.map((panel) => (
                <PanelCard
                  key={panel.id}
                  panel={panel}
                  writable={writable}
                  checking={checking}
                  onEdit={() => setEditing(panel)}
                  onCheck={() => void checkNow(panel)}
                  onMore={() => setActions(panel)}
                />
              ))}
            </div>
          )
        }
      </Async>
      {actions && (
        <Sheet open onClose={() => setActions(null)} title={actions.name}>
          <div className="flex flex-col gap-2 [&>button]:w-full [&>button]:justify-start">
            {actions.kind === "pasarguard" && (
              <Button icon="panels" onClick={() => { setNodes(actions); setActions(null); }}>
                {t("nodes")}
              </Button>
            )}
            {writable && (
              <Button icon="users" onClick={() => { setImporting(actions); setActions(null); }}>
                {t("import_users")}
              </Button>
            )}
            {writable && ["pasarguard", "marzban", "marzneshin"].includes(actions.kind) && (
              <Button icon="sort" onClick={() => { setTransferring(actions); setActions(null); }}>
                {t("transfer_users")}
              </Button>
            )}
            {writable && (
              <Button icon="reports" onClick={() => { setCapabilities(actions); setActions(null); }}>
                {t("cap_open")}
              </Button>
            )}
            {writable && ["marzban", "pasarguard", "xui"].includes(actions.kind) && (
              <Button icon="refresh" busy={checking === -actions.id} onClick={() => void restart(actions)}>
                {actions.kind === "pasarguard" ? t("reconnect_nodes") : t("restart_core")}
              </Button>
            )}
          </div>
        </Sheet>
      )}
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
      {capabilities && <CapabilitySheet key={capabilities.id} panel={capabilities} onClose={() => setCapabilities(null)} />}
      {nodes && <NodesSheet panel={nodes} onClose={() => setNodes(null)} />}
      {importing && <ImportSheet panel={importing} onClose={() => setImporting(null)} />}
      {transferring && <TransferSheet panel={transferring} onClose={() => setTransferring(null)} />}
    </Page>
  );
}
