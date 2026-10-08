import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { money } from "@shared/format";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Panel, Row, Screen } from "../ui";

export function More() {
  const { me, t, locale, setLocale, toast } = useApp();
  const [switching, setSwitching] = useState(false);
  const navigate = useNavigate();
  if (!me) return null;
  const referral = me.shop.bot_username ? `https://t.me/${me.shop.bot_username}?start=ref_${me.telegram_id}` : null;

  const changeLocale = async (code: string) => {
    setSwitching(true);
    try {
      await setLocale(code);
      haptic("success");
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setSwitching(false);
    }
  };

  const share = () => {
    if (!referral) return;
    const url = `https://t.me/share/url?url=${encodeURIComponent(referral)}&text=${encodeURIComponent(t("share_text"))}`;
    if (tg) tg.openTelegramLink(url);
    else window.open(url, "_blank", "noopener");
  };

  return (
    <Screen title={t("more")}>
      {me.staff && (me.staff.dashboard || me.staff.review) && (
        <section className="mb-4 overflow-hidden rounded-3xl bg-[var(--surface)]">
          <Row onClick={() => navigate("/manage")}>
            <span className="flex-1">
              <span className="block font-bold">{t("manage")}</span>
              <span className="block text-sm text-[var(--hint)]">{t("manage_hint")}</span>
            </span>
            <span className="text-[var(--hint)]" aria-hidden="true">›</span>
          </Row>
        </section>
      )}
      {me.reseller.active && (
        <Panel className="mb-4 bg-firouzeh-50 text-firouzeh-700">
          <p className="font-bold">{t("reseller_active")}</p>
          <p className="text-sm">{t("reseller_discount_note", { percent: money(me.reseller.discount_percent, locale) })}</p>
          <button type="button" onClick={() => navigate("/desk")}
            className="mt-3 w-full rounded-2xl bg-[var(--accent)] py-3 font-bold text-white">
            {t("desk_open")}
          </button>
        </Panel>
      )}

      {me.referral.enabled && referral && (
        <Panel className="mb-4">
          <p className="font-bold">{t("invite_title")}</p>
          <p className="mt-1 text-sm text-[var(--hint)]">
            {t("invite_hint", { percent: money(me.referral.percent, locale) })}
          </p>
          <p className="num mt-3 text-sm">{t("invited_count", { count: money(me.referral.count, locale) })}</p>
          <button type="button" onClick={share}
            className="mt-3 w-full rounded-2xl bg-[var(--accent)] py-3 font-bold text-white">
            {t("share_invite")}
          </button>
        </Panel>
      )}

      <section className="mb-4 overflow-hidden rounded-3xl bg-[var(--surface)]">
        <p className="px-4 pt-4 text-sm font-bold">{t("language")}</p>
        {me.shop.languages.map((code) => (
          <Row key={code} onClick={() => !switching && code !== locale && changeLocale(code)}>
            <span className="flex-1">{t(`lang_${code}`)}</span>
            {code === locale && <span className="text-[var(--accent)]">✓</span>}
          </Row>
        ))}
      </section>

      {me.shop.support && (
        <section className="overflow-hidden rounded-3xl bg-[var(--surface)]">
          <Row onClick={() => tg?.openTelegramLink(`https://t.me/${me.shop.support}`)}>
            <span className="flex-1">{t("contact_support")}</span>
            <span className="text-sm text-[var(--hint)]" dir="ltr">@{me.shop.support}</span>
          </Row>
        </section>
      )}
    </Screen>
  );
}
