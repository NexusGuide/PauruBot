import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { daysLeft, digits } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Empty, Icon, Money, Page, SearchBox, When, useConfirm, Intro } from "../ui";

/** One bot run by a customer of this shop, as `GET /api/admin/customer-bots` reports it. */
export interface CustomerBot {
  shop_id: number;
  /** reseller: sells this shop's plans (TEN-04); own: sells from the customer's own panel (OPB). */
  kind: "reseller" | "own";
  title: string;
  bot_username: string | null;
  state: BotState;
  paused: string;
  expires_at: string | null;
  owner: { id: number; name: string; username: string | null } | null;
  profile_id: number | null;
  account: { id: number; username: string; status: string } | null;
  customers: number;
  active_services: number;
  orders_month: number;
  revenue_month: number;
  created_at: string;
}

export type BotState = "selling" | "paused" | "held" | "stopped" | "blocked";
export type BotFilter = "all" | "reseller" | "own" | "attention";

const STATE_TONE: Record<BotState, "health" | "wait" | "loss" | "neutral"> = {
  selling: "health",
  paused: "wait",
  held: "wait",
  stopped: "neutral",
  blocked: "loss",
};

/** Which bots a filter keeps; "attention" is every bot not selling right now. */
export function filterBots(items: CustomerBot[], filter: BotFilter, query: string): CustomerBot[] {
  const q = query.trim().toLowerCase().replace(/^@/, "");
  return items.filter((bot) => {
    if (filter === "attention" && bot.state === "selling") return false;
    if ((filter === "reseller" || filter === "own") && bot.kind !== filter) return false;
    if (!q) return true;
    return [bot.title, bot.bot_username, bot.owner?.name, bot.owner?.username]
      .filter(Boolean)
      .some((text) => String(text).toLowerCase().includes(q));
  });
}

export function CustomerBots() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<{ items: CustomerBot[] }>("/api/admin/customer-bots");
  const [filter, setFilter] = useState<BotFilter>("all");
  const [query, setQuery] = useState("");

  return (
    <Page title={t("nav_customer_bots")} wide actions={<Button tone="quiet" icon="refresh" onClick={() => void reload()}>{t("refresh")}</Button>}>
      <Async data={data} error={error} reload={() => void reload()}>
        {({ items }) => {
          const shown = filterBots(items, filter, query);
          const count = (f: BotFilter) => filterBots(items, f, "").length;
          const revenue = items.reduce((sum, bot) => sum + bot.revenue_month, 0);
          return (
            <>
              <Intro>{t("cbots_intro")}</Intro>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label={t("cbots_total")} value={digits(items.length, locale)} />
                <Stat label={t("cbots_selling")} value={digits(count("all") - count("attention"), locale)} />
                <Stat label={t("cbots_attention")} value={digits(count("attention"), locale)} warn={count("attention") > 0} />
                <Stat label={t("cbots_revenue_month")} value={<Money amount={revenue} className="!whitespace-normal" />} />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {(["all", "reseller", "own", "attention"] as BotFilter[]).map((f) => (
                  <button
                    key={f}
                    type="button"
                    aria-pressed={filter === f}
                    onClick={() => setFilter(f)}
                    className={`min-h-9 rounded-full px-3.5 text-[0.84rem] font-semibold ring-1 transition-colors ${
                      filter === f
                        ? "bg-firouzeh-600 text-white ring-firouzeh-600"
                        : "bg-[var(--surface)] text-[var(--text)] ring-[var(--line)] hover:bg-[var(--bg)]"
                    }`}
                  >
                    {t(`cbots_filter_${f}`)} · {digits(count(f), locale)}
                  </button>
                ))}
                <div className="ms-auto w-full md:w-72">
                  <SearchBox value={query} onChange={setQuery} placeholder={t("cbots_search")} />
                </div>
              </div>
              {shown.length === 0 ? (
                <Empty title={items.length === 0 ? t("cbots_none") : t("empty_list")} hint={items.length === 0 ? t("cbots_none_hint") : undefined} />
              ) : (
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  {shown.map((bot) => (
                    <BotCard key={bot.shop_id} bot={bot} onChanged={() => void reload()} />
                  ))}
                </div>
              )}
            </>
          );
        }}
      </Async>
    </Page>
  );
}

function Stat({ label, value, warn }: { label: string; value: ReactNode; warn?: boolean }) {
  return (
    <div className="rounded-2xl bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
      <p className="text-[0.8rem] text-[var(--hint)]">{label}</p>
      <p className={`mt-1 text-lg font-extrabold md:text-xl ${warn ? "text-saffron-700" : ""}`}>{value}</p>
    </div>
  );
}

