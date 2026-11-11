import { PasswordRecovery } from "../password-recovery/PasswordRecovery";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { digits, money } from "@shared/format";
import { call, qs, type AuditRow, type Channels as ChannelData, type JobRow, type SalesReport, type SettingItem, type TextItem } from "../api";
import { Columns } from "../chart";
import { PAGE_SIZE, useAction, useList, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Bytes,
  Button,
  Checkbox,
  Empty,
  Facts,
  Field,
  Input,
  Money,
  NumberInput,
  Page,
  Pager,
  SearchBox,
  Section,
  Select,
  Sheet,
  Status,
  Table,
  Textarea,
  Toggle,
  Toolbar,
  When,
  type Column,
  CopyText,
  useConfirm,
} from "../ui";
import { AlertMatrix, AutoReplies, ReportSections, type AutoReply } from "../settings-editors";
import { TrialResetCard } from "./Tools";
import { AmountList, ResetTextsButton } from "./ClockTools";
import { BackupBotForm, DateTimeInput, IdPicker, PackagesEditor, SettingsFileButtons, TierEditor, VerifyBackupButton, type ExtraPackage, type Tier as BulkTier } from "./ClockTools2";
import { MethodRules, type MethodRule } from "./FoximaTools";
import { compact } from "./Home";
import { controllers, matchesQuery, shown } from "../settings-layout";
import { byFeature, FEATURES } from "../settings-features";

const FEATURE_KEYS = [...FEATURES.map((feature) => feature.key), "other"];
import { featureFor } from "../settings-nav";
import { atAnHour, everyLabel, everyOptions, summary, type BackupSchedule } from "../backup-schedule";
import { bySectionOrder, countBySection, isButton, kindOf, preview, sectionOf, SECTIONS, type PreviewNode, type Section as TextSection, type TextKind } from "../texts-layout";

// ---------------------------------------------------------------- settings

export interface CardAccount {
  number: string;
  holder: string;
  bank: string;
}

export function CardAccounts({ value, onChange }: { value: CardAccount[]; onChange: (value: CardAccount[]) => void }) {
  const { t } = useSession();
  const update = (index: number, key: keyof CardAccount, text: string) =>
    onChange(value.map((row, i) => (i === index ? { ...row, [key]: text } : row)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((row, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1.4fr_1fr_1fr_auto]">
          <Input aria-label={t("card_number")} placeholder={t("card_number")} dir="ltr" value={row.number} onChange={(e) => update(index, "number", e.target.value)} />
          <Input aria-label={t("card_holder")} placeholder={t("card_holder")} value={row.holder} onChange={(e) => update(index, "holder", e.target.value)} />
          <Input aria-label={t("card_bank")} placeholder={t("card_bank")} value={row.bank} onChange={(e) => update(index, "bank", e.target.value)} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" onClick={() => onChange([...value, { number: "", holder: "", bank: "" }])}>
        {t("add_card")}
      </Button>
    </div>
  );
}

interface BonusRule {
  provider: string;
  min_amount: number;
  percent: number;
  max_bonus: number | null;
}

const BONUS_PROVIDERS = ["*", "card", "zarinpal", "stars", "usdt_trc20", "trx", "ton", "nowpayments", "plisio", "aqayepardakht", "tetra98", "variza", "abangateway", "blupal", "tronado", "cubepay", "atlaspay"];

function BonusRules({ value, onChange }: { value: BonusRule[]; onChange: (value: BonusRule[]) => void }) {
  const { t } = useSession();
  const update = (index: number, patch: Partial<BonusRule>) => onChange(value.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((rule, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1.2fr_1fr_0.7fr_1fr_auto]">
          <Select aria-label={t("col_method")} value={rule.provider} onChange={(e) => update(index, { provider: e.target.value })}>
            {BONUS_PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p === "*" ? t("all_methods") : t(`provider_${p}`)}
              </option>
            ))}
          </Select>
          <NumberInput aria-label={t("min_amount")} placeholder={t("min_amount")} value={rule.min_amount} onChange={(v) => update(index, { min_amount: v ?? 0 })} />
          <NumberInput aria-label={t("percent")} placeholder="%" value={rule.percent} onChange={(v) => update(index, { percent: Math.min(100, v ?? 0) })} />
          <NumberInput aria-label={t("max_bonus")} placeholder={t("max_bonus")} value={rule.max_bonus} onChange={(v) => update(index, { max_bonus: v || null })} allowEmpty />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" onClick={() => onChange([...value, { provider: "*", min_amount: 0, percent: 5, max_bonus: null }])}>
        {t("add_bonus_rule")}
      </Button>
    </div>
  );
}

interface Tier {
  title: string;
  min_spent: number;
  percent: number;
}

