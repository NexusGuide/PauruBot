import { useCallback, useEffect, useState } from "react";
import { call } from "../api";
import { useSession } from "../session";
import { Badge, Button, Section, When } from "../ui";

interface BuildRun {
  id: number;
  run_number: number;
  name: string;
  display_title: string;
  status: string;
  conclusion: string | null;
  head_branch: string;
  head_sha: string;
  event: string;
  html_url: string;
  created_at: string | null;
  updated_at: string | null;
}

interface BuildPayload {
  repo: string;
  branch: string;
  runs: BuildRun[];
  error: string | null;
}

function runState(run: BuildRun): { tone: "health" | "loss" | "wait" | "neutral"; key: string } {
  if (run.status === "completed") {
    if (run.conclusion === "success") return { tone: "health", key: "build_status_success" };
    if (run.conclusion === "failure" || run.conclusion === "timed_out") return { tone: "loss", key: "build_status_failure" };
    if (run.conclusion === "cancelled" || run.conclusion === "skipped") return { tone: "neutral", key: "build_status_cancelled" };
    return { tone: "wait", key: "build_status_other" };
  }
  if (run.status === "in_progress") return { tone: "wait", key: "build_status_running" };
  return { tone: "neutral", key: "build_status_queued" };
}

export function BuildStatus() {
  const { t, can, me } = useSession();
  const [branch, setBranch] = useState<"dev" | "main">("dev");
  const [live, setLive] = useState(true);
  const [data, setData] = useState<BuildPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (silent = false) => {
      if (!silent) setBusy(true);
      try {
        const res = await call<BuildPayload>(`/api/admin/system/builds?branch=${encodeURIComponent(branch)}`);
        setData(res);
      } catch {
        setData((prev) =>
          prev
            ? { ...prev, error: "github_unreachable" }
            : { repo: "", branch, runs: [], error: "github_unreachable" },
        );
      } finally {
        setLoading(false);
        setBusy(false);
      }
    },
    [branch],
  );

  useEffect(() => {
    if (me?.role === "reseller" || !can("settings.read")) return;
    setLoading(true);
    void load(false);
  }, [load, me?.role, can]);

  useEffect(() => {
    if (!live || me?.role === "reseller" || !can("settings.read")) return;
    const timer = window.setInterval(() => void load(true), 30_000);
    return () => window.clearInterval(timer);
  }, [live, load, me?.role, can]);

  if (me?.role === "reseller" || !can("settings.read")) return null;

  const runs = data?.runs ?? [];
  const latest = runs[0];
  const latestState = latest ? runState(latest) : null;

  return (
    <Section
      title={t("dash_builds")}
      actions={
        <div className="flex items-center gap-1.5">
          {(["dev", "main"] as const).map((b) => (
            <button
              key={b}
              type="button"
              onClick={() => setBranch(b)}
              dir="ltr"
              className={`rounded-lg px-2 py-1 font-mono text-[0.75rem] font-semibold transition-colors ${
                branch === b
                  ? "bg-firouzeh-100 text-firouzeh-700"
                  : "text-[var(--hint)] hover:bg-[var(--surface-2)]"
              }`}
            >
              {b}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setLive((v) => !v)}
            className={`rounded-lg px-2 py-1 text-[0.74rem] font-semibold transition-colors ${
              live ? "bg-pista-100 text-pista-700" : "bg-[var(--surface-2)] text-[var(--hint)]"
            }`}
          >
            {live ? t("build_live_on") : t("build_live_off")}
          </button>
          <Button small tone="ghost" busy={busy} onClick={() => void load(false)}>
            {t("refresh")}
          </Button>
        </div>
      }
    >
      {loading && !data ? (
        <p className="text-[0.84rem] text-[var(--hint)]">{t("build_loading")}</p>
      ) : data?.error ? (
        <p className="text-[0.84rem] text-anar-700">{t("build_fetch_error")}</p>
      ) : runs.length === 0 ? (
        <p className="text-[0.84rem] text-[var(--hint)]">{t("build_empty", { branch })}</p>
      ) : (
        <div className="flex flex-col gap-2">
          {latest && latestState && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-[var(--bg)] px-3 py-2.5 ring-1 ring-[var(--line)]">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <Badge tone={latestState.tone}>{t(latestState.key)}</Badge>
                  <span className="truncate text-[0.86rem] font-bold">{latest.name}</span>
                  <span dir="ltr" className="num font-mono text-[0.74rem] text-[var(--hint)]">
                    #{latest.run_number}
                  </span>
                </div>
                <p className="mt-1 truncate text-[0.78rem] text-[var(--hint)]" dir="auto">
                  {latest.display_title}
                </p>
              </div>
              <div className="flex items-center gap-2 text-[0.74rem] text-[var(--hint)]">
                <span dir="ltr" className="font-mono">
                  {latest.head_sha}
                </span>
                {latest.updated_at && <When iso={latest.updated_at} />}
                {latest.html_url && (
                  <a
                    href={latest.html_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-semibold text-firouzeh-700 hover:underline"
                  >
                    GitHub
                  </a>
                )}
              </div>
            </div>
          )}
          {runs.length > 1 && (
            <ul className="divide-y divide-[var(--line)]">
              {runs.slice(1, 5).map((run) => {
                const st = runState(run);
                return (
                  <li key={run.id} className="flex items-center justify-between gap-2 py-2 text-[0.8rem]">
                    <div className="flex min-w-0 items-center gap-2">
                      <Badge tone={st.tone}>{t(st.key)}</Badge>
                      <span className="truncate font-semibold">{run.name}</span>
                      <span dir="ltr" className="num shrink-0 font-mono text-[0.72rem] text-[var(--hint)]">
                        {run.head_sha}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-2 text-[0.73rem] text-[var(--hint)]">
                      {run.updated_at && <When iso={run.updated_at} />}
                      {run.html_url && (
                        <a
                          href={run.html_url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-semibold text-firouzeh-700 hover:underline"
                        >
                          #{run.run_number}
                        </a>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </Section>
  );
}