function BotCard({ bot, onChanged }: { bot: CustomerBot; onChanged: () => void }) {
  const { t, locale, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  const ownPath = bot.owner ? `/api/admin/customers/${bot.owner.id}/own-bot` : null;

  const act = async (path: string, body: unknown, done: string) => {
    try {
      await run(() => call(path, { method: "POST", body }));
      toast(t(done));
      onChanged();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const stopReseller = async () => {
    const ok = await confirm({ title: t("cbots_stop_title"), body: t("cbots_stop_body"), confirm: t("own_bot_stop"), danger: true });
    if (ok && bot.profile_id) await act(`/api/admin/resellers/${bot.profile_id}/bot/stop`, undefined, "own_bot_stopped");
  };
  const left = bot.expires_at ? daysLeft(bot.expires_at) : null;
  const stateText =
    bot.state === "paused" && bot.paused ? t(`own_bot_status_paused_${bot.paused}`) : t(`cbots_state_${bot.state}`);

  return (
    <article className="flex flex-col gap-3 rounded-2xl bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
      <header className="flex items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-firouzeh-100 text-firouzeh-700">
          <Icon name="bot" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-bold">{bot.title}</p>
          <p className="truncate text-[0.82rem] text-[var(--hint)]" dir="ltr" style={{ textAlign: "start" }}>
            {bot.bot_username ? `@${bot.bot_username}` : "—"}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Badge tone={STATE_TONE[bot.state]}>{stateText}</Badge>
          <Badge tone="brand">{t(`cbots_kind_${bot.kind}`)}</Badge>
        </div>
      </header>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[0.84rem] sm:grid-cols-4">
        <Fig label={t("cbots_customers")} value={digits(bot.customers, locale)} />
        <Fig label={t("cbots_active")} value={digits(bot.active_services, locale)} />
        <Fig label={t("cbots_orders_month")} value={digits(bot.orders_month, locale)} />
        <Fig label={t("cbots_revenue_month")} value={<Money amount={bot.revenue_month} />} />
      </dl>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.82rem] text-[var(--hint)]">
        {bot.owner && (
          <span>
            {t("cbots_owner")}:{" "}
            <Link to={`/customers/${bot.owner.id}`} className="font-semibold text-firouzeh-700 hover:underline">
              {bot.owner.name}
            </Link>
          </span>
        )}
        {bot.account && (
          <span>
            {t("cbots_account")}: <b dir="ltr">{bot.account.username}</b>
          </span>
        )}
        {bot.kind === "own" && (
          <span>
            {t("cbots_valid")}:{" "}
            {bot.expires_at ? (
              <>
                <When iso={bot.expires_at} /> {left !== null && left >= 0 ? `(${t("own_bot_days", { days: digits(left, locale) })})` : ""}
              </>
            ) : (
              t("cbots_no_end")
            )}
          </span>
        )}
        <span>
          {t("cbots_since")}: <When iso={bot.created_at} />
        </span>
      </div>

      <div className="flex flex-wrap gap-2 border-t border-[var(--line)] pt-3">
        {bot.kind === "own" && ownPath && bot.state !== "stopped" && (
          <>
            <Button small tone="quiet" disabled={busy} onClick={() => void act(`${ownPath}/hold`, { hold: bot.state !== "held" }, bot.state === "held" ? "own_bot_hold_off" : "own_bot_hold_on")}>
              {bot.state === "held" ? t("cbots_resume") : t("own_bot_hold")}
            </Button>
            <Button small tone="quiet" disabled={busy} onClick={() => void act(`${ownPath}/expiry`, { days: 30 }, "own_bot_expiry_saved")}>
              {t("cbots_extend_30")}
            </Button>
          </>
        )}
        {bot.kind === "reseller" && bot.profile_id && bot.state === "blocked" && (
          <Button small tone="quiet" disabled={busy} onClick={() => void act(`/api/admin/resellers/${bot.profile_id}/bot/allow`, undefined, "cbots_allowed")}>
            {t("cbots_allow")}
          </Button>
        )}
        {bot.kind === "reseller" && bot.profile_id && bot.state !== "blocked" && bot.state !== "stopped" && (
          <Button small tone="danger" disabled={busy} onClick={() => void stopReseller()}>
            {t("own_bot_stop")}
          </Button>
        )}
        {bot.owner && (
          <Link
            to={`/customers/${bot.owner.id}`}
            className="ms-auto inline-flex min-h-9 items-center rounded-xl px-3 text-[0.84rem] font-semibold text-firouzeh-700 hover:bg-[var(--line)]"
          >
            {t("cbots_manage")}
          </Link>
        )}
      </div>
    </article>
  );
}

function Fig({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="text-[0.75rem] text-[var(--hint)]">{label}</dt>
      <dd className="font-bold">{value}</dd>
    </div>
  );
}
