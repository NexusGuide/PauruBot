import { useState } from "react";
import { date, daysLeft, digits, gb } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Badge, Button, Field, NumberInput, Section, Toggle } from "../ui";
import { BotLogsSheet } from "./BotLogsSheet";
import { WebhookHealth } from "./MyBot";

/** A purchased PasarGuard admin account the bot can sell from. */
export interface OwnAccount {
  id: number;
  username: string;
  status: string;
  plan_title: string | null;
  data_limit: number;
  used_bytes: number;
  max_users: number | null;
  users_count: number;
  expire_at: string | null;
  usable: boolean;
}

/** The customer's dedicated bot on its own panel, as the server reports it. */
export interface OwnBotState {
  shop_id: number;
  title: string;
  bot_username: string | null;
  connected: boolean;
  account_id: number | null;
  paused: string;
  hold: boolean;
  trial_enabled: boolean;
  /** Until when the shop lets this bot sell (ISO); null: no end. */
  expires_at?: string | null;
  customers: number;
  active_services: number;
  /** Who runs the bot: its owner (the buyer) first, then the admins staff added. */
  admins?: { telegram_id: number; owner: boolean; name: string }[];
  max_admins?: number;
}

interface OwnBotView {
  accounts: OwnAccount[];
  bot: OwnBotState | null;
}

const ACCOUNT_TONE: Record<string, "health" | "loss" | "wait"> = { active: "health", expired: "loss", paused: "wait" };

/** The «dedicated reseller bot» section of a customer's page (OPB): staff enter the bot token and
 *  pick which purchased admin account it sells from. */
