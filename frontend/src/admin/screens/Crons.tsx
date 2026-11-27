import { useMemo, useState } from "react";
import { digits, money } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Field, NumberInput, Section, Select, Sheet, Toggle, When } from "../ui";

interface CronRow {
  name: string;
  category: string;
  default_interval: number;
  effective_interval: number;
  custom_interval: number | null;
  enabled: boolean;
  protected: boolean;
  last_run_at: string | null;
  last_duration_ms: number | null;
  last_status: string | null;
  last_error: string | null;
  run_count: number;
}

const CATEGORIES = ["all", "services", "payments", "panels", "reports", "growth", "cleanup", "system"] as const;
type Unit = "s" | "m" | "h" | "d";

function formatInterval(seconds: number, t: (k: string, p?: Record<string, unknown>) => string, locale: string): string {
  if (seconds % 86400 === 0) return t("cron_every_days", { n: digits(seconds / 86400, locale) });
  if (seconds % 3600 === 0) return t("cron_every_hours", { n: digits(seconds / 3600, locale) });
  if (seconds % 60 === 0) return t("cron_every_minutes", { n: digits(seconds / 60, locale) });
  return t("cron_every_seconds", { n: digits(seconds, locale) });
}

function splitInterval(seconds: number): { value: number; unit: Unit } {
  if (seconds % 86400 === 0) return { value: seconds / 86400, unit: "d" };
  if (seconds % 3600 === 0) return { value: seconds / 3600, unit: "h" };
  if (seconds % 60 === 0) return { value: seconds / 60, unit: "m" };
  return { value: seconds, unit: "s" };
}

function toSeconds(value: number, unit: Unit): number {
  if (unit === "d") return value * 86400;
  if (unit === "h") return value * 3600;
  if (unit === "m") return value * 60;
  return value;
}

