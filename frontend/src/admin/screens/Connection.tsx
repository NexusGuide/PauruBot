// Live connection status: every panel and its nodes, how fast they answer now and on average,
// uptime, and the last checks as a strip of bars. Refreshes itself while the page is open.

import { useEffect } from "react";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Empty, Page, Section, When } from "../ui";

interface Check {
  at: string;
  ok: boolean;
  ms: number | null;
}

interface Node {
  name: string;
  status: string;
  ok: boolean;
  message: string | null;
}

export interface ConnectionRow {
  id: number;
  name: string;
  kind: string;
  location: string | null;
  active: boolean;
  healthy: boolean | null;
  error: string | null;
  checked_at: string | null;
  down_since: string | null;
  latency_ms: number | null;
  latency_avg: number | null;
  uptime: { day: number | null; week: number | null } | null;
  history: Check[];
  nodes: Node[];
}

interface ConnectionData {
  panels: ConnectionRow[];
  at: string;
}

const REFRESH_MS = 30_000;

/** Fast, slow or very slow: the colour of a response time. */
export function latencyTone(ms: number | null): "health" | "wait" | "loss" | "neutral" {
  if (ms === null) return "neutral";
  if (ms < 400) return "health";
  if (ms < 1200) return "wait";
  return "loss";
}

const TONE_TEXT = { health: "text-pista-700", wait: "text-saffron-700", loss: "text-anar-700", neutral: "text-[var(--hint)]" };

function state(row: ConnectionRow): "off" | "up" | "down" | "unknown" {
  if (!row.active) return "off";
  if (row.healthy === null) return "unknown";
  return row.healthy ? "up" : "down";
}

const DOT = { up: "bg-pista-500", down: "bg-anar-500", off: "bg-[var(--line)]", unknown: "bg-saffron-500" };

function History({ checks }: { checks: Check[] }) {
  const { t } = useSession();
  const top = Math.max(400, ...checks.map((c) => c.ms ?? 0));
  const slots = Array.from({ length: 48 - Math.min(48, checks.length) });
  return (
    <div className="flex h-9 items-end gap-[2px]" dir="ltr" role="img" aria-label={t("conn_history")}>
      {slots.map((_, i) => (
        <span key={`e${i}`} className="h-1 flex-1 rounded-sm bg-[var(--surface-2)]" />
      ))}
      {checks.map((c) => (
        <span
          key={c.at}
          title={c.ok ? `${c.ms ?? "?"} ms` : t("conn_failed")}
          className={`flex-1 rounded-sm ${c.ok ? (latencyTone(c.ms) === "health" ? "bg-pista-500" : "bg-saffron-500") : "bg-anar-500"}`}
          style={{ height: c.ok ? `${Math.max(12, Math.round(((c.ms ?? 0) / top) * 100))}%` : "100%", opacity: c.ok ? 0.75 : 1 }}
        />
      ))}
    </div>
  );
}