export function OwnBotSection({ customerId }: { customerId: number | string }) {
  const { t, locale, toast, errorText } = useSession();
  const path = `/api/admin/customers/${customerId}/own-bot`;
  const { data, setData } = useLoad<OwnBotView>(path);
  const [token, setToken] = useState("");
  const [picked, setPicked] = useState<number | null>(null);
  const [days, setDays] = useState<number | null>(30);
  const [adminId, setAdminId] = useState("");
  const [busy, run] = useAction();
  const [logsOpen, setLogsOpen] = useState(false);
  if (!data) return null;
  const { bot, accounts } = data;
  const chosen = picked ?? bot?.account_id ?? accounts.find((a) => a.usable)?.id ?? null;
  const changed = bot === null || !bot.connected || chosen !== bot.account_id || token.trim() !== "";

  const apply = async (request: () => Promise<{ bot: OwnBotState | null }>, done: string) => {
    try {
      const result = await run(request);
      if (result) setData({ ...data, bot: result.bot });
      setToken("");
      toast(t(done));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const connect = () =>
    apply(
      () => call(path, { method: "POST", body: { account_id: chosen, token: token.trim() || null } }),
      "own_bot_saved",
    );
  const hold = (value: boolean) =>
    apply(() => call(`${path}/hold`, { method: "POST", body: { hold: value } }), value ? "own_bot_hold_on" : "own_bot_hold_off");
  const stop = () => apply(() => call(path, { method: "DELETE" }), "own_bot_stopped");
  const extend = (value: number | null) =>
    apply(() => call(`${path}/expiry`, { method: "POST", body: { days: value } }), "own_bot_expiry_saved");
  const addAdmin = async () => {
    await apply(() => call(`${path}/admins`, { method: "POST", body: { telegram_id: Number(adminId) } }), "own_bot_admin_added");
    setAdminId("");
  };
  const removeAdmin = (telegramId: number) =>
    apply(() => call(`${path}/admins/${telegramId}`, { method: "DELETE" }), "own_bot_admin_removed");

  if (accounts.length === 0 && bot === null) {
    return (
      <Section title={t("own_bot_title")}>
        <p className="text-[0.85rem] text-[var(--hint)]">{t("own_bot_no_accounts")}</p>
      </Section>
    );
  }
  const status = bot?.hold ? "held" : bot?.paused ? `paused_${bot.paused}` : "selling";
  return (
    <Section
      title={t("own_bot_title")}
      actions={bot?.connected ? <Badge tone={status === "selling" ? "health" : "loss"}>{t(`own_bot_status_${status}`)}</Badge> : undefined}
    >
      <div className="flex flex-col gap-5">
        <p className="text-[0.82rem] leading-relaxed text-[var(--hint)]">{t("own_bot_intro")}</p>

        {bot?.connected && (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2 rounded-xl bg-[var(--bg)] px-3 py-2.5 text-[0.88rem]">
              <span className="h-2.5 w-2.5 rounded-full bg-pista-500" aria-hidden="true" />
              <span>{t("own_bot_connected")}</span>
              <b dir="ltr">@{bot.bot_username}</b>
              <span className="ms-auto text-[0.8rem] text-[var(--hint)]">
                {t("own_bot_figures", { customers: digits(bot.customers, locale), services: digits(bot.active_services, locale) })}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex-1">
                <WebhookHealth basePath={path} />
              </div>
              <Button small tone="quiet" onClick={() => setLogsOpen(true)}>
                {t("bot_logs_open")}
              </Button>
            </div>
            {logsOpen && (
              <BotLogsSheet
                basePath={`/api/admin/customer-bots/${bot.shop_id}/logs`}
                title={bot.bot_username ? `@${bot.bot_username}` : bot.title}
                onClose={() => setLogsOpen(false)}
              />
            )}
          </div>
        )}

        <Field label={t("own_bot_token")} hint={bot?.connected ? t("own_bot_token_keep") : t("own_bot_token_hint")}>
          {(id) => (
            <input
              id={id}
              dir="ltr"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="123456789:AA…"
              className="min-h-10 w-full rounded-xl bg-[var(--surface)] px-3 text-[0.88rem] ring-1 ring-[var(--line)]"
            />
          )}
        </Field>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-[0.88rem] font-semibold">{t("own_bot_account")}</legend>
          {accounts.map((account) => {
            const left = daysLeft(account.expire_at);
            return (
              <label
                key={account.id}
                className={`flex flex-wrap items-center gap-3 rounded-xl px-3 py-2.5 ring-1 ${
                  chosen === account.id ? "bg-firouzeh-50/60 ring-firouzeh-600" : "ring-[var(--line)]"
                } ${account.usable ? "cursor-pointer" : "opacity-60"}`}
              >
                <input
                  type="radio"
                  name={`own-account-${customerId}`}
                  checked={chosen === account.id}
                  disabled={!account.usable}
                  onChange={() => setPicked(account.id)}
                />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="flex flex-wrap items-center gap-2">
                    <b dir="ltr" className="text-[0.88rem]">{account.username}</b>
                    <Badge tone={ACCOUNT_TONE[account.status] ?? "neutral"}>{t(`status_${account.status}`)}</Badge>
                  </span>
                  <small className="text-[0.78rem] text-[var(--hint)]">
                    {account.usable ? account.plan_title ?? "" : t("own_bot_account_unusable")}
                  </small>
                </span>
                <span className="flex flex-wrap gap-3 text-[0.8rem]">
                  <span>
                    {t("own_bot_traffic", {
                      used: digits(Math.round(gb(account.used_bytes)), locale),
                      total: account.data_limit ? digits(Math.round(gb(account.data_limit)), locale) : "∞",
                    })}
                  </span>
                  <span>
                    {t("own_bot_users", {
                      count: digits(account.users_count, locale),
                      max: account.max_users ? digits(account.max_users, locale) : "∞",
                    })}
                  </span>
                  {left !== null && <span>{t("own_bot_days", { days: digits(Math.max(0, left), locale) })}</span>}
                </span>
              </label>
            );
          })}
        </fieldset>

        {bot && (
          <div className="flex flex-col gap-2 rounded-xl bg-[var(--bg)] px-3 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[0.88rem] font-semibold">{t("own_bot_expiry")}</span>
              <span className="text-[0.85rem]">
                {!bot.expires_at
                  ? t("own_bot_expiry_none")
                  : new Date(bot.expires_at) > new Date()
                    ? t("own_bot_expiry_until", { date: date(bot.expires_at, locale) })
                    : t("own_bot_expiry_ended", { date: date(bot.expires_at, locale) })}
              </span>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <Field label={t("own_bot_expiry_days")} className="w-32">
                {(id) => <NumberInput id={id} allowEmpty value={days} onChange={setDays} />}
              </Field>
              <Button busy={busy} disabled={!days || days < 1} onClick={() => days && void extend(days)}>
                {t("own_bot_expiry_add")}
              </Button>
              {bot.expires_at && (
                <Button tone="ghost" disabled={busy} onClick={() => void extend(null)}>
                  {t("own_bot_expiry_clear")}
                </Button>
              )}
            </div>
            <p className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{t("own_bot_expiry_hint")}</p>
          </div>
        )}

        {bot && (
          <div className="flex flex-col gap-2 rounded-xl bg-[var(--bg)] px-3 py-3">
            <span className="text-[0.88rem] font-semibold">{t("own_bot_admins")}</span>
            <ul className="flex flex-col gap-1.5">
              {(bot.admins ?? []).map((admin) => (
                <li key={admin.telegram_id} className="flex flex-wrap items-center gap-2 text-[0.85rem]">
                  <b dir="ltr" className="num">{admin.telegram_id}</b>
                  {admin.name && <bdi className="text-[var(--hint)]">{admin.name}</bdi>}
                  {admin.owner ? (
                    <Badge tone="neutral">{t("own_bot_admin_owner")}</Badge>
                  ) : (
                    <Button small tone="ghost" className="ms-auto" disabled={busy} onClick={() => void removeAdmin(admin.telegram_id)}>
                      {t("own_bot_admin_remove")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {(bot.admins ?? []).filter((a) => !a.owner).length < (bot.max_admins ?? 3) && (
              <div className="flex flex-wrap items-end gap-2">
                <Field label={t("own_bot_admin_id")} className="w-48">
                  {(id) => (
                    <input
                      id={id}
                      dir="ltr"
                      inputMode="numeric"
                      autoComplete="off"
                      value={adminId}
                      onChange={(e) => setAdminId(e.target.value.replace(/[^0-9]/g, ""))}
                      placeholder="8697735343"
                      className="num min-h-10 w-full rounded-xl bg-[var(--surface)] px-3 text-[0.88rem] ring-1 ring-[var(--line)]"
                    />
                  )}
                </Field>
                <Button busy={busy} disabled={!/^[1-9][0-9]{4,12}$/.test(adminId)} onClick={() => void addAdmin()}>
                  {t("own_bot_admin_add")}
                </Button>
              </div>
            )}
            <p className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{t("own_bot_admins_hint", { max: digits(bot.max_admins ?? 3, locale) })}</p>
          </div>
        )}

        {bot?.connected && (
          <Toggle
            checked={bot.hold}
            onChange={(value) => void hold(value)}
            disabled={busy}
            label={t("own_bot_hold")}
            hint={t("own_bot_hold_hint")}
          />
        )}

        <div className="flex flex-wrap gap-2 border-t border-[var(--line)] pt-4">
          <Button tone="primary" busy={busy} disabled={!chosen || !changed || (!bot?.connected && !token.trim())} onClick={() => void connect()}>
            {bot?.connected ? t("own_bot_save") : t("own_bot_connect")}
          </Button>
          {bot?.connected && (
            <Button tone="danger" className="ms-auto" disabled={busy} onClick={() => void stop()}>
              {t("own_bot_stop")}
            </Button>
          )}
        </div>
        <p className="text-[0.78rem] leading-relaxed text-[var(--hint)]">{t("own_bot_keeps")}</p>
      </div>
    </Section>
  );
}