export function CronsSection() {
  const { t, can, me, locale, toast, errorText } = useSession();
  const allowed = !me?.scoped && can("settings.read");
  const writable = !me?.scoped && can("settings.write");
  const { data, error, reload } = useLoad<{ items: CronRow[] }>(allowed ? "/api/admin/system/crons" : null);
  const [busy, run] = useAction();
  const [cat, setCat] = useState<(typeof CATEGORIES)[number]>("all");
  const [editing, setEditing] = useState<CronRow | null>(null);
  const [val, setVal] = useState<number | null>(5);
  const [unit, setUnit] = useState<Unit>("m");

  const items = data?.items ?? [];
  const filtered = useMemo(
    () => (cat === "all" ? items : items.filter((item) => item.category === cat)),
    [items, cat],
  );

  if (!allowed) return null;

  const openEdit = (row: CronRow) => {
    const parts = splitInterval(row.effective_interval);
    setVal(parts.value);
    setUnit(parts.unit);
    setEditing(row);
  };

  const toggleEnabled = async (row: CronRow, next: boolean) => {
    try {
      await run(() =>
        call(`/api/admin/system/crons/${encodeURIComponent(row.name)}`, {
          method: "PUT",
          body: { enabled: next },
        }),
      );
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const saveInterval = async (reset = false) => {
    if (!editing) return;
    const secs = reset ? null : toSeconds(val ?? 0, unit);
    try {
      await run(() =>
        call(`/api/admin/system/crons/${encodeURIComponent(editing.name)}`, {
          method: "PUT",
          body: reset ? { reset_interval: true } : { interval_seconds: secs },
        }),
      );
      toast(t("saved"));
      setEditing(null);
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const triggerNow = async (row: CronRow) => {
    try {
      await run(() =>
        call(`/api/admin/system/crons/${encodeURIComponent(row.name)}/run`, {
          method: "POST",
        }),
      );
      toast(t("cron_run_queued", { name: row.name }));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Section
      title={t("crons_title")}
      actions={
        <Button small tone="ghost" busy={busy} onClick={() => void reload()}>
          {t("refresh")}
        </Button>
      }
    >
      <p className="mb-3 text-[0.84rem] leading-relaxed text-[var(--hint)]">{t("crons_hint")}</p>
      <div className="mb-4 flex flex-wrap gap-1.5">
        {CATEGORIES.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setCat(c)}
            className={`rounded-xl px-3 py-1.5 text-[0.78rem] font-semibold transition-colors ${
              cat === c
                ? "bg-firouzeh-100 text-firouzeh-700"
                : "bg-[var(--bg)] text-[var(--hint)] ring-1 ring-[var(--line)] hover:text-[var(--text)]"
            }`}
          >
            {t(`cron_cat_${c}`)}
          </button>
        ))}
      </div>

      <Async data={data} error={error} reload={reload}>
        {() => (
          <ul className="divide-y divide-[var(--line)]">
            {filtered.map((row) => {
              const titleKey = `cron_name_${row.name.replace(/\./g, "_")}`;
              const translated = t(titleKey);
              const label = translated === titleKey ? row.name : translated;
              const statusTone = !row.enabled
                ? "neutral"
                : row.last_status === "ok"
                  ? "health"
                  : row.last_status === "error"
                    ? "loss"
                    : "wait";
              const statusLabel = !row.enabled
                ? t("cron_state_disabled")
                : row.last_status === "ok"
                  ? t("cron_state_ok")
                  : row.last_status === "error"
                    ? t("cron_state_error")
                    : t("cron_state_idle");

              return (
                <li key={row.name} className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[0.9rem] font-bold">{label}</span>
                      <code dir="ltr" className="text-[0.74rem] text-[var(--hint)]">
                        {row.name}
                      </code>
                      <Badge tone={statusTone}>{statusLabel}</Badge>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.76rem] text-[var(--hint)]">
                      <span>
                        {formatInterval(row.effective_interval, t, locale)}
                        {row.custom_interval !== null ? ` (${t("cron_custom")})` : ""}
                      </span>
                      {row.last_run_at && (
                        <span>
                          {t("cron_last_run")}: <When iso={row.last_run_at} time />
                        </span>
                      )}
                      {typeof row.last_duration_ms === "number" && (
                        <span dir="ltr" className="num font-mono">
                          {digits(row.last_duration_ms, locale)}ms
                        </span>
                      )}
                      {row.run_count > 0 && (
                        <span className="num">
                          {t("cron_runs_n", { n: money(row.run_count, locale) })}
                        </span>
                      )}
                    </div>
                    {row.last_error && (
                      <p dir="ltr" className="mt-1 line-clamp-2 font-mono text-[0.74rem] text-anar-700">
                        {row.last_error}
                      </p>
                    )}
                  </div>

                  {writable && (
                    <div className="flex flex-wrap items-center gap-2 shrink-0">
                      {!row.protected && (
                        <Toggle
                          label={t("cron_enabled")}
                          checked={row.enabled}
                          onChange={(on) => void toggleEnabled(row, on)}
                        />
                      )}
                      <Button small tone="ghost" busy={busy} onClick={() => openEdit(row)}>
                        {t("cron_schedule")}
                      </Button>
                      <Button small busy={busy} onClick={() => void triggerNow(row)}>
                        {t("cron_run_now")}
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Async>

      <Sheet
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={editing ? `${t("cron_schedule")} — ${editing.name}` : ""}
        footer={
          <>
            {editing?.custom_interval !== null && (
              <Button tone="ghost" busy={busy} onClick={() => void saveInterval(true)}>
                {t("cron_reset_default")}
              </Button>
            )}
            <Button
              tone="primary"
              busy={busy}
              disabled={!val || val < 1}
              onClick={() => void saveInterval(false)}
            >
              {t("save")}
            </Button>
          </>
        }
      >
        {editing && (
          <div className="flex flex-col gap-4">
            <p className="text-[0.84rem] text-[var(--hint)]">
              {t("cron_default_hint", {
                interval: formatInterval(editing.default_interval, t, locale),
              })}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("cron_interval_value")}>
                {(id) => <NumberInput id={id} value={val} onChange={setVal} />}
              </Field>
              <Field label={t("cron_interval_unit")}>
                {(id) => (
                  <Select id={id} value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
                    <option value="s">{t("cron_unit_s")}</option>
                    <option value="m">{t("cron_unit_m")}</option>
                    <option value="h">{t("cron_unit_h")}</option>
                    <option value="d">{t("cron_unit_d")}</option>
                  </Select>
                )}
              </Field>
            </div>
            <p className="text-[0.76rem] text-[var(--hint)]">{t("cron_interval_bounds")}</p>
          </div>
        )}
      </Sheet>
    </Section>
  );
}
