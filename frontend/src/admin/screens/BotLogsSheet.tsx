import { useState } from "react";
import { digits } from "@shared/format";
import { qs } from "../api";
import { useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Empty, Money, SearchBox, Sheet, When } from "../ui";

export type BotLogCategory = "all" | "errors" | "orders" | "payments" | "jobs" | "audit";

export interface BotLogItem {
  id: string;
  at: string;
  category: "orders" | "payments" | "jobs" | "audit";
  level: "info" | "warn" | "error";
  title: string;
  status: string | null;
  actor: string;
  detail: string;
  error: string | null;
  amount: number | null;
  meta: Record<string, unknown> | null;
}

interface BotLogsResponse {
  bot: { shop_id: number; title: string; bot_username: string | null; kind: "reseller" | "own" };
  counts: Record<BotLogCategory, number>;
  items: BotLogItem[];
}

const LEVEL_TONE: Record<BotLogItem["level"], "health" | "wait" | "loss"> = {
  info: "health",
  warn: "wait",
  error: "loss",
};

const CATEGORIES: BotLogCategory[] = ["all", "errors", "orders", "payments", "jobs", "audit"];

export function BotLogsSheet({
  basePath,
  title,
  onClose,
}: {
  basePath: string;
  title: string;
  onClose: () => void;
}) {
  const { t, locale } = useSession();
  const [category, setCategory] = useState<BotLogCategory>("all");
  const [query, setQuery] = useState("");
  const queryStr = qs({ category, q: query.trim() || undefined, limit: 120 });
  const state = useLoad<BotLogsResponse>(`${basePath}${queryStr}`);
  const csvHref = `${basePath}.csv${qs({ category, q: query.trim() || undefined, limit: 300 })}`;

  return (
    <Sheet open onClose={onClose} title={`${t("bot_logs_title")} — ${title}`}>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[12rem] flex-1">
            <SearchBox value={query} onChange={setQuery} placeholder={t("bot_logs_search")} />
          </div>
          <Button small tone="quiet" onClick={() => void state.reload()}>
            {t("bot_logs_refresh")}
          </Button>
          <a
            href={csvHref}
            download
            className="inline-flex min-h-9 items-center rounded-xl bg-[var(--bg)] px-3 text-[0.82rem] font-semibold ring-1 ring-[var(--line)] hover:bg-[var(--line)]"
          >
            {t("billing_export_csv")}
          </a>
        </div>

        <div className="flex flex-wrap gap-1.5">
          {CATEGORIES.map((cat) => {
            const count = state.data?.counts?.[cat];
            const active = category === cat;
            return (
              <button
                key={cat}
                type="button"
                onClick={() => setCategory(cat)}
                className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-[0.8rem] font-semibold transition ${
                  active
                    ? "bg-firouzeh-600 text-white"
                    : "bg-[var(--bg)] text-[var(--ink)] ring-1 ring-[var(--line)] hover:bg-[var(--line)]"
                }`}
              >
                <span>{t(`bot_logs_cat_${cat}`)}</span>
                {count !== undefined && (
                  <span className={`text-[0.72rem] ${active ? "text-white/80" : "text-[var(--hint)]"}`}>
                    ({digits(count, locale)})
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <Async {...state}>
          {(data) =>
            data.items.length === 0 ? (
              <Empty title={t("bot_logs_empty")} hint={t("bot_logs_empty_hint")} />
            ) : (
              <ol className="flex flex-col gap-2.5">
                {data.items.map((item) => (
                  <li
                    key={item.id}
                    className="flex flex-col gap-1.5 rounded-xl bg-[var(--bg)] p-3 text-[0.82rem] ring-1 ring-[var(--line)]"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={LEVEL_TONE[item.level]}>{t(`bot_logs_cat_${item.category}`)}</Badge>
                      <b dir="ltr" className="font-mono text-[0.8rem]">
                        {item.title}
                      </b>
                      {item.status && <Badge tone="neutral">{item.status}</Badge>}
                      {item.amount !== null && item.amount > 0 && (
                        <span className="font-semibold">
                          <Money amount={item.amount} />
                        </span>
                      )}
                      <span className="ms-auto text-[0.74rem] text-[var(--hint)]">
                        <When iso={item.at} time />
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.78rem] text-[var(--hint)]">
                      {item.actor && (
                        <span>
                          {t("bot_logs_actor")}: <bdi className="text-[var(--ink)]">{item.actor}</bdi>
                        </span>
                      )}
                      {item.detail && <bdi>{item.detail}</bdi>}
                    </div>
                    {item.error && (
                      <pre
                        dir="ltr"
                        className="overflow-x-auto whitespace-pre-wrap rounded-lg bg-red-500/10 px-2.5 py-2 font-mono text-[0.75rem] text-[var(--loss)]"
                      >
                        {item.error}
                      </pre>
                    )}
                    {item.meta && Object.keys(item.meta).length > 0 && (
                      <details className="text-[0.75rem] text-[var(--hint)]">
                        <summary className="cursor-pointer select-none hover:text-[var(--ink)]">
                          {t("bot_logs_details")}
                        </summary>
                        <pre
                          dir="ltr"
                          className="mt-1.5 overflow-x-auto rounded-lg bg-[var(--surface)] p-2 font-mono text-[0.72rem] text-[var(--ink)] ring-1 ring-[var(--line)]"
                        >
                          {JSON.stringify(item.meta, null, 2)}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
              </ol>
            )
          }
        </Async>
      </div>
    </Sheet>
  );
}
