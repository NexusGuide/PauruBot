import { useRef, useState } from "react";
import { errorText, useApp } from "./state";
import { haptic } from "./telegram";

/** Shared by More/Profile: identical explicit settings, no implicit opt-in on page visit. */
export function CustomerSettings() {
  const { me, locale, t, setLocale, setMarketing, setAlerts, toast } = useApp();
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  if (!me) return null;

  const save = async (action: () => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    try {
      await action();
      haptic("success");
      toast(t("profile_saved"));
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      busy.current = false;
      setPending(false);
    }
  };

  const languageCodes: string[] = me.shop.languages.filter(code => code === "fa" || code === "en");
  return <section className="mb-4 rounded-3xl bg-[var(--surface)] p-4" aria-label={t("profile_settings")} aria-busy={pending}>
    <h2 className="mb-3 font-bold">{t("profile_settings")}</h2>
    <label className="block text-sm font-semibold">{t("language")}
      <select aria-label={t("language")} value={locale} disabled={pending} className="mt-2 w-full rounded-xl bg-[var(--bg)] p-3"
        onChange={event => { const code = event.target.value; if (code !== locale) void save(() => setLocale(code)); }}>
        {!languageCodes.includes(locale) && <option value={locale} disabled>{t(`lang_${locale}`)}</option>}
        {languageCodes.map(code => <option key={code} value={code}>{t(`lang_${code}`)}</option>)}
      </select>
    </label>
    {typeof me.alerts_muted === "boolean" && <div className="mt-4 border-t border-[var(--line)] pt-4">
      <button type="button" role="switch" aria-checked={!me.alerts_muted} disabled={pending}
        aria-label={t("profile_alerts")} onClick={() => void save(() => setAlerts(!me.alerts_muted))}
        className="flex min-h-12 w-full items-center justify-between gap-3 text-start font-semibold disabled:opacity-50">
        <span>{t("profile_alerts")}</span><span>{t(me.alerts_muted ? "profile_off" : "profile_on")}</span>
      </button>
      <p className="mt-1 text-sm text-[var(--hint)]">{t("profile_alerts_hint")}</p>
    </div>}
    {me.preferences && <div className="mt-4 border-t border-[var(--line)] pt-4">
      <button type="button" role="switch" aria-checked={!me.preferences.marketing_opt_out} disabled={pending}
        aria-label={t("profile_marketing")} onClick={() => void save(() => setMarketing(!me.preferences.marketing_opt_out))}
        className="flex min-h-12 w-full items-center justify-between gap-3 text-start font-semibold disabled:opacity-50">
        <span>{t("profile_marketing")}</span><span>{t(me.preferences.marketing_opt_out ? "profile_off" : "profile_on")}</span>
      </button>
      <p className="mt-1 text-sm text-[var(--hint)]">{t("marketing_hint")}</p>
    </div>}
    <p className="sr-only" role="status">{pending ? t("profile_saving") : ""}</p>
  </section>;
}
