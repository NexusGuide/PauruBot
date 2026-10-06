import { useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { call, type PanelRow } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Button, Facts, Field, Money, Page, Section, Select } from "../ui";

interface Summary {
  token: string;
  size: number;
  source: string;
  customers: number;
  resellers: number;
  balance_total: number;
  services: number;
  skipped_rows: number;
  panels: { key: string; label: string; services: number }[];
}

interface Report {
  source: string;
  dry_run: boolean;
  customers_new: number;
  customers_existing: number;
  balance_credited: number;
  resellers: number;
  services_imported: number;
  services_existing: number;
  services_not_on_panel: number;
  services_unmapped: number;
  panel_errors: string[];
  job_id?: number;
}

export function Migration() {
  const { t, locale, toast, errorText } = useSession();
  const panels = useLoad<{ items: PanelRow[] }>("/api/admin/panels");
  const last = useLoad<{ report: Report | null }>("/api/admin/migration/last");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [map, setMap] = useState<Record<string, number>>({});
  const [uploading, setUploading] = useState(false);
  const [waiting, setWaiting] = useState<number | null>(null);
  const [busy, run] = useAction();
  const n = (value: number) => money(value, locale);

  const reloadLast = useRef(last.reload);
  reloadLast.current = last.reload;
  useEffect(() => {
    if (waiting === null) return;
    const timer = window.setInterval(() => void reloadLast.current(), 3000);
    return () => window.clearInterval(timer);
  }, [waiting]);

  useEffect(() => {
    if (waiting !== null && last.data?.report?.job_id === waiting) setWaiting(null);
  }, [last.data, waiting]);

  const upload = async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    setUploading(true);
    try {
      const result = await call<Summary>("/api/admin/migration/upload", { form });
      setSummary(result);
      const guess: Record<string, number> = {};
      for (const panel of result.panels) {
        const match = panels.data?.items.find((p) => p.name.toLowerCase() === panel.key.toLowerCase());
        if (match) guess[panel.key] = match.id;
      }
      setMap(guess);
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setUploading(false);
    }
  };

  const start = async (dryRun: boolean) => {
    if (!summary) return;
    try {
      const result = await run(() =>
        call<{ job_id: number }>("/api/admin/migration/run", {
          method: "POST",
          body: { token: summary.token, source: summary.source, panel_map: map, dry_run: dryRun },
        }),
      );
      if (result) setWaiting(result.job_id);
      toast(dryRun ? t("migration_dry_started") : t("migration_started"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const report = last.data?.report;
  return (
    <Page title={t("nav_migration")}>
      <Section title={t("migration_upload")}>
        <p className="mb-4 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("migration_intro")}</p>
        <label className="inline-flex min-h-11 cursor-pointer items-center rounded-xl bg-firouzeh-600 px-4 font-semibold text-white">
          {uploading ? t("migration_reading") : t("migration_choose_file")}
          <input
            type="file"
            accept=".sql,.db,.sqlite,.sqlite3,.txt"
            className="sr-only"
            disabled={uploading}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void upload(file);
              e.target.value = "";
            }}
          />
        </label>
      </Section>

      {summary && (
        <Section title={t("migration_found", { source: t(`migration_source_${summary.source}`) })}>
          <Facts
            items={[
              [t("migration_customers"), n(summary.customers)],
              [t("migration_resellers"), n(summary.resellers)],
              [t("migration_balance"), <Money key="b" amount={summary.balance_total} />],
              [t("migration_services"), n(summary.services)],
            ]}
          />
          <div className="mt-5 flex flex-col gap-3 border-t border-[var(--line)] pt-4">
            <p className="text-[0.88rem] font-bold">{t("migration_map_panels")}</p>
            <p className="text-[0.82rem] text-[var(--hint)]">{t("migration_map_hint")}</p>
            {summary.panels.map((panel) => (
              <Field key={panel.key} label={t("migration_panel_label", { label: panel.label, n: n(panel.services) })}>
                {(id) => (
                  <Select
                    id={id}
                    value={map[panel.key] ?? ""}
                    onChange={(e) => {
                      const value = e.target.value;
                      setMap((prev) => {
                        const next = { ...prev };
                        if (value) next[panel.key] = Number(value);
                        else delete next[panel.key];
                        return next;
                      });
                    }}
                  >
                    <option value="">{t("migration_skip_panel")}</option>
                    {(panels.data?.items ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            ))}
          </div>
          <div className="mt-5 flex flex-wrap gap-2">
            <Button busy={busy || waiting !== null} onClick={() => void start(true)}>
              {t("migration_dry_run")}
            </Button>
            <Button tone="primary" busy={busy || waiting !== null} onClick={() => void start(false)}>
              {t("migration_run")}
            </Button>
          </div>
          <p className="mt-3 text-[0.8rem] text-[var(--hint)]">{t("migration_safe_twice")}</p>
        </Section>
      )}

      {report && (
        <Section title={report.dry_run ? t("migration_last_dry") : t("migration_last_run")}>
          <Facts
            items={[
              [t("migration_new_customers"), n(report.customers_new)],
              [t("migration_existing_customers"), n(report.customers_existing)],
              [t("migration_balance_credited"), <Money key="c" amount={report.balance_credited} />],
              [t("migration_resellers"), n(report.resellers)],
              [t("migration_services_imported"), n(report.services_imported)],
              [t("migration_services_existing"), n(report.services_existing)],
              [t("migration_services_missing"), n(report.services_not_on_panel)],
              [t("migration_services_unmapped"), n(report.services_unmapped)],
            ]}
          />
          {report.panel_errors.length > 0 && (
            <ul className="mt-3 list-inside list-disc text-[0.82rem] text-anar-700">
              {report.panel_errors.map((e) => (
                <li key={e} dir="auto">
                  {e}
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}
    </Page>
  );
}