function PanelStatus({ row }: { row: ConnectionRow }) {
  const { t } = useSession();
  const now = state(row);
  const tone = latencyTone(row.latency_ms);
  const badNodes = row.nodes.filter((n) => !n.ok).length;
  return (
    <article className="flex flex-col gap-3 rounded-2xl bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
      <header className="flex items-start gap-3">
        <span className={`mt-1.5 size-2.5 shrink-0 rounded-full ${DOT[now]} ${now === "up" ? "animate-pulse" : ""}`} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-bold">{row.name}</h3>
          <p className="truncate text-[0.8rem] text-[var(--hint)]">
            {[row.location, row.kind].filter(Boolean).join(" · ")}
          </p>
        </div>
        <div className="text-end">
          {now === "up" || now === "unknown" ? (
            <p className={`num text-[1.15rem] font-extrabold ${TONE_TEXT[tone]}`} dir="ltr">
              {row.latency_ms !== null ? `${row.latency_ms} ms` : "—"}
            </p>
          ) : (
            <Badge tone={now === "down" ? "loss" : "neutral"}>{t(now === "down" ? "conn_down" : "conn_inactive")}</Badge>
          )}
          {row.latency_avg !== null && (
            <p className="text-[0.74rem] text-[var(--hint)]">
              {t("conn_avg")}{" "}
              <span className="num" dir="ltr">
                {row.latency_avg} ms
              </span>
            </p>
          )}
        </div>
      </header>

      {row.history.length > 0 ? (
        <History checks={row.history} />
      ) : (
        <p className="text-[0.8rem] text-[var(--hint)]">{t("conn_no_checks")}</p>
      )}

      <dl className="grid grid-cols-3 gap-2 text-[0.78rem]">
        <div>
          <dt className="text-[var(--hint)]">{t("conn_uptime_day")}</dt>
          <dd className="num font-bold">{row.uptime?.day != null ? `${row.uptime.day}%` : "—"}</dd>
        </div>
        <div>
          <dt className="text-[var(--hint)]">{t("conn_uptime_week")}</dt>
          <dd className="num font-bold">{row.uptime?.week != null ? `${row.uptime.week}%` : "—"}</dd>
        </div>
        <div>
          <dt className="text-[var(--hint)]">{t("conn_last_check")}</dt>
          <dd className="font-bold">{row.checked_at ? <When iso={row.checked_at} time /> : "—"}</dd>
        </div>
      </dl>

      {now === "down" && row.error && (
        <p className="rounded-xl bg-anar-100 px-3 py-2 text-[0.8rem] text-anar-700" dir="auto">
          {row.down_since && (
            <>
              {t("conn_down_since")} <When iso={row.down_since} time />
              {" — "}
            </>
          )}
          {row.error}
        </p>
      )}

      {row.nodes.length > 0 && (
        <div className="flex flex-col gap-2 border-t border-[var(--line)] pt-3">
          <p className="text-[0.78rem] font-bold text-[var(--hint)]">
            {t("conn_nodes", { ok: row.nodes.length - badNodes, n: row.nodes.length })}
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {row.nodes.map((node, i) => (
              <li
                key={`${node.name}-${i}`}
                title={node.message ?? node.status}
                className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[0.78rem] ring-1 ${
                  node.ok ? "bg-[var(--bg)] ring-[var(--line)]" : "bg-anar-100 text-anar-700 ring-transparent"
                }`}
              >
                <span className={`size-1.5 rounded-full ${node.ok ? "bg-pista-500" : "bg-anar-500"}`} />
                <bdi>{node.name}</bdi>
                {!node.ok && <span className="opacity-80">· {node.status}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}

function Summary({ data }: { data: ConnectionData }) {
  const { t } = useSession();
  const active = data.panels.filter((p) => p.active);
  const up = active.filter((p) => p.healthy).length;
  const times = active.map((p) => p.latency_avg).filter((v): v is number => v !== null);
  const avg = times.length ? Math.round(times.reduce((a, b) => a + b, 0) / times.length) : null;
  const nodes = active.flatMap((p) => p.nodes);
  const badNodes = nodes.filter((n) => !n.ok).length;
  const tiles = [
    { label: t("conn_panels_up"), value: `${up}/${active.length}`, tone: up === active.length ? "health" : "loss" },
    { label: t("conn_avg_latency"), value: avg !== null ? `${avg} ms` : "—", tone: latencyTone(avg) },
    {
      label: t("conn_nodes_up"),
      value: nodes.length ? `${nodes.length - badNodes}/${nodes.length}` : "—",
      tone: nodes.length === 0 ? "neutral" : badNodes ? "loss" : "health",
    },
  ] as const;
  return (
    <div className="grid grid-cols-3 gap-2 md:gap-3">
      {tiles.map((tile) => (
        <div key={tile.label} className="rounded-2xl bg-[var(--surface)] px-3 py-3 ring-1 ring-[var(--line)] md:px-4">
          <p className="truncate text-[0.74rem] text-[var(--hint)] md:text-[0.8rem]">{tile.label}</p>
          <p className={`num mt-1 text-[1.1rem] font-extrabold md:text-[1.3rem] ${TONE_TEXT[tile.tone]}`} dir="ltr">
            {tile.value}
          </p>
        </div>
      ))}
    </div>
  );
}

export function Connection() {
  const { t, toast, errorText } = useSession();
  const loaded = useLoad<ConnectionData>("/api/admin/connection");
  const [busy, run] = useAction();
  const { reload, setData } = loaded;

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const checkAll = async () => {
    try {
      const fresh = await run(() => call<ConnectionData>("/api/admin/connection/check", { method: "POST" }));
      if (fresh) setData(fresh);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_connection")}
      actions={
        <Button tone="primary" icon="refresh" busy={busy} onClick={() => void checkAll()}>
          {t("conn_check_all")}
        </Button>
      }
    >
      <Async {...loaded}>
        {(data) =>
          data.panels.length === 0 ? (
            <Section>
              <Empty title={t("conn_empty")} hint={t("conn_empty_hint")} />
            </Section>
          ) : (
            <>
              <div className="flex items-center gap-2 text-[0.8rem] text-[var(--hint)]">
                <span className="size-2 animate-pulse rounded-full bg-pista-500" />
                <span>{t("conn_live")}</span>
                <span>·</span>
                <span>
                  {t("conn_updated")} <When iso={data.at} time />
                </span>
              </div>
              <Summary data={data} />
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {data.panels.map((row) => (
                  <PanelStatus key={row.id} row={row} />
                ))}
              </div>
            </>
          )
        }
      </Async>
    </Page>
  );
}