function LoyaltyTiers({ value, onChange }: { value: Tier[]; onChange: (value: Tier[]) => void }) {
  const { t } = useSession();
  const update = (index: number, patch: Partial<Tier>) => onChange(value.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((tier, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1.2fr_1.2fr_0.6fr_auto]">
          <Input aria-label={t("tier_title")} placeholder={t("tier_title")} maxLength={32} value={tier.title}
            onChange={(e) => update(index, { title: e.target.value })} />
          <NumberInput aria-label={t("tier_min_spent")} placeholder={t("tier_min_spent")} value={tier.min_spent}
            onChange={(v) => update(index, { min_spent: v ?? 0 })} />
          <NumberInput aria-label={t("percent")} placeholder="%" value={tier.percent}
            onChange={(v) => update(index, { percent: Math.min(50, v ?? 0) })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 10}
        onClick={() => onChange([...value, { title: "", min_spent: 0, percent: 5 }])}>
        {t("add_tier")}
      </Button>
    </div>
  );
}

interface Prize {
  label: string;
  kind: "wallet" | "discount" | "nothing";
  value: number;
  weight: number;
}

function WheelPrizes({ value, onChange }: { value: Prize[]; onChange: (value: Prize[]) => void }) {
  const { t } = useSession();
  const total = value.reduce((sum, p) => sum + (p.weight || 0), 0);
  const update = (index: number, patch: Partial<Prize>) => onChange(value.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((prize, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1.2fr_1fr_1fr_0.7fr_auto]">
          <Input aria-label={t("prize_label")} placeholder={t("prize_label")} maxLength={64} value={prize.label}
            onChange={(e) => update(index, { label: e.target.value })} />
          <Select aria-label={t("prize_kind")} value={prize.kind} onChange={(e) => update(index, { kind: e.target.value as Prize["kind"] })}>
            <option value="wallet">{t("prize_kind_wallet")}</option>
            <option value="discount">{t("prize_kind_discount")}</option>
            <option value="nothing">{t("prize_kind_nothing")}</option>
          </Select>
          <NumberInput aria-label={t("prize_value")} placeholder={t(prize.kind === "discount" ? "percent" : "amount_toman")}
            value={prize.value} disabled={prize.kind === "nothing"} onChange={(v) => update(index, { value: v ?? 0 })} />
          <NumberInput aria-label={t("prize_weight")} placeholder={t("prize_weight")} value={prize.weight}
            onChange={(v) => update(index, { weight: v ?? 0 })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
          {total > 0 && (
            <p className="text-[0.75rem] text-[var(--hint)] sm:col-span-5">
              {t("prize_chance", { percent: Math.round(((prize.weight || 0) * 1000) / total) / 10 })}
            </p>
          )}
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 12}
        onClick={() => onChange([...value, { label: "", kind: "wallet", value: 5000, weight: 10 }])}>
        {t("add_prize")}
      </Button>
    </div>
  );
}

interface BusinessRule {
  keywords: string[] | string;
  reply: string;
}

function BusinessRules({ value, onChange }: { value: BusinessRule[]; onChange: (value: BusinessRule[]) => void }) {
  const { t } = useSession();
  const update = (index: number, patch: Partial<BusinessRule>) => onChange(value.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((rule, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[1fr_2fr_auto]">
          <Input aria-label={t("business_keywords")} placeholder={t("business_keywords")} maxLength={300}
            value={Array.isArray(rule.keywords) ? rule.keywords.join(", ") : rule.keywords}
            onChange={(e) => update(index, { keywords: e.target.value })} />
          <Textarea aria-label={t("business_answer")} placeholder={t("business_answer")} rows={2} maxLength={1000}
            value={rule.reply} onChange={(e) => update(index, { reply: e.target.value })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 40}
        onClick={() => onChange([...value, { keywords: "", reply: "" }])}>
        {t("business_add_rule")}
      </Button>
    </div>
  );
}

interface AppLink {
  name: string;
  url: string;
}

function AppLinks({ value, onChange }: { value: AppLink[]; onChange: (value: AppLink[]) => void }) {
  const { t } = useSession();
  const update = (index: number, patch: Partial<AppLink>) => onChange(value.map((a, i) => (i === index ? { ...a, ...patch } : a)));
  return (
    <div className="flex flex-col gap-3">
      {value.length === 0 && <p className="text-[0.82rem] text-[var(--hint)]">{t("app_links_default")}</p>}
      {value.map((app, index) => (
        <div key={index} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-[0.8fr_2fr_auto]">
          <Input aria-label={t("app_name")} placeholder={t("app_name")} maxLength={32} value={app.name}
            onChange={(e) => update(index, { name: e.target.value })} />
          <Input aria-label={t("app_url")} placeholder="hiddify://import/{url}" dir="ltr" maxLength={300} value={app.url}
            onChange={(e) => update(index, { url: e.target.value })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 12}
        onClick={() => onChange([...value, { name: "", url: "" }])}>
        {t("add_app_link")}
      </Button>
    </div>
  );
}

// Text settings with a fixed set of values: shown as a choice, labelled setting_<key>_<value>.
const CHOICES: Record<string, string[]> = {
  referral_reward_mode: ["cash", "config", "both"],
  referral_config_counts: ["purchase", "join"],
  referral_config_source: ["plan", "custom"],
  terms_when: ["entry", "purchase"],
  phone_when: ["entry", "purchase"],
};

function RefSelect({
  id,
  kind,
  value,
  onChange,
}: {
  id: string;
  kind: "panel" | "location" | "plan";
  value: unknown;
  onChange: (value: number | null) => void;
}) {
  const panels = useLoad<{ items: { id: number; name: string }[] }>(kind === "panel" ? "/api/admin/panels" : null);
  const catalog = useLoad<{
    locations: { id: number; title: string }[];
    plans: { id: number; title: string; is_active: boolean }[];
  }>(kind === "panel" ? null : "/api/admin/catalog");
  const options =
    kind === "panel"
      ? (panels.data?.items ?? []).map((p) => ({ id: p.id, title: p.name }))
      : kind === "plan"
        ? (catalog.data?.plans ?? []).filter((p) => p.is_active).map((p) => ({ id: p.id, title: p.title }))
        : (catalog.data?.locations ?? []).map((l) => ({ id: l.id, title: l.title }));
  return (
    <Select id={id} value={typeof value === "number" ? value : ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">—</option>
      {options.map((option) => (
        <option key={option.id} value={option.id}>
          {option.title}
        </option>
      ))}
    </Select>
  );
}

function PlanPicker({ value, onChange }: { value: number[]; onChange: (value: number[]) => void }) {
  const { t } = useSession();
  const catalog = useLoad<{ plans: { id: number; title: string; is_active: boolean; delivery: string }[] }>("/api/admin/catalog");
  const plans = (catalog.data?.plans ?? []).filter((plan) => plan.is_active && plan.delivery !== "stock");
  if (!plans.length) return <p className="text-[0.85rem] text-[var(--muted)]">{t("no_plans")}</p>;
  return (
    <div className="flex flex-col gap-2">
      {plans.map((plan) => (
        <label key={plan.id} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox
            label={plan.title}
            checked={value.includes(plan.id)}
            onChange={(on) => onChange(on ? [...value, plan.id] : value.filter((id) => id !== plan.id))}
          />
          {plan.title}
        </label>
      ))}
    </div>
  );
}

function SettingControl({
  item,
  value,
  onChange,
  canSecrets,
}: {
  item: SettingItem;
  value: unknown;
  onChange: (value: unknown) => void;
  canSecrets: boolean;
}) {
  const { t } = useSession();
  const label = t(`setting_${item.key}`);
  const hintKey = `setting_${item.key}_hint`;
  const hintText = t(hintKey);
  const hint = hintText === hintKey ? undefined : hintText;

  if (item.type === "bool") return <Toggle checked={Boolean(value)} onChange={onChange} label={label} hint={hint} />;
  if (item.key === "renew_mode") {
    // A list in place of typing the mode's code; "" keeps following the reset switch above.
    return (
      <Field label={label} hint={t("setting_renew_mode_pick_hint")}>
        {(id) => (
          <Select id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
            <option value="">{t("setting_renew_mode_follow")}</option>
            {["extend_reset", "extend_carry", "restart_reset", "restart_carry"].map((mode) => (
              <option key={mode} value={mode}>
                {t(`renew_mode_${mode}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "payment_method_rules") {
    const rows = Array.isArray(value) ? (value as Partial<MethodRule>[]).map((r) => ({ provider: String(r.provider ?? "card"), min: Number(r.min ?? 0), max: Number(r.max ?? 0), help: String(r.help ?? "") })) : [];
    return <Field label={label} hint={hint}>{() => <MethodRules value={rows} onChange={onChange} />}</Field>;
  }
  if (item.key === "topup_bonus_rules") {
    return (
      <Field label={label} hint={hint}>
        {() => <BonusRules value={(value as BonusRule[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "crypto_networks") {
    const list = Array.isArray(value) ? (value as string[]) : [];
    return (
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[0.82rem] font-semibold">{label}</legend>
        {["usdt_trc20", "trx", "ton"].map((network) => (
          <label key={network} className="flex items-center gap-2 text-[0.88rem]">
            <Checkbox
              label={t(`provider_${network}`)}
              checked={list.includes(network)}
              onChange={(on) => onChange(on ? [...list, network] : list.filter((n) => n !== network))}
            />
            {t(`provider_${network}`)}
          </label>
        ))}
      </fieldset>
    );
  }
  if (item.key === "miniapp_accent") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
            {["", "green", "purple", "orange", "rose", "teal"].map((accent) => (
              <option key={accent || "blue"} value={accent}>
                {t(`miniapp_accent_${accent || "blue"}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "wheel_style") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "wheel")} onChange={(e) => onChange(e.target.value)}>
            {["wheel", "dice", "slot"].map((s) => <option key={s} value={s}>{t(`wheel_style_${s}`)}</option>)}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "fx_source") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "nobitex")} onChange={(e) => onChange(e.target.value)}>
            <option value="nobitex">{t("fx_nobitex")}</option>
            <option value="manual">{t("fx_manual")}</option>
            <option value="api">{t("fx_api")}</option>
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "info_card_theme") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "midnight")} onChange={(e) => onChange(e.target.value)}>
            {["midnight", "paper", "rose", "amber"].map((theme) => (
              <option key={theme} value={theme}>
                {t(`info_card_theme_${theme}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "username_method") {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={String(value ?? "telegram_id")} onChange={(e) => onChange(e.target.value)}>
            {["telegram_id", "telegram_username", "random", "sequential", "custom_label"].map((method) => (
              <option key={method} value={method}>
                {t(`username_method_${method}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key === "business_rules") {
    return (
      <Field label={label} hint={hint}>
        {() => <BusinessRules value={(value as BusinessRule[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "business_reply") {
    return (
      <Field label={label} hint={hint}>
        {(id) => <Textarea id={id} rows={3} maxLength={1000} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />}
      </Field>
    );
  }
  if (item.key === "guest_plan_ids") {
    const chosen = Array.isArray(value) ? (value as unknown[]).map(Number).filter(Number.isFinite) : [];
    return (
      <Field label={label} hint={hint}>
        {() => <PlanPicker value={chosen} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "loyalty_tiers") {
    return (
      <Field label={label} hint={hint}>
        {() => <LoyaltyTiers value={(value as Tier[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "wheel_prizes") {
    return (
      <Field label={label} hint={hint}>
        {() => <WheelPrizes value={(value as Prize[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "bulk_buy_tiers") {
    return <Field label={label} hint={hint}>{() => <TierEditor value={Array.isArray(value) ? (value as BulkTier[]) : []} onChange={onChange} />}</Field>;
  }
  if (item.key === "extra_packages") {
    return <Field label={label} hint={hint}>{() => <PackagesEditor value={Array.isArray(value) ? (value as ExtraPackage[]) : []} onChange={onChange} />}</Field>;
  }
  if (item.key === "catalog_discount_until") {
    return <Field label={label} hint={hint}>{() => <DateTimeInput value={String(value ?? "")} onChange={onChange} />}</Field>;
  }
  if (item.key === "catalog_discount_category_ids" || item.key === "service_import_panel_ids") {
    const source = item.key === "service_import_panel_ids" ? "panels" : "categories";
    return <Field label={label} hint={hint}>{() => <IdPicker source={source} value={Array.isArray(value) ? (value as number[]) : []} onChange={onChange} />}</Field>;
  }
  if (item.key === "wallet_topup_presets") {
    return (
      <Field label={label} hint={hint}>
        {() => <AmountList value={Array.isArray(value) ? (value as number[]) : []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "alerts_muted") {
    return (
      <Field label={label} hint={hint}>
        {() => <AlertMatrix value={Array.isArray(value) ? (value as string[]) : []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "auto_replies") {
    return (
      <Field label={label} hint={hint}>
        {() => <AutoReplies value={Array.isArray(value) ? (value as AutoReply[]) : []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "daily_report_sections") {
    return (
      <Field label={label} hint={hint}>
        {() => <ReportSections value={Array.isArray(value) ? (value as string[]) : []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "app_links") {
    return (
      <Field label={label} hint={hint}>
        {() => <AppLinks value={(value as AppLink[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  const choices = CHOICES[item.key];
  if (choices) {
    return (
      <Field label={label} hint={hint}>
        {(id) => (
          <Select id={id} value={typeof value === "string" ? value : choices[0]} onChange={(e) => onChange(e.target.value)}>
            {choices.map((choice) => (
              <option key={choice} value={choice}>
                {t(`setting_${item.key}_${choice}`)}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }
  if (item.key.endsWith("_panel_id") || item.key.endsWith("_location_id") || item.key.endsWith("_plan_id")) {
    const kind = item.key.endsWith("_panel_id") ? "panel" : item.key.endsWith("_plan_id") ? "plan" : "location";
    return (
      <Field label={label} hint={hint}>
        {(id) => <RefSelect id={id} kind={kind} value={value} onChange={onChange} />}
      </Field>
    );
  }
  if (item.key === "card_accounts") {
    return (
      <Field label={label} hint={hint}>
        {() => <CardAccounts value={(value as CardAccount[]) ?? []} onChange={onChange} />}
      </Field>
    );
  }
  if (item.type === "list") {
    const list = Array.isArray(value) ? (value as unknown[]).map(String) : [];
    return (
      <Field label={label} hint={hint ?? t("comma_separated")}>
        {(id) => (
          <Input
            id={id}
            dir="ltr"
            value={list.join(", ")}
            onChange={(e) =>
              onChange(
                e.target.value
                  .split(",")
                  .map((part) => part.trim())
                  .filter(Boolean),
              )
            }
          />
        )}
      </Field>
    );
  }
  if (item.secret) {
    const set = typeof value === "object" && value !== null && "set" in value ? Boolean((value as { set: boolean }).set) : Boolean(value);
    return (
      <Field label={label} hint={canSecrets ? (set ? t("secret_set_hint") : hint) : t("secrets_owner_only")}>
        {(id) => (
          <Input
            id={id}
            type="password"
            dir="ltr"
            autoComplete="off"
            disabled={!canSecrets}
            placeholder={set ? "••••••••" : ""}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
      </Field>
    );
  }
  if (item.type === "int") {
    return (
      <Field label={label} hint={hint}>
        {(id) => <NumberInput id={id} value={typeof value === "number" ? value : null} onChange={onChange} allowEmpty />}
      </Field>
    );
  }
  return (
    <Field label={label} hint={hint}>
      {(id) => <Input id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  );
}

const WIDE_SETTINGS = ["payment_method_rules", "bulk_buy_tiers", "extra_packages", "catalog_discount_category_ids", "service_import_panel_ids", "alerts_muted", "auto_replies", "card_accounts", "topup_bonus_rules", "app_links", "loyalty_tiers", "wheel_prizes", "business_rules", "business_reply", "guest_plan_ids"];
// Edited elsewhere: the trust channel on the channels screen; appearance and resale on their own screens.
const HIDDEN_GROUPS = ["appearance", "resale"];
const HIDDEN_SETTINGS = ["trust_channel_id"];

export function Settings() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ groups: Record<string, SettingItem[]> }>("/api/admin/settings");
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState("");
  const [params] = useSearchParams();
  const [busy, run] = useAction();
  const writable = can("settings.write");
  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    try {
      await run(() => call("/api/admin/settings", { method: "PUT", body: { values: changes } }));
      setChanges({});
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page title={t(`settings_tab_${featureFor(params.get("group"), FEATURE_KEYS) ?? "general"}`)} wide>
      <Async data={data} error={error} reload={reload}>
        {(value) => {
          // Every option on the page of its feature, whatever group the server files it under.
          const groups = byFeature(
            Object.fromEntries(
              Object.entries(value.groups)
                .filter(([group]) => !HIDDEN_GROUPS.includes(group))
                .map(([group, items]) => [group, items.filter((item) => !HIDDEN_SETTINGS.includes(item.key))]),
            ),
          );
          const names = Object.keys(groups);
          // The two-level settings nav above picks the feature; general when none is asked.
          const selected = featureFor(params.get("group"), names);
          const label = (key: string) => t(`setting_${key}`);
          const hintOf = (key: string) => {
            const hint = t(`setting_${key}_hint`);
            return hint === `setting_${key}_hint` ? undefined : hint;
          };
          const shownGroups = query.trim() ? names : selected ? [selected] : [];
          const changed = Object.keys(changes);
          return (
            <div className="flex flex-col gap-6">
              <div className="flex flex-col gap-5">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="w-full md:w-96">
                    <SearchBox value={query} onChange={setQuery} placeholder={t("settings_search")} />
                  </div>
                  <SettingsFileButtons onImported={() => void reload()} />
                </div>
                <div className="flex min-w-0 flex-1 flex-col gap-5">
                  {shownGroups.map((group) => {
                    const items = groups[group] ?? [];
                    const deps = controllers(items.map((item) => item.key));
                    const valueOf = (key: string) => (key in changes ? changes[key] : items.find((item) => item.key === key)?.value);
                    const visible = items.filter((item) =>
                      query.trim() ? matchesQuery(query, label(item.key), hintOf(item.key), item.key) : shown(item.key, deps, valueOf),
                    );
                    if (query.trim() && visible.length === 0) return null;
                    return (
                      <Section key={group} title={t(`settings_tab_${group}`)}>
                        <div className="grid gap-5 md:grid-cols-2">
                          {visible.map((item) => {
                            const nested = !query.trim() && (deps[item.key]?.length ?? 0) > 0;
                            const wide = WIDE_SETTINGS.includes(item.key) || item.type === "bool";
                            return (
                              <div
                                key={item.key}
                                className={`${wide ? "md:col-span-2" : ""} ${nested ? "border-s-2 border-firouzeh-100 ps-4" : ""}`}
                              >
                                <SettingControl
                                  item={item}
                                  value={valueOf(item.key)}
                                  canSecrets={can("panels.secrets") && writable}
                                  onChange={(next) => writable && setChanges((prev) => ({ ...prev, [item.key]: next }))}
                                />
                              </div>
                            );
                          })}
                        </div>
                      </Section>
                    );
                  })}
                  {!query.trim() && selected === "trial" && <TrialResetCard />}
                  {query.trim() && shownGroups.every((group) => !(groups[group] ?? []).some((item) => matchesQuery(query, label(item.key), hintOf(item.key), item.key))) && (
                    <Empty title={t("empty_list")} />
                  )}
                </div>
              </div>

              {writable && dirty && (
                <div
                  role="status"
                  className="sticky bottom-20 z-10 flex flex-wrap items-center gap-3 rounded-2xl bg-[var(--text)] px-5 py-3 text-[var(--bg)] shadow-lg md:bottom-4"
                >
                  <span className="font-bold">{t("settings_unsaved", { count: changed.length })}</span>
                  <span className="min-w-0 flex-1 truncate text-[0.85rem] opacity-80">
                    {changed.slice(0, 3).map(label).join("، ")}
                    {changed.length > 3 ? "…" : ""}
                  </span>
                  <Button onClick={() => setChanges({})}>{t("discard")}</Button>
                  <Button tone="primary" busy={busy} onClick={() => void save()}>
                    {t("save_changes")}
                  </Button>
                </div>
              )}
            </div>
          );
        }}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- Mini App look (REF-08)

// The Mini App's appearance group also holds the bot menu's layout (edited on the bot-menu
// screen); these four are the Mini App's own look, which no screen used to offer.
export const LOOK_KEYS = ["miniapp_enabled", "miniapp_accent", "miniapp_logo", "miniapp_notice"];
// The light-theme accents from the Mini App's app.css ([data-accent=…]); "" is its default blue.
const ACCENTS: Record<string, { ink: string; soft: string }> = {
  "": { ink: "#256fb6", soft: "#e8f1fa" },
  green: { ink: "#1f7a4d", soft: "#e6f4ec" },
  purple: { ink: "#6b46c1", soft: "#f0ebfb" },
  orange: { ink: "#b45309", soft: "#fdf1e3" },
  rose: { ink: "#be2d5a", soft: "#fcebf0" },
  teal: { ink: "#0f766e", soft: "#e3f4f2" },
};

export function MiniAppLook() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ groups: Record<string, SettingItem[]> }>("/api/admin/settings");
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [busy, run] = useAction();
  const writable = can("settings.write");
  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    try {
      await run(() => call("/api/admin/settings", { method: "PUT", body: { values: changes } }));
      setChanges({});
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_appearance")}
      actions={
        writable && (
          <>
            {dirty && <Button onClick={() => setChanges({})}>{t("discard")}</Button>}
            <Button tone="primary" busy={busy} disabled={!dirty} onClick={() => void save()}>
              {t("save_changes")}
            </Button>
          </>
        )
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => {
          const items = LOOK_KEYS.map((key) => (value.groups.appearance ?? []).find((item) => item.key === key)).filter(
            (item): item is SettingItem => Boolean(item),
          );
          const valueOf = (key: string) => (key in changes ? changes[key] : items.find((item) => item.key === key)?.value);
          const accent = ACCENTS[String(valueOf("miniapp_accent") ?? "")] ?? ACCENTS[""];
          const enabled = Boolean(valueOf("miniapp_enabled"));
          const notice = String(valueOf("miniapp_notice") ?? "").trim();
          return (
            <div className="flex flex-wrap items-start gap-6">
              <div className="min-w-0 flex-[2_1_420px]">
                <Section title={t("appearance_settings")}>
                  <div className="flex flex-col gap-5">
                    {items.map((item) => (
                      <SettingControl
                        key={item.key}
                        item={item}
                        value={valueOf(item.key)}
                        canSecrets={false}
                        onChange={(next) => writable && setChanges((prev) => ({ ...prev, [item.key]: next }))}
                      />
                    ))}
                  </div>
                </Section>
              </div>
              <div className="min-w-0 flex-[1_1_280px]">
                <Section title={t("appearance_preview")}>
                  <div
                    aria-label={t("appearance_preview")}
                    className={`flex flex-col gap-3 rounded-2xl bg-[var(--bg)] p-4 ${enabled ? "" : "opacity-50"}`}
                  >
                    <div className="flex items-center gap-3">
                      {Boolean(valueOf("miniapp_logo")) && (
                        <span
                          aria-hidden="true"
                          className="grid size-10 place-items-center rounded-full font-extrabold text-white"
                          style={{ background: accent?.ink }}
                        >
                          {t("appearance_logo_letter")}
                        </span>
                      )}
                      <strong className="text-[0.95rem]">{t("appearance_shop_name")}</strong>
                    </div>
                    {notice && (
                      <p className="rounded-xl px-3 py-2 text-[0.85rem]" style={{ background: accent?.soft, color: accent?.ink }}>
                        {notice}
                      </p>
                    )}
                    <span
                      className="flex min-h-11 items-center justify-center rounded-xl text-[0.9rem] font-bold text-white"
                      style={{ background: accent?.ink }}
                    >
                      {t("appearance_sample_button")}
                    </span>
                  </div>
                  {!enabled && <p className="mt-3 text-[0.82rem] text-[var(--hint)]">{t("appearance_off")}</p>}
                </Section>
              </div>
            </div>
          );
        }}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- texts

interface TextsData {
  locales: string[];
  globals: string[];
  items: TextItem[];
}

function TextSheet({ item, locales, globals, onClose, onSaved }: { item: TextItem; locales: string[]; globals: string[]; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [locale, setLocale] = useState(locales[0] ?? "fa");
  const [value, setValue] = useState(item.overrides[locale] ?? item.defaults[locale] ?? "");
  const [caret, setCaret] = useState<number | null>(null);
  const [busy, run] = useAction();
  const insert = (name: string) => {
    const at = caret ?? value.length;
    setValue(`${value.slice(0, at)}{${name}}${value.slice(at)}`);
    setCaret(at + name.length + 2);
  };

  const pick = (next: string) => {
    setLocale(next);
    setValue(item.overrides[next] ?? item.defaults[next] ?? "");
    setCaret(null); // the cursor belonged to the other language's text
  };
  const save = async () => {
    try {
      await run(() => call(`/api/admin/texts/${locale}/${item.key}`, { method: "PUT", body: { value } }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const reset = async () => {
    try {
      await run(() => call(`/api/admin/texts/${locale}/${item.key}`, { method: "DELETE" }));
      toast(t("text_reset_done"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Sheet
      open
      onClose={onClose}
      title={item.key}
      wide
      footer={
        <>
          {item.overrides[locale] !== undefined && (
            <Button busy={busy} onClick={() => void reset()}>
              {t("text_reset")}
            </Button>
          )}
          <Button tone="primary" busy={busy} disabled={!value.trim()} onClick={() => void save()}>
            {t("save")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex gap-2">
          {locales.map((loc) => (
            <Button key={loc} small tone={loc === locale ? "primary" : "quiet"} onClick={() => pick(loc)}>
              {loc === "fa" ? "فارسی" : loc === "en" ? "English" : loc}
            </Button>
          ))}
        </div>
        <Textarea
          aria-label={item.key}
          rows={8}
          dir={locale === "fa" ? "rtl" : "ltr"}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setCaret(e.target.selectionStart);
          }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
          // Clicking a variable takes focus from the text: keep where the cursor was.
          onBlur={(e) => setCaret(e.currentTarget.selectionStart)}
        />
        <div className="flex flex-col gap-1.5">
          <p className="text-[0.8rem] font-semibold">{t("text_variables")}</p>
          <p className="flex flex-wrap gap-1.5" dir="ltr">
            {/* A text's own variable can also be a global one: list each name once. */}
            {[...new Set([...item.variables, ...globals])].map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => insert(name)}
                aria-label={t("text_insert_variable", { name })}
                className="min-h-8 rounded-md bg-[var(--bg)] px-2 font-mono text-[0.78rem] hover:bg-firouzeh-50 hover:text-firouzeh-700"
              >
                {`{${name}}`}
              </button>
            ))}
          </p>
          <p className="text-[0.78rem] text-[var(--hint)]">{t("text_html_hint")}</p>
        </div>
        <div className="flex flex-col gap-1.5">
          <p className="text-[0.8rem] font-semibold">{t("text_preview")}</p>
          <div className="rounded-2xl bg-[var(--bg)] p-4">
            <div
              className="max-w-[90%] whitespace-pre-wrap break-words rounded-2xl rounded-bl-md bg-[var(--surface)] px-3.5 py-2.5 text-[0.9rem] leading-relaxed shadow-sm"
              dir={locale === "fa" ? "rtl" : "ltr"}
            >
              <PreviewText nodes={preview(value, previewSamples(t))} />
            </div>
          </div>
        </div>
        <details className="text-[0.85rem]">
          <summary className="cursor-pointer font-semibold text-[var(--hint)]">{t("text_default")}</summary>
          <p className="mt-2 whitespace-pre-wrap rounded-xl bg-[var(--bg)] p-3" dir="auto">
            {item.defaults[locale]}
          </p>
        </details>
      </div>
    </Sheet>
  );
}

const PREVIEW_TAGS: Record<string, string> = {
  b: "font-bold",
  i: "italic",
  u: "underline",
  s: "line-through",
  code: "rounded bg-[var(--bg)] px-1 font-mono text-[0.85em]",
  pre: "block rounded bg-[var(--bg)] p-2 font-mono text-[0.85em]",
  a: "text-firouzeh-700 underline",
  spoiler: "rounded bg-[var(--line)] px-1",
  quote: "block border-s-2 border-firouzeh-500 ps-2",
  var: "rounded bg-[var(--line)] px-1 font-mono text-[0.8em]",
};

function PreviewText({ nodes }: { nodes: PreviewNode[] }) {
  return (
    <>
      {nodes.map((node, index) =>
        typeof node === "string" ? (
          <span key={index}>{node}</span>
        ) : (
          <span key={index} className={PREVIEW_TAGS[node.tag]} dir={node.tag === "var" ? "ltr" : undefined}>
            {node.tag === "var" ? `{${String(node.children[0] ?? "")}}` : <PreviewText nodes={node.children} />}
          </span>
        ),
      )}
    </>
  );
}

/** Example values for the preview; anything else stays shown as {name}. */
function previewSamples(t: (key: string) => string): Record<string, string> {
  return { name: t("text_sample_name"), shop: t("text_sample_shop"), first_name: t("text_sample_name") };
}

export function Texts() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<TextsData>("/api/admin/texts");
  const [query, setQuery] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [params, setParams] = useSearchParams();
  // Message texts and button labels are two tabs of the settings nav, like the design.
  const kind: TextKind = params.get("kind") === "buttons" ? "buttons" : "messages";
  const [open, setOpen] = useState<TextItem | null>(null);
  const asked = params.get("section");
  const section: TextSection = SECTIONS.find((name) => name === asked) ?? "start";
  const searching = Boolean(query.trim());
  const filtered = useMemo(
    () =>
      (data?.items ?? []).filter((item) => {
        if (onlyChanged && Object.keys(item.overrides).length === 0) return false;
        if (kindOf(item.key) !== kind) return false;
        return matchesQuery(query, item.key, ...Object.values(item.defaults), ...Object.values(item.overrides));
      }),
    [data, query, onlyChanged, kind],
  );
  const counts = useMemo(() => countBySection(filtered.map((item) => item.key)), [filtered]);
  const changedCount = (data?.items ?? []).filter((item) => kindOf(item.key) === kind && Object.keys(item.overrides).length > 0).length;
  const firstSection = SECTIONS.find((name) => counts[name] > 0) ?? "start";
  const current: TextSection = asked || searching ? section : counts[section] > 0 ? section : firstSection;
  const items = searching
    ? filtered
    : filtered.filter((item) => sectionOf(item.key) === current).sort((a, b) => bySectionOrder(a.key, b.key));

  return (
    <Page title={t(kind === "buttons" ? "settings_tab_buttons" : "settings_tab_messages")} wide>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <div className="flex flex-wrap items-start gap-6">
            <aside aria-label={t("texts_sections")} className="flex w-full flex-col gap-3 md:sticky md:top-6 md:w-64 md:shrink-0">
              <SearchBox value={query} onChange={setQuery} placeholder={t("search_texts")} />
              <div className="flex flex-wrap gap-2">
                <Button small tone={onlyChanged ? "primary" : "quiet"} aria-pressed={onlyChanged} onClick={() => setOnlyChanged((v) => !v)}>
                  {t("only_changed")} · {changedCount}
                </Button>
                <ResetTextsButton
                  keys={(data?.items ?? [])
                    .filter((item) => kindOf(item.key) === kind && sectionOf(item.key) === current && Object.keys(item.overrides).length > 0)
                    .map((item) => item.key)}
                  onDone={() => void reload()}
                />
              </div>
              <nav className="flex flex-col gap-0.5 rounded-2xl border border-[var(--line)] bg-[var(--surface)] p-2">
                {SECTIONS.filter((name) => counts[name] > 0 || name === current).map((name) => {
                  const active = !searching && name === current;
                  return (
                    <button
                      key={name}
                      type="button"
                      aria-current={active ? "true" : undefined}
                      onClick={() => {
                        setQuery("");
                        setParams((prev) => {
                          const next = new URLSearchParams(prev);
                          next.set("section", name);
                          return next;
                        }, { replace: true });
                      }}
                      className={`flex min-h-11 items-center gap-2 rounded-xl px-3 text-start text-[0.9rem] font-semibold ${
                        active ? "bg-firouzeh-50 text-firouzeh-700" : "hover:bg-[var(--line)]"
                      }`}
                    >
                      {t(`texts_section_${name}`)}
                      <span className="ms-auto text-[0.75rem] font-normal text-[var(--hint)]">{counts[name]}</span>
                    </button>
                  );
                })}
              </nav>
            </aside>

            <div className="min-w-0 flex-1">
              <Section flush title={searching ? t("texts_search_results", { count: items.length }) : t(`texts_section_${current}`)}>
                {items.length === 0 ? (
                  <Empty title={t("empty_list")} />
                ) : (
                  <ul className="divide-y divide-[var(--line)]">
                    {items.map((item) => (
                      <li key={item.key}>
                        <button type="button" onClick={() => setOpen(item)} className="flex w-full flex-col gap-1 px-4 py-3 text-start hover:bg-[var(--bg)]">
                          <span className="line-clamp-2 text-[0.9rem] font-semibold" dir="auto">
                            {item.overrides[locale] ?? item.defaults[locale] ?? item.defaults.fa}
                          </span>
                          <span className="flex flex-wrap items-center gap-2">
                            <code className="text-[0.75rem] text-[var(--hint)]" dir="ltr">
                              {item.key}
                            </code>
                            {isButton(item.key) && <Badge>{t("texts_button")}</Badge>}
                            {Object.keys(item.overrides).length > 0 && <Badge tone="brand">{t("customised")}</Badge>}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </Section>
            </div>
            {open && (
              <TextSheet
                item={open}
                locales={value.locales}
                globals={value.globals}
                onClose={() => setOpen(null)}
                onSaved={() => {
                  setOpen(null);
                  void reload();
                }}
              />
            )}
          </div>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- channels

type ForcedRow = ChannelData["forced"][number];
type TrustRow = ChannelData["trust"][number];
type CheckInfo = { chat_id: number; title: string; url: string; bot_admin: boolean };

/** The report kinds a channel receives; the labels come from the order-kind names. */
function KindPicker({ kinds, available, onChange }: { kinds: string[]; available: string[]; onChange: (kinds: string[]) => void }) {
  const { t } = useSession();
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-2">
      {available.map((kind) => (
        <label key={kind} className="flex items-center gap-2 text-[0.85rem]">
          <Checkbox
            label={t(`order_kind_${kind}`)}
            checked={kinds.includes(kind)}
            onChange={(on) => onChange(on ? [...kinds, kind] : kinds.filter((item) => item !== kind))}
          />
          {t(`order_kind_${kind}`)}
        </label>
      ))}
    </div>
  );
}

function KindList({ kinds }: { kinds: string[] }) {
  const { t } = useSession();
  return (
    <span className="text-[0.78rem] text-[var(--hint)]">
      {kinds.map((kind) => t(`order_kind_${kind}`)).join(" · ")}
    </span>
  );
}

/** What Telegram said about a channel before the admin saved it. */
function CheckNote({ info }: { info: CheckInfo }) {
  const { t } = useSession();
  return (
    <p className="flex items-center gap-2 text-[0.8rem]">
      <Badge tone={info.bot_admin ? "health" : "loss"}>{info.bot_admin ? t("bot_admin_ok") : t("bot_admin_no")}</Badge>
      <span className="text-[var(--hint)]">{info.title}</span>
    </p>
  );
}

/** A channel Telegram could not verify or deliver to during the last minutes. */
function ChannelWarning({ text, label }: { text: string | null; label: string }) {
  if (!text) return null;
  return (
    <span title={text}>
      <Badge tone="loss">{label}</Badge>
    </span>
  );
}

export function Channels() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<ChannelData>("/api/admin/channels");
  const [busy, run] = useAction();
  const [forced, setForced] = useState({ chat_id: "", title: "", url: "" });
  const [forcedChecked, setForcedChecked] = useState<CheckInfo | null>(null);
  const [trust, setTrust] = useState({ chat_id: "", kinds: ["purchase", "renew"] });
  const [trustChecked, setTrustChecked] = useState<CheckInfo | null>(null);
  const [forcedEdit, setForcedEdit] = useState<ForcedRow | null>(null);
  const [trustEdit, setTrustEdit] = useState<TrustRow | null>(null);
  const [contact, setContact] = useState({ title: "", url: "https://t.me/" });
  const [log, setLog] = useState<{ topic: string; chat_id: string; thread_id: string } | null>(null);
  const writable = can("settings.write");

  const act = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await run(fn);
      toast(t(done));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const check = async (raw: string, apply: (info: CheckInfo) => void) => {
    try {
      const info = await run(() =>
        call<CheckInfo>("/api/admin/channels/check", { method: "POST", body: { chat_id: raw.trim() } }),
      );
      if (info) apply(info);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page title={t("nav_channels")}>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <Section title={t("forced_channels")}>
              <p className="mb-3 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("forced_channels_hint")}</p>
              <ul className="mb-4 divide-y divide-[var(--line)]">
                {value.forced.map((c) => (
                  <li key={c.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="flex min-w-0 flex-col">
                      <span className="flex flex-wrap items-center gap-2 font-semibold">
                        {c.title}
                        {c.is_active ? <Badge tone="health">{t("active")}</Badge> : <Badge>{t("inactive")}</Badge>}
                        <ChannelWarning text={c.warning} label={t("channel_check_failed")} />
                      </span>
                      <span className="truncate text-[0.78rem] text-[var(--hint)]" dir="ltr">
                        {c.chat_id}
                      </span>
                    </span>
                    {writable && (
                      <span className="flex shrink-0 gap-2">
                        <Button small onClick={() => setForcedEdit(c)}>
                          {t("edit")}
                        </Button>
                        <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/channels/forced/${c.id}`, { method: "DELETE" }), "removed")}>
                          {t("remove")}
                        </Button>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              {writable && (
                <div className="flex flex-col gap-2">
                  <div className="grid gap-2 sm:grid-cols-[1.1fr_auto_1fr_1.3fr_auto]">
                    <Input aria-label={t("channel_id")} placeholder="@channel / -100…" dir="ltr" value={forced.chat_id} onChange={(e) => setForced({ ...forced, chat_id: e.target.value })} />
                    <Button
                      busy={busy}
                      disabled={!forced.chat_id.trim()}
                      onClick={() =>
                        void check(forced.chat_id, (info) => {
                          setForcedChecked(info);
                          setForced((prev) => ({ ...prev, title: prev.title || info.title, url: prev.url || info.url }));
                        })
                      }
                    >
                      {t("check_channel")}
                    </Button>
                    <Input aria-label={t("title")} placeholder={t("title")} value={forced.title} onChange={(e) => setForced({ ...forced, title: e.target.value })} />
                    <Input aria-label={t("invite_link")} dir="ltr" placeholder={t("invite_link")} value={forced.url} onChange={(e) => setForced({ ...forced, url: e.target.value })} />
                    <Button
                      tone="primary"
                      busy={busy}
                      disabled={!forced.chat_id.trim()}
                      onClick={() =>
                        void act(async () => {
                          await call("/api/admin/channels/forced", { method: "POST", body: forced });
                          setForced({ chat_id: "", title: "", url: "" });
                          setForcedChecked(null);
                        }, "added")
                      }
                    >
                      {t("add")}
                    </Button>
                  </div>
                  {forcedChecked && <CheckNote info={forcedChecked} />}
                </div>
              )}
            </Section>

            <Section title={t("trust_channels")}>
              <p className="mb-3 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("trust_channels_hint")}</p>
              <ul className="mb-4 divide-y divide-[var(--line)]">
                {value.trust.map((row) => (
                  <li key={row.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="flex min-w-0 flex-col">
                      <span className="flex flex-wrap items-center gap-2 font-semibold">
                        {row.title || row.chat_id}
                        {row.is_active ? <Badge tone="health">{t("active")}</Badge> : <Badge>{t("inactive")}</Badge>}
                        <ChannelWarning text={row.warning} label={t("send_failed")} />
                      </span>
                      <span className="truncate text-[0.78rem] text-[var(--hint)]" dir="ltr">
                        {row.chat_id}
                      </span>
                      <KindList kinds={row.kinds} />
                    </span>
                    {writable && (
                      <span className="flex shrink-0 gap-2">
                        <Button small onClick={() => setTrustEdit(row)}>
                          {t("edit")}
                        </Button>
                        <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/channels/trust/${row.id}`, { method: "DELETE" }), "removed")}>
                          {t("remove")}
                        </Button>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              {writable && (
                <div className="flex flex-col gap-3">
                  <div className="grid gap-2 sm:grid-cols-[1.5fr_auto_auto]">
                    <Input aria-label={t("channel_id")} placeholder="@channel / -100…" dir="ltr" value={trust.chat_id} onChange={(e) => setTrust({ ...trust, chat_id: e.target.value })} />
                    <Button busy={busy} disabled={!trust.chat_id.trim()} onClick={() => void check(trust.chat_id, setTrustChecked)}>
                      {t("check_channel")}
                    </Button>
                    <Button
                      tone="primary"
                      busy={busy}
                      disabled={!trust.chat_id.trim() || trust.kinds.length === 0}
                      onClick={() =>
                        void act(async () => {
                          await call("/api/admin/channels/trust", {
                            method: "POST",
                            body: { chat_id: trust.chat_id, kinds: trust.kinds },
                          });
                          setTrust({ chat_id: "", kinds: ["purchase", "renew"] });
                          setTrustChecked(null);
                        }, "added")
                      }
                    >
                      {t("add")}
                    </Button>
                  </div>
                  {trustChecked && <CheckNote info={trustChecked} />}
                  <span className="text-[0.85rem] font-semibold">{t("report_kinds")}</span>
                  <KindPicker kinds={trust.kinds} available={value.trust_kinds} onChange={(kinds) => setTrust({ ...trust, kinds })} />
                </div>
              )}
            </Section>

            <Section title={t("log_channels")}>
              <p className="mb-3 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("log_channels_hint")}</p>
              <ul className="divide-y divide-[var(--line)]">
                {value.topics.map((topic) => {
                  const row = value.logs.find((l) => l.topic === topic);
                  return (
                    <li key={topic} className="flex items-center justify-between gap-3 py-2.5">
                      <span className="flex flex-col">
                        <span className="font-semibold">{t(`log_topic_${topic}`)}</span>
                        <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                          {row ? `${row.chat_id}${row.thread_id ? ` / ${row.thread_id}` : ""}` : t("not_set")}
                        </span>
                      </span>
                      {writable && (
                        <span className="flex gap-2">
                          {row && (
                            <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/channels/logs/${row.id}`, { method: "DELETE" }), "removed")}>
                              {t("remove")}
                            </Button>
                          )}
                          <Button
                            small
                            onClick={() => setLog({ topic, chat_id: row ? String(row.chat_id) : "", thread_id: row?.thread_id ? String(row.thread_id) : "" })}
                          >
                            {row ? t("edit") : t("set")}
                          </Button>
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Section>

            <Section title={t("support_contacts")}>
              <ul className="mb-4 divide-y divide-[var(--line)]">
                {value.contacts.map((c) => (
                  <li key={c.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="flex flex-col">
                      <span className="font-semibold">{c.title}</span>
                      <span className="text-[0.78rem] text-[var(--hint)]" dir="ltr">
                        {c.url}
                      </span>
                    </span>
                    {writable && (
                      <Button small tone="ghost" busy={busy} onClick={() => void act(() => call(`/api/admin/contacts/${c.id}`, { method: "DELETE" }), "removed")}>
                        {t("remove")}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              {writable && (
                <div className="grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]">
                  <Input aria-label={t("title")} placeholder={t("title")} value={contact.title} onChange={(e) => setContact({ ...contact, title: e.target.value })} />
                  <Input aria-label={t("link")} dir="ltr" value={contact.url} onChange={(e) => setContact({ ...contact, url: e.target.value })} />
                  <Button
                    tone="primary"
                    busy={busy}
                    disabled={!contact.title}
                    onClick={() =>
                      void act(async () => {
                        await call("/api/admin/contacts", { method: "POST", body: contact });
                        setContact({ title: "", url: "https://t.me/" });
                      }, "added")
                    }
                  >
                    {t("add")}
                  </Button>
                </div>
              )}
            </Section>

            <Sheet
              open={log !== null}
              onClose={() => setLog(null)}
              title={log ? t(`log_topic_${log.topic}`) : ""}
              footer={
                <Button
                  tone="primary"
                  busy={busy}
                  disabled={!log || !/^-?\d+$/.test(log.chat_id.trim())}
                  onClick={() =>
                    log &&
                    void act(async () => {
                      await call("/api/admin/channels/logs", {
                        method: "PUT",
                        body: { topic: log.topic, chat_id: Number(log.chat_id.trim()), thread_id: log.thread_id.trim() ? Number(log.thread_id.trim()) : null },
                      });
                      setLog(null);
                    }, "saved")
                  }
                >
                  {t("save")}
                </Button>
              }
            >
              {log && (
                <div className="flex flex-col gap-4">
                  <Field label={t("chat_id")} hint={t("chat_id_hint")}>
                    {(id) => <Input id={id} dir="ltr" value={log.chat_id} onChange={(e) => setLog({ ...log, chat_id: e.target.value })} />}
                  </Field>
                  <Field label={t("thread_id")} hint={t("thread_id_hint")}>
                    {(id) => <Input id={id} dir="ltr" value={log.thread_id} onChange={(e) => setLog({ ...log, thread_id: e.target.value })} />}
                  </Field>
                </div>
              )}
            </Sheet>

            <Sheet
              open={forcedEdit !== null}
              onClose={() => setForcedEdit(null)}
              title={forcedEdit ? forcedEdit.title : t("forced_channels")}
              footer={
                <Button
                  tone="primary"
                  busy={busy}
                  disabled={!forcedEdit}
                  onClick={() =>
                    forcedEdit &&
                    void act(async () => {
                      await call(`/api/admin/channels/forced/${forcedEdit.id}`, {
                        method: "PUT",
                        body: { title: forcedEdit.title, url: forcedEdit.url, is_active: forcedEdit.is_active },
                      });
                      setForcedEdit(null);
                    }, "saved")
                  }
                >
                  {t("save")}
                </Button>
              }
            >
              {forcedEdit && (
                <div className="flex flex-col gap-4">
                  <Field label={t("title")}>
                    {(id) => <Input id={id} value={forcedEdit.title} onChange={(e) => setForcedEdit({ ...forcedEdit, title: e.target.value })} />}
                  </Field>
                  <Field label={t("invite_link")} hint={t("invite_link_hint")}>
                    {(id) => <Input id={id} dir="ltr" value={forcedEdit.url} onChange={(e) => setForcedEdit({ ...forcedEdit, url: e.target.value })} />}
                  </Field>
                  <Toggle
                    checked={forcedEdit.is_active}
                    onChange={(on) => setForcedEdit({ ...forcedEdit, is_active: on })}
                    label={t("channel_is_active")}
                    hint={t("channel_is_active_hint")}
                  />
                </div>
              )}
            </Sheet>

            <Sheet
              open={trustEdit !== null}
              onClose={() => setTrustEdit(null)}
              title={trustEdit ? trustEdit.title || String(trustEdit.chat_id) : t("trust_channels")}
              footer={
                <Button
                  tone="primary"
                  busy={busy}
                  disabled={!trustEdit || trustEdit.kinds.length === 0}
                  onClick={() =>
                    trustEdit &&
                    void act(async () => {
                      await call(`/api/admin/channels/trust/${trustEdit.id}`, {
                        method: "PUT",
                        body: { kinds: trustEdit.kinds, is_active: trustEdit.is_active },
                      });
                      setTrustEdit(null);
                    }, "saved")
                  }
                >
                  {t("save")}
                </Button>
              }
            >
              {trustEdit && (
                <div className="flex flex-col gap-4">
                  <Toggle
                    checked={trustEdit.is_active}
                    onChange={(on) => setTrustEdit({ ...trustEdit, is_active: on })}
                    label={t("channel_is_active")}
                    hint={t("channel_is_active_hint")}
                  />
                  <div className="flex flex-col gap-2">
                    <span className="text-[0.88rem] font-semibold">{t("report_kinds")}</span>
                    <KindPicker kinds={trustEdit.kinds} available={value.trust_kinds} onChange={(kinds) => setTrustEdit({ ...trustEdit, kinds })} />
                  </div>
                </div>
              )}
            </Sheet>
          </>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- audit

function Diff({ before, after }: { before: Record<string, unknown> | null; after: Record<string, unknown> | null }) {
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])];
  if (keys.length === 0) return null;
  const show = (v: unknown) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
  return (
    <table className="w-full text-[0.8rem]" dir="ltr">
      <tbody>
        {keys.map((key) => {
          const a = before?.[key];
          const b = after?.[key];
          const changed = JSON.stringify(a) !== JSON.stringify(b);
          return (
            <tr key={key} className="border-t border-[var(--line)] align-top">
              <td className="py-1.5 pe-3 font-semibold">{key}</td>
              <td className={`py-1.5 pe-3 break-all ${changed && before ? "text-anar-700 line-through decoration-anar-500/50" : "text-[var(--hint)]"}`}>
                {before ? show(a) : ""}
              </td>
              <td className={`py-1.5 break-all ${changed ? "text-pista-700" : "text-[var(--hint)]"}`}>{show(b)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function Audit() {
  const { t } = useSession();
  const list = useList<AuditRow>("/api/admin/audit", ["action", "entity", "entity_id", "actor_id"]);
  const [open, setOpen] = useState<AuditRow | null>(null);
  const columns: Column<AuditRow>[] = [
    { key: "action", label: t("audit_action"), mobile: "title", render: (r) => <code className="text-[0.82rem]" dir="ltr">{r.action}</code> },
    {
      key: "entity",
      label: t("audit_entity"),
      mobile: "meta",
      render: (r) => (r.entity ? <span dir="ltr">{`${r.entity}${r.entity_id ? `#${r.entity_id}` : ""}`}</span> : "—"),
    },
    { key: "actor", label: t("audit_actor"), mobile: "meta", render: (r) => <span dir="ltr">{`${r.actor_type}${r.actor_id ? `#${r.actor_id}` : ""}`}</span> },
    { key: "ip", label: "IP", mobile: "hide", render: (r) => <span dir="ltr">{r.ip ?? "—"}</span> },
    { key: "at", label: t("audit_when"), mobile: "end", render: (r) => <When iso={r.created_at} time /> },
  ];
  return (
    <Page title={t("nav_audit")} wide>
      <Section flush>
        <Toolbar>
          <SearchBox value={list.get("action")} onChange={(action) => list.set({ action })} placeholder={t("audit_filter_action")} />
          <Input className="w-40" aria-label={t("audit_entity")} placeholder={t("audit_entity")} dir="ltr" value={list.get("entity")} onChange={(e) => list.set({ entity: e.target.value })} />
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} onRow={setOpen} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
      {open && (
        <Sheet open onClose={() => setOpen(null)} title={open.action} wide>
          <div className="flex flex-col gap-4">
            <Facts
              items={[
                [t("audit_actor"), <span key="a" dir="ltr">{`${open.actor_type}${open.actor_id ? `#${open.actor_id}` : ""}`}</span>],
                ["IP", <span key="ip" dir="ltr">{open.ip ?? "—"}</span>],
                [t("audit_when"), <When key="w" iso={open.created_at} time />],
              ]}
            />
            <Diff before={open.before} after={open.after} />
          </div>
        </Sheet>
      )}
    </Page>
  );
}

// ---------------------------------------------------------------- jobs

export function Jobs() {
  const { t, locale, toast, errorText } = useSession();
  const list = useList<JobRow>("/api/admin/jobs", ["status"]);
  const [busy, run] = useAction();
  const retryAll = async () => {
    try {
      const result = await run(() => call<{ retried: number }>("/api/admin/jobs/retry-dead", { method: "POST", body: {} }));
      toast(t("jobs_retried", { n: money(result?.retried ?? 0, locale) }));
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const data = list.data as (typeof list.data & { counts?: Record<string, number> }) | null;

  const retry = async (job: JobRow) => {
    try {
      await run(() => call(`/api/admin/jobs/${job.id}/retry`, { method: "POST" }));
      toast(t("job_requeued"));
      void list.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const columns: Column<JobRow>[] = [
    { key: "kind", label: t("job_kind"), mobile: "title", render: (j) => <code dir="ltr" className="text-[0.82rem]">{`${j.queue}/${j.kind}`}</code> },
    { key: "status", label: t("col_status"), mobile: "end", render: (j) => <Status value={j.status} /> },
    { key: "attempts", label: t("job_attempts"), mobile: "meta", render: (j) => <span className="num">{`${money(j.attempts, locale)} / ${money(j.max_attempts, locale)}`}</span> },
    { key: "error", label: t("job_error"), mobile: "meta", className: "max-w-sm", render: (j) => <span className="line-clamp-2 text-anar-700" dir="ltr">{j.last_error ?? ""}</span> },
    { key: "run", label: t("job_run_at"), mobile: "hide", render: (j) => <When iso={j.run_at} time /> },
    {
      key: "retry",
      label: "",
      mobile: "end",
      render: (j) =>
        ["dead", "failed"].includes(j.status) ? (
          <Button small busy={busy} onClick={() => void retry(j)}>
            {t("retry")}
          </Button>
        ) : null,
    },
  ];
  return (
    <Page
      title={t("nav_jobs")}
      wide
      actions={
        (data?.counts?.dead ?? 0) > 0 ? (
          <Button busy={busy} onClick={() => void retryAll()}>
            {t("retry_all_dead", { n: money(data?.counts?.dead ?? 0, locale) })}
          </Button>
        ) : null
      }
    >
      {data?.counts && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(data.counts)
            .filter(([, n]) => n > 0)
            .map(([status, n]) => (
              <button key={status} type="button" onClick={() => list.set({ status })} className="rounded-xl px-3 py-1.5 ring-1 ring-[var(--line)] hover:bg-[var(--surface)]">
                <Status value={status} /> <span className="num ms-1 font-bold">{money(n, locale)}</span>
              </button>
            ))}
        </div>
      )}
      <Section flush>
        <Toolbar>
          <Select className="w-auto" aria-label={t("filter_status")} value={list.get("status")} onChange={(e) => list.set({ status: e.target.value })}>
            <option value="">{t("filter_all_statuses")}</option>
            {["queued", "running", "done", "failed", "dead", "canceled"].map((s) => (
              <option key={s} value={s}>
                {t(`status_${s}`)}
              </option>
            ))}
          </Select>
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(value) => (
            <>
              <Table columns={columns} rows={value.items} />
              <Pager page={list.page} total={value.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
    </Page>
  );
}

// ---------------------------------------------------------------- reports

function isoDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

export function Reports() {
  const { t, locale, me } = useSession();
  const [range, setRange] = useState({ start: isoDay(-29), end: isoDay(0) });
  const path = `/api/admin/reports/sales${qs(range)}`;
  const { data, error, reload } = useLoad<SalesReport>(path);
  const presets: [string, number][] = [
    ["range_7", -6],
    ["range_30", -29],
    ["range_90", -89],
  ];

  return (
    <Page
      title={t("nav_reports")}
      actions={
        <a className="inline-flex min-h-10 items-center rounded-xl px-3.5 text-[0.88rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--surface)]" href={`/api/admin/reports/sales.csv${qs(range)}`}>
          {t("download_csv")}
        </a>
      }
    >
      <Section>
        <div className="flex flex-wrap items-end gap-3">
          {presets.map(([key, offset]) => (
            <Button key={key} small tone={range.start === isoDay(offset) && range.end === isoDay(0) ? "primary" : "quiet"} onClick={() => setRange({ start: isoDay(offset), end: isoDay(0) })}>
              {t(key)}
            </Button>
          ))}
          <Field label={t("from")}>{(id) => <Input id={id} type="date" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} />}</Field>
          <Field label={t("to")}>{(id) => <Input id={id} type="date" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} />}</Field>
        </div>
      </Section>
      <Async data={data} error={error} reload={reload}>
        {(report) => (
          <>
            <Section>
              <p className="mb-4 text-[0.95rem]">
                {t("report_totals", { orders: money(report.totals.orders, locale) })} <Money amount={report.totals.amount} className="font-extrabold" />
              </p>
              <Columns
                title={t("report_daily")}
                data={report.days.map((d) => ({
                  key: d.day,
                  label: new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", { day: "numeric", month: "short" }).format(new Date(d.day)),
                  value: d.amount,
                  detail: t("dash_orders_n", { n: money(d.orders, locale) }),
                }))}
                format={(v) => `${money(v, locale)} ${t("currency")}`}
                axisFormat={(v) => compact(v, locale)}
              />
            </Section>
            <div className="grid gap-6 lg:grid-cols-2">
              <Section title={t("report_by_plan")} flush>
                <ReportRows rows={report.plans.map((p, i) => ({ id: i, label: `${p.plan ?? "—"} (${t(`order_kind_${p.kind}`)})`, count: p.orders, amount: p.amount }))} />
              </Section>
              {report.providers && (
                <Section title={t("report_by_method")} flush>
                  <ReportRows rows={report.providers.map((p, i) => ({ id: i, label: t(`provider_${p.provider}`), count: p.payments, amount: p.amount }))} />
                </Section>
              )}
            </div>
          </>
        )}
      </Async>
      <Funnel range={range} />
      <TopCustomers range={range} />
      <Exports range={range} />
      {me?.role !== "reseller" && <Settlements />}
    </Page>
  );
}

interface TopCustomer {
  id: number;
  customer_id: number;
  customer: string;
  telegram_id: number;
  is_reseller: boolean;
  orders: number;
  spent: number;
  topped_up: number;
  last_order: string | null;
}

interface FunnelData {
  steps: { key: string; count: number }[];
  days_to_first: number | null;
}

function Funnel({ range }: { range: { start: string; end: string } }) {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<FunnelData>(`/api/admin/reports/funnel${qs(range)}`);
  return (
    <Section title={t("funnel_title")}>
      <Async data={data} error={error} reload={reload}>
        {(value) => {
          const top = value.steps[0]?.count ?? 0;
          if (!top) return <Empty title={t("funnel_empty")} />;
          return (
            <div className="flex flex-col gap-3">
              <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("funnel_hint")}</p>
              <ol className="flex flex-col gap-2.5">
                {value.steps.map((step, index) => {
                  const share = top ? step.count / top : 0;
                  const prev = index > 0 ? value.steps[index - 1]?.count ?? 0 : 0;
                  return (
                    <li key={step.key} className="flex flex-col gap-1">
                      <div className="flex items-baseline justify-between gap-3 text-[0.85rem]">
                        <span className="font-semibold">{t(`funnel_${step.key}`)}</span>
                        <span className="tabular-nums">
                          {money(step.count, locale)}
                          <span className="text-[var(--hint)]"> · {Math.round(share * 100)}%</span>
                          {index > 0 && prev > 0 && (
                            <span className="text-[0.75rem] text-[var(--hint)]"> ({t("funnel_from_prev", { percent: Math.round((step.count / prev) * 100) })})</span>
                          )}
                        </span>
                      </div>
                      <div className="h-2.5 overflow-hidden rounded-full bg-[var(--line)]" aria-hidden="true">
                        <div className="h-full rounded-full bg-firouzeh-600" style={{ width: `${Math.max(2, share * 100)}%` }} />
                      </div>
                    </li>
                  );
                })}
              </ol>
              {value.days_to_first !== null && (
                <p className="text-[0.82rem] text-[var(--hint)]">{t("funnel_days_to_first", { days: money(value.days_to_first, locale) })}</p>
              )}
            </div>
          );
        }}
      </Async>
    </Section>
  );
}

function TopCustomers({ range }: { range: { start: string; end: string } }) {
  const { t, locale, can } = useSession();
  const { data, error, reload } = useLoad<{ items: Omit<TopCustomer, "id">[] }>(`/api/admin/reports/top-customers${qs({ ...range, limit: 20 })}`);
  const rows: TopCustomer[] = (data?.items ?? []).map((r, i) => ({ ...r, id: i + 1 }));
  return (
    <Section title={t("top_customers")} flush>
      <Async data={data} error={error} reload={reload}>
        {() => (
          <Table
            rows={rows}
            empty={<Empty title={t("top_customers_empty")} />}
            columns={[
              { key: "rank", label: "#", mobile: "hide", render: (r) => money(r.id, locale) },
              {
                key: "name",
                label: t("col_customer"),
                mobile: "title",
                render: (r) => (
                  <span className="flex items-center gap-2">
                    {can("customers.read") && !r.is_reseller ? (
                      <Link className="font-semibold text-firouzeh-700" to={`/customers/${r.customer_id}`}>{r.customer}</Link>
                    ) : (
                      <span className="font-semibold">{r.customer}</span>
                    )}
                    {r.is_reseller && <Badge tone="brand">{t("reseller")}</Badge>}
                  </span>
                ),
              },
              { key: "orders", label: t("col_orders"), mobile: "meta", render: (r) => t("dash_orders_n", { n: money(r.orders, locale) }) },
              { key: "spent", label: t("spent"), mobile: "end", render: (r) => <Money amount={r.spent} /> },
              { key: "topped", label: t("topped_up"), mobile: "hide", render: (r) => <Money amount={r.topped_up} /> },
              { key: "last", label: t("last_order"), mobile: "hide", render: (r) => <When iso={r.last_order} /> },
            ]}
          />
        )}
      </Async>
    </Section>
  );
}

const EXPORTS: [string, string][] = [
  ["orders", "orders.read"],
  ["payments", "payments.read"],
  ["services", "services.read"],
  ["customers", "customers.read"],
];

function Exports({ range }: { range: { start: string; end: string } }) {
  const { t, can, me } = useSession();
  const kinds = EXPORTS.filter(([kind, perm]) => can(perm) && !(kind === "customers" && me?.role === "reseller"));
  if (!kinds.length) return null;
  return (
    <Section title={t("excel_export")}>
      <p className="mb-3 text-[0.82rem] text-[var(--hint)]">{t("excel_export_hint")}</p>
      <div className="flex flex-wrap gap-2">
        {kinds.map(([kind]) => (
          <a
            key={kind}
            className="inline-flex min-h-10 items-center rounded-xl px-3.5 text-[0.88rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--surface)]"
            href={`/api/admin/export/${kind}.xlsx${qs(range)}`}
          >
            {t(`export_${kind}`)}
          </a>
        ))}
      </div>
    </Section>
  );
}

interface SettlementData {
  balances: { provider: string; collected: number; settled: number; outstanding: number }[];
  items: { id: number; provider: string; amount: number; reference: string | null; note: string | null; created_at: string }[];
}

function Settlements() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<SettlementData>("/api/admin/settlements");
  const [form, setForm] = useState<{ provider: string; amount: number | null; reference: string } | null>(null);
  const [busy, run] = useAction();
  const save = async () => {
    if (!form?.amount) return;
    try {
      await run(() =>
        call("/api/admin/settlements", { method: "POST", body: { provider: form.provider, amount: form.amount, reference: form.reference || null } }),
      );
      toast(t("saved"));
      setForm(null);
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("settlements")} flush>
      <Async data={data} error={error} reload={reload}>
        {(value) =>
          value.balances.length === 0 ? (
            <Empty title={t("empty_list")} />
          ) : (
            <Table
              rows={value.balances.map((b, index) => ({ ...b, id: index + 1 }))}
              columns={[
                { key: "provider", label: t("col_method"), mobile: "title", render: (b) => <span className="font-semibold">{t(`provider_${b.provider}`)}</span> },
                { key: "collected", label: t("collected"), mobile: "meta", render: (b) => <Money amount={b.collected} /> },
                { key: "settled", label: t("settled"), mobile: "meta", render: (b) => <Money amount={b.settled} /> },
                { key: "outstanding", label: t("outstanding"), mobile: "end", render: (b) => <Money amount={b.outstanding} className="font-bold" /> },
                {
                  key: "action",
                  label: "",
                  mobile: "meta",
                  render: (b) =>
                    can("payments.refund") && b.outstanding > 0 ? (
                      <Button small onClick={() => setForm({ provider: b.provider, amount: b.outstanding, reference: "" })}>
                        {t("record_settlement")}
                      </Button>
                    ) : null,
                },
              ]}
            />
          )
        }
      </Async>
      <Sheet
        open={form !== null}
        onClose={() => setForm(null)}
        title={form ? `${t("record_settlement")}: ${t(`provider_${form.provider}`)}` : ""}
        footer={
          <Button tone="primary" busy={busy} disabled={!form?.amount} onClick={() => void save()}>
            {t("save")}
          </Button>
        }
      >
        {form && (
          <div className="flex flex-col gap-4">
            <p className="text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("settlement_hint")}</p>
            <Field label={t("amount_toman")}>{(id) => <NumberInput id={id} value={form.amount} onChange={(v) => setForm({ ...form, amount: v })} />}</Field>
            <Field label={t("settlement_reference")}>
              {(id) => <Input id={id} value={form.reference} maxLength={128} onChange={(e) => setForm({ ...form, reference: e.target.value })} />}
            </Field>
          </div>
        )}
      </Sheet>
    </Section>
  );
}

function ReportRows({ rows }: { rows: { id: number; label: string; count: number; amount: number }[] }) {
  const { t, locale } = useSession();
  if (rows.length === 0) return <Empty title={t("empty_list")} />;
  const max = Math.max(...rows.map((r) => r.amount), 1);
  return (
    <ul className="divide-y divide-[var(--line)]">
      {rows.map((row) => (
        <li key={row.id} className="flex flex-col gap-1.5 px-4 py-2.5">
          <span className="flex items-center justify-between gap-3 text-[0.86rem]">
            <span className="font-semibold">{row.label}</span>
            <Money amount={row.amount} />
          </span>
          <span className="flex items-center gap-2">
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--line)]">
              <span className="block h-full rounded-full bg-firouzeh-500" style={{ width: `${(row.amount / max) * 100}%` }} />
            </span>
            <span className="num text-[0.75rem] text-[var(--hint)]">{money(row.count, locale)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------- account

export function Account() {
  const { t, me, toast, errorText } = useSession();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, run] = useAction();
  const change = async () => {
    try {
      await run(() => call("/api/auth/password", { method: "POST", body: { current_password: current, new_password: next } }));
      setCurrent("");
      setNext("");
      toast(t("password_changed"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Page title={t("account")}>
      <Section>
        <p className="font-bold" dir="ltr">
          {me?.username}
        </p>
        <p className="text-[0.85rem] text-[var(--hint)]">{t(`role_${me?.role ?? "admin"}`)}</p>
      </Section>
      <Section title={t("change_password")}>
        <div className="flex max-w-md flex-col gap-4">
          <Field label={t("current_password")}>
            {(id) => <Input id={id} type="password" dir="ltr" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />}
          </Field>
          <Field label={t("new_password")} hint={t("password_hint")}>
            {(id) => <Input id={id} type="password" dir="ltr" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />}
          </Field>
          <p className="text-[0.8rem] text-[var(--hint)]">{t("password_change_signs_out")}</p>
          <Button tone="primary" busy={busy} disabled={!current || next.length < 10} onClick={() => void change()} className="self-start">
            {t("change_password")}
          </Button>
        </div>
      </Section>
      <Section><PasswordRecovery initialUsername={me?.username ?? ""} /></Section>
      <ApiKeys />
    </Page>
  );
}

interface ApiKeyRow {
  id: number;
  name: string;
  prefix: string;
  read_only: boolean;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

function ApiKeys() {
  const { t, toast, errorText } = useSession();
  const confirm = useConfirm();
  const { data, error, reload } = useLoad<{ items: ApiKeyRow[] }>("/api/auth/api-keys");
  const [name, setName] = useState("");
  const [readOnly, setReadOnly] = useState(true);
  const [fresh, setFresh] = useState<string | null>(null);
  const [busy, run] = useAction();
  const create = async () => {
    try {
      const result = await run(() => call<{ key: string }>("/api/auth/api-keys", { method: "POST", body: { name: name.trim(), read_only: readOnly } }));
      if (result) setFresh(result.key);
      setName("");
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const revoke = async (row: ApiKeyRow) => {
    const ok = await confirm({ title: t("revoke_key"), body: t("revoke_key_body", { name: row.name }), confirm: t("revoke_key"), danger: true });
    if (!ok) return;
    try {
      await run(() => call(`/api/auth/api-keys/${row.id}`, { method: "DELETE" }));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("api_keys")}>
      <p className="mb-4 text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("api_keys_hint")}</p>
      {fresh && (
        <div className="mb-4 flex flex-col gap-2 rounded-xl bg-saffron-100 p-3">
          <p className="text-[0.85rem] font-semibold">{t("api_key_shown_once")}</p>
          <CopyText text={fresh} />
        </div>
      )}
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label={t("api_key_name")}>
          {(id) => <Input id={id} maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Toggle checked={readOnly} onChange={setReadOnly} label={t("api_key_read_only")} />
        <Button tone="primary" busy={busy} disabled={!name.trim()} onClick={() => void create()}>
          {t("api_key_create")}
        </Button>
      </div>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <Table
            rows={value.items}
            empty={<Empty title={t("api_keys_empty")} />}
            columns={[
              { key: "name", label: t("api_key_name"), mobile: "title", render: (k) => <span className="font-semibold">{k.name}</span> },
              { key: "prefix", label: t("api_key_prefix"), mobile: "meta", render: (k) => <code dir="ltr">{`pbk_${k.prefix}_…`}</code> },
              { key: "mode", label: t("api_key_mode"), mobile: "meta", render: (k) => (k.read_only ? t("api_key_read_only") : t("api_key_read_write")) },
              { key: "used", label: t("api_key_last_used"), mobile: "hide", render: (k) => <When iso={k.last_used_at} time /> },
              {
                key: "state",
                label: t("col_status"),
                mobile: "end",
                render: (k) =>
                  k.revoked_at ? (
                    <Badge tone="loss">{t("revoked")}</Badge>
                  ) : (
                    <Button small tone="danger" busy={busy} onClick={() => void revoke(k)}>
                      {t("revoke_key")}
                    </Button>
                  ),
              },
            ]}
          />
        )}
      </Async>
    </Section>
  );
}

const LEVEL_TONE: Record<string, "health" | "wait" | "loss"> = { ok: "health", warn: "wait", fail: "loss" };

export function Health() {
  const { t } = useSession();
  const { data, error, reload } = useLoad<{ checks: { key: string; level: string; detail: string }[]; checked_at: string }>("/api/admin/doctor");
  return (
    <Page
      title={t("nav_health")}
      actions={
        <Button icon="refresh" onClick={() => void reload()}>
          {t("check_now")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <Section flush>
            <ul className="divide-y divide-[var(--line)]">
              {value.checks.map((check) => (
                <li key={check.key} className="flex items-start justify-between gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <p className="font-semibold">{t(`doctor_${check.key}`)}</p>
                    <p className="break-words text-[0.82rem] text-[var(--hint)]" dir="auto">{check.detail}</p>
                  </div>
                  <Badge tone={LEVEL_TONE[check.level] ?? "neutral"}>{t(`doctor_level_${check.level}`)}</Badge>
                </li>
              ))}
            </ul>
          </Section>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- backups

interface BackupData {
  items: { name: string; size: number; created_at: string }[];
  schedule: BackupSchedule & { passphrase_set: boolean; timezone: string };
}

function BackupScheduleSection({ saved, timezone, onSaved }: { saved: BackupSchedule; timezone: string; onSaved: () => void }) {
  const { t, locale, toast, errorText } = useSession();
  const pick = (value: BackupSchedule): BackupSchedule => ({
    enabled: value.enabled,
    every_hours: value.every_hours,
    hour: value.hour,
    keep: value.keep,
    telegram: value.telegram,
  });
  const [draft, setDraft] = useState<BackupSchedule>(() => pick(saved));
  const [busy, run] = useAction();
  const dirty = (Object.keys(draft) as (keyof BackupSchedule)[]).some((key) => draft[key] !== saved[key]);
  const set = (change: Partial<BackupSchedule>) => setDraft((prev) => ({ ...prev, ...change }));
  const number = (value: number) => money(value, locale);
  const label = (hours: number) => {
    const [key, vars] = everyLabel(hours);
    return t(key, Object.fromEntries(Object.entries(vars).map(([name, value]) => [name, number(value)])));
  };
  const [summaryKey, summaryVars] = summary(saved);
  const save = async () => {
    try {
      await run(() => call("/api/admin/backups/schedule", { method: "PUT", body: draft }));
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("backup_auto_title")}>
      <p className="mb-4 leading-relaxed">
        {t(summaryKey, Object.fromEntries(Object.entries(summaryVars).map(([name, value]) => [name, number(value)])))}{" "}
        {saved.enabled && saved.telegram ? t("backup_schedule_telegram") : ""}
      </p>
      <div className="flex flex-col gap-5">
        <Toggle checked={draft.enabled} onChange={(enabled) => set({ enabled })} label={t("setting_backup_auto_enabled")} />
        {draft.enabled && (
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label={t("setting_backup_every_hours")}>
              {(id) => (
                <Select id={id} value={String(draft.every_hours)} onChange={(e) => set({ every_hours: Number(e.target.value) })}>
                  {everyOptions(draft.every_hours).map((hours) => (
                    <option key={hours} value={hours}>
                      {label(hours)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {atAnHour(draft.every_hours) && (
              <Field label={t("setting_backup_hour")} hint={t("setting_backup_hour_hint", { timezone })}>
                {(id) => (
                  <Select id={id} value={String(draft.hour)} onChange={(e) => set({ hour: Number(e.target.value) })}>
                    {Array.from({ length: 24 }, (_, hour) => (
                      <option key={hour} value={hour}>
                        {`${String(hour).padStart(2, "0")}:00`.replace(/\d/g, (d) => digits(d, locale))}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            )}
            <Field label={t("setting_backup_keep")} hint={t("setting_backup_keep_hint")}>
              {(id) => (
                <NumberInput id={id} value={draft.keep} onChange={(value) => set({ keep: Math.min(365, Math.max(1, value ?? 1)) })} />
              )}
            </Field>
          </div>
        )}
        <Toggle
          checked={draft.telegram}
          onChange={(telegram) => set({ telegram })}
          label={t("setting_backup_send_telegram")}
          hint={t("setting_backup_send_telegram_hint")}
        />
        <div className="flex flex-wrap gap-2">
          <Button tone="primary" busy={busy} disabled={!dirty} onClick={() => void save()}>
            {t("save_changes")}
          </Button>
          {dirty && <Button onClick={() => setDraft(pick(saved))}>{t("discard")}</Button>}
        </div>
      </div>
    </Section>
  );
}

export function Backups() {
  const { t, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<BackupData>("/api/admin/backups");
  const [busy, run] = useAction();
  const start = async () => {
    try {
      await run(() => call("/api/admin/backups", { method: "POST" }));
      toast(t("backup_started"));
      window.setTimeout(() => void reload(), 8000);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Page
      title={t("nav_backups")}
      actions={
        <Button tone="primary" busy={busy} disabled={!data?.schedule.passphrase_set} onClick={() => void start()}>
          {t("backup_now")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            {!value.schedule.passphrase_set && (
              <p className="rounded-2xl bg-anar-100 px-4 py-3 text-[0.88rem] font-semibold text-anar-700">{t("backup_no_passphrase")}</p>
            )}
            <BackupScheduleSection
              key={JSON.stringify(value.schedule)}
              saved={value.schedule}
              timezone={value.schedule.timezone}
              onSaved={() => void reload()}
            />
            <p className="px-1 text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("backup_contents_hint")}</p>
            <Section flush title={t("backup_files")}>
              {value.items.length === 0 ? (
                <Empty title={t("backup_none")} />
              ) : (
                <ul className="divide-y divide-[var(--line)]">
                  {value.items.map((item) => (
                    <li key={item.name} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                      <span className="flex flex-col">
                        <span className="font-mono text-[0.82rem]" dir="ltr">
                          {item.name}
                        </span>
                        <span className="text-[0.78rem] text-[var(--hint)]">
                          <When iso={item.created_at} time /> — <Bytes value={item.size} />
                        </span>
                      </span>
                      <span className="flex items-center gap-2">
                        <VerifyBackupButton name={item.name} />
                        <a
                          className="inline-flex min-h-8 items-center rounded-lg px-2.5 text-[0.8rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--bg)]"
                          href={`/api/admin/backups/${encodeURIComponent(item.name)}`}
                          download
                        >
                          {t("download")}
                        </a>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
            <BackupBotForm />
            <BundleSection />
          </>
        )}
      </Async>
    </Page>
  );
}

interface BundleReport {
  dry_run: boolean;
  created: Record<string, number>;
  updated: Record<string, number>;
  warnings: string[];
}

function BundleSection() {
  const { t, locale, toast, errorText } = useSession();
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<BundleReport | null>(null);
  const [busy, run] = useAction();
  const send = async (dryRun: boolean) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await run(() => call<BundleReport>(`/api/admin/bundle?dry_run=${dryRun}`, { method: "POST", form }));
      if (result) setReport(result);
      if (!dryRun) {
        toast(t("bundle_imported"));
        setFile(null);
      }
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const lines = (counts: Record<string, number>) =>
    Object.entries(counts).map(([kind, n]) => `${t(`bundle_kind_${kind}`)}: ${money(n, locale)}`);
  return (
    <Section title={t("bundle_title")}>
      <p className="mb-4 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("bundle_intro")}</p>
      <div className="flex flex-wrap gap-2">
        <a
          className="inline-flex min-h-10 items-center rounded-xl px-4 font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--bg)]"
          href="/api/admin/bundle"
          download
        >
          {t("bundle_export")}
        </a>
        <label className="inline-flex min-h-10 cursor-pointer items-center rounded-xl px-4 font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--bg)]">
          {file ? file.name : t("bundle_choose")}
          <input
            type="file"
            accept=".json,application/json"
            className="sr-only"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setReport(null);
              e.target.value = "";
            }}
          />
        </label>
      </div>
      {file && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button busy={busy} onClick={() => void send(true)}>
            {t("bundle_check")}
          </Button>
          <Button tone="primary" busy={busy} disabled={!report?.dry_run} onClick={() => void send(false)}>
            {t("bundle_import")}
          </Button>
        </div>
      )}
      {report && (
        <div className="mt-4 flex flex-col gap-2 rounded-xl bg-[var(--bg)] p-3 text-[0.85rem]">
          <p className="font-bold">{report.dry_run ? t("bundle_would") : t("bundle_did")}</p>
          {lines(report.created).map((line) => (
            <p key={`c${line}`}>+ {line}</p>
          ))}
          {lines(report.updated).map((line) => (
            <p key={`u${line}`}>~ {line}</p>
          ))}
          {!Object.keys(report.created).length && !Object.keys(report.updated).length && <p>{t("bundle_nothing")}</p>}
          {report.warnings.length > 0 && (
            <ul className="list-inside list-disc text-saffron-700">
              {report.warnings.map((w) => (
                <li key={w} dir="auto">
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Section>
  );
}
