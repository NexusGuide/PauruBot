// Editors for settings whose value is a structured list: admin alerts, keyword replies, report parts.
import { useSession } from "./session";
import { Button, Checkbox, Input, Textarea, Toggle } from "./ui";

/** Report-group topics and staff-chat events, as the server knows them (core/settings.py). */
export const REPORT_TOPICS = [
  "orders", "payments", "users", "errors", "services", "reports",
  "backups", "resellers", "panels", "tickets", "trials", "referrals",
] as const;
export const STAFF_EVENTS = ["receipt", "card_verify", "order_failed", "reseller_application", "panel", "lottery", "own_bot"] as const;
export const DAILY_SECTIONS = ["summary", "money", "panels"] as const;

/** Turn one alert on or off; the setting stores the ones that are off. */
export function setMuted(muted: string[], key: string, on: boolean): string[] {
  const rest = muted.filter((item) => item !== key);
  return on ? rest : [...rest, key];
}

export function AlertMatrix({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const { t } = useSession();
  const row = (key: string, label: string) => (
    <div key={key} className="border-b border-[var(--line)] py-1 last:border-0">
      <Toggle label={label} checked={!value.includes(key)} onChange={(on) => onChange(setMuted(value, key, on))} />
    </div>
  );
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="rounded-xl bg-[var(--bg)] p-3">
        <p className="mb-1 text-[0.82rem] font-bold">{t("alerts_dm_title")}</p>
        <p className="mb-2 text-[0.78rem] text-[var(--hint)]">{t("alerts_dm_hint")}</p>
        {STAFF_EVENTS.map((event) => row(`dm:${event}`, t(`alert_event_${event}`)))}
      </div>
      <div className="rounded-xl bg-[var(--bg)] p-3">
        <p className="mb-1 text-[0.82rem] font-bold">{t("alerts_group_title")}</p>
        <p className="mb-2 text-[0.78rem] text-[var(--hint)]">{t("alerts_group_hint")}</p>
        {REPORT_TOPICS.map((topic) => row(`group:${topic}`, t(`log_topic_${topic}`)))}
      </div>
    </div>
  );
}

export interface AutoReply {
  words: string[];
  text: string;
}

/** Keywords as typed: split on commas (Latin or Persian), trimmed, empty ones dropped. */
export function parseWords(text: string): string[] {
  return text.split(/[,،]/).map((word) => word.trim()).filter(Boolean);
}

export function AutoReplies({ value, onChange }: { value: AutoReply[]; onChange: (value: AutoReply[]) => void }) {
  const { t } = useSession();
  const update = (index: number, patch: Partial<AutoReply>) => onChange(value.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {value.map((rule, index) => (
        <div key={index} className="flex flex-col gap-2 rounded-xl bg-[var(--bg)] p-3">
          <div className="flex gap-2">
            <Input
              aria-label={t("auto_reply_words")}
              placeholder={t("auto_reply_words")}
              className="flex-1"
              defaultValue={rule.words.join("، ")}
              onBlur={(e) => update(index, { words: parseWords(e.target.value) })}
            />
            <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, i) => i !== index))} />
          </div>
          <Textarea
            aria-label={t("auto_reply_text")}
            placeholder={t("auto_reply_text")}
            maxLength={2000}
            rows={2}
            value={rule.text}
            onChange={(e) => update(index, { text: e.target.value })}
          />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={value.length >= 30} onClick={() => onChange([...value, { words: [], text: "" }])}>
        {t("auto_reply_add")}
      </Button>
    </div>
  );
}

export function ReportSections({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const { t } = useSession();
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-2">
      {DAILY_SECTIONS.map((section) => (
        <label key={section} className="flex items-center gap-2 text-[0.88rem]">
          <Checkbox
            label={t(`daily_section_${section}`)}
            checked={value.includes(section)}
            onChange={(on) => onChange(on ? DAILY_SECTIONS.filter((s) => s === section || value.includes(s)) : value.filter((s) => s !== section))}
          />
          {t(`daily_section_${section}`)}
        </label>
      ))}
    </div>
  );
}
