import { useApp } from "../state";
import { Screen } from "../ui";

export function Profile() {
  const { me, t, locale } = useApp();
  if (!me) return null;
  const joined = me.profile?.member_since ? new Date(me.profile.member_since) : null;
  const rows = [
    [t("profile_name"), me.name || t("profile_unset")],
    [t("profile_username"), me.username ? `@${me.username}` : t("profile_unset")],
    [t("profile_telegram_id"), String(me.telegram_id)],
    [t("profile_phone"), me.profile?.phone_masked || t("profile_unset")],
    [t("profile_shop"), me.shop.title],
    [t("profile_member_since"), joined && Number.isFinite(joined.getTime())
      ? joined.toLocaleDateString(locale, { year: "numeric", month: "short", day: "numeric" }) : t("profile_unset")],
  ];
  return (
    <Screen title={t("profile_title")}>
      <section className="app-card p-4" aria-label={t("profile_identity")}>
        <h2 className="mb-2 font-bold">{t("profile_identity")}</h2>
        <dl>
          {rows.map(([label, value]) => (
            <div key={label} className="border-b border-[var(--line)] py-3 last:border-0">
              <dt className="text-xs text-[var(--hint)]">{label}</dt>
              <dd className="mt-1 break-words font-semibold" dir="auto">{value}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3 text-sm text-[var(--hint)]">{t("profile_identity_hint")}</p>
      </section>
    </Screen>
  );
}
