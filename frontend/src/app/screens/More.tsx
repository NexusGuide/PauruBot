import { useState } from "react";
import { money } from "@shared/format";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic, setThemePref, tg, themePref, type ThemePref } from "../telegram";
import { CustomerSettings } from "../CustomerSettings";
import { FunCards } from "./FunCards";
import { ReferralReward } from "./ReferralReward";
import { Item, Screen, Seg } from "../ui";

export function More() {
  const { me, t, locale } = useApp();
  const [theme, setTheme] = useState<ThemePref>(() => themePref());
  if (!me) return null;
  const referral = me.shop.bot_username ? `https://t.me/${me.shop.bot_username}?start=ref_${me.telegram_id}` : null;
  const joined = me.profile?.member_since ? new Date(me.profile.member_since) : null;
  const joinedText =
    joined && Number.isFinite(joined.getTime())
      ? joined.toLocaleDateString(locale, { year: "numeric", month: "short", day: "numeric" })
      : null;

  const stats = [
    { label: t("balance"), value: money(me.balance, locale) },
    ...(me.referral.enabled
      ? [
          { label: t("stat_invites"), value: money(me.referral.count, locale) },
          ...(me.referral.cash !== false
            ? [{ label: t("stat_reward"), value: `${money(me.referral.percent, locale)}${t("percent")}` }]
            : []),
        ]
      : []),
  ];

  const changeTheme = (value: string) => {
    const pref = value as ThemePref;
    setTheme(pref);
    setThemePref(pref);
    haptic("select");
  };

  const share = () => {
    if (!referral) return;
    const url = `https://t.me/share/url?url=${encodeURIComponent(referral)}&text=${encodeURIComponent(t("share_text"))}`;
    if (tg) tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener");
  };

  return (
    <Screen>
      <div className="mb-4 text-center">
        <div className="app-avatar">{(me.name || "?").trim().charAt(0)}</div>
        <h1 className="mt-3 text-2xl font-extrabold">{me.name || t("profile_unset")}</h1>
        {me.username && (
          <p className="app-hint text-sm" dir="ltr">
            @{me.username}
          </p>
        )}
        <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
          {joinedText && <span className="app-pill">{`${t("profile_member_since")} ${joinedText}`}</span>}
          <span className="app-pill num" dir="ltr">{me.telegram_id}</span>
        </div>
      </div>

      <div className="app-stats" style={{ gridTemplateColumns: `repeat(${stats.length}, 1fr)` }}>
        {stats.map((stat) => (
          <div key={stat.label} className="app-stat">
            <b className="num">{stat.value}</b>
            <small>{stat.label}</small>
          </div>
        ))}
      </div>

      <div className="app-card p-4">
        <div className="mb-3 flex items-center gap-3">
          <span className="app-ico" data-tone="gray">
            <Icon name="globe" />
          </span>
          <span className="app-grow text-[0.95rem] font-bold">{t("appearance")}</span>
        </div>
        <Seg
          options={[
            { value: "light", label: t("theme_light") },
            { value: "auto", label: t("theme_auto") },
            { value: "dark", label: t("theme_dark") },
          ]}
          value={theme}
          onChange={changeTheme}
        />
      </div>

      {me.referral.enabled && me.referral.config && <ReferralReward config={me.referral.config} />}

      <FunCards />

      <CustomerSettings />

      <p className="app-lbl">{t("account")}</p>
      <div className="app-card">
        {me.referral.enabled && referral && (
          <Item
            icon={<Icon name="gift" />}
            tone="orange"
            title={t("invite_title")}
            sub={me.referral.cash === false && me.referral.config
              ? t("invite_hint_config", { every: money(me.referral.config.every, locale) })
              : t("invite_hint", { percent: money(me.referral.percent, locale) })}
            onClick={share}
          />
        )}
        <Item icon={<Icon name="recv" />} title={t("receipts_title")} sub={t("receipts_hint")} to="/receipts" />
        <Item icon={<Icon name="headset" />} title={t("tickets_title")} sub={t("tickets_entry_hint")} to="/tickets" />
        <Item icon={<Icon name="help" />} title={t("help_title")} sub={t("help_entry_hint")} to="/help" />
        {me.staff?.own && (
          <Item icon={<Icon name="layers" />} tone="green" title={t("own_title")} sub={t("own_entry_hint")} to="/shop" />
        )}
        {me.staff && (me.staff.dashboard || me.staff.review) && (
          <Item icon={<Icon name="gauge" />} tone="green" title={t("manage")} sub={t("manage_hint")} to="/manage" />
        )}
        {me.reseller.active && (
          <Item
            icon={<Icon name="panel" />}
            tone="green"
            title={t("desk_open")}
            sub={t("reseller_discount_note", { percent: money(me.reseller.discount_percent, locale) })}
            to="/desk"
          />
        )}
        {me.shop.support && (
          <Item
            icon={<Icon name="headset" />}
            tone="gray"
            title={t("contact_support")}
            sub={<span dir="ltr">@{me.shop.support}</span>}
            onClick={() => tg?.openTelegramLink(`https://t.me/${me.shop.support}`)}
          />
        )}
      </div>
    </Screen>
  );
}
