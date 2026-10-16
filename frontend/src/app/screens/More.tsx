import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { money } from "@shared/format";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { translator } from "../receipts/copy";
import { Panel, Row, Screen } from "../ui";
import { CustomerSettings } from "../CustomerSettings";

export function More() {
  const { me, t, locale, setMarketing, toast } = useApp();
  const [savingMarketing, setSavingMarketing] = useState(false);
  const marketingBusy = useRef(false);
  const navigate = useNavigate();
  if (!me) return null;
  const referral = me.shop.bot_username ? `https://t.me/${me.shop.bot_username}?start=ref_${me.telegram_id}` : null;

  const changeMarketing = async () => {
    if (marketingBusy.current || !me.preferences) return;
    marketingBusy.current = true;
    setSavingMarketing(true);
    try {
      await setMarketing(!me.preferences.marketing_opt_out);
      haptic("success");
      toast(t("marketing_saved"));
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      marketingBusy.current = false;
      setSavingMarketing(false);
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
      <section className="mb-4 overflow-hidden rounded-3xl bg-[var(--surface)]">
        <Row onClick={() => navigate("/profile")}>
          <span className="flex-1 text-start">
            <span className="block font-bold">{t("profile_title")}</span>
            <span className="block text-sm text-[var(--hint)]">{t("profile_open_hint")}</span>
          </span><span aria-hidden="true">›</span>
        </Row>
      </section>
      <section className="mb-4 rounded-3xl bg-[var(--surface)]"><Row onClick={() => navigate("/receipts")}>{translator(locale)("title")}</Row></section>
      {me.preferences && (
        <section className="mb-4 rounded-3xl bg-[var(--surface)] p-4" aria-busy={savingMarketing}>
          <p className="font-bold" aria-live="polite">
            {t(me.preferences.marketing_opt_out ? "marketing_off" : "marketing_on")}
          </p>
          <p className="my-2 text-sm text-[var(--hint)]">{t("marketing_hint")}</p>
          <button type="button" disabled={savingMarketing} onClick={() => void changeMarketing()}
            className="rounded-xl bg-[var(--accent)] px-4 py-2 text-white disabled:opacity-50">
            {t(me.preferences.marketing_opt_out ? "marketing_enable" : "marketing_disable")}
          </button>
        </section>
      )}
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

      <CustomerSettings />

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
