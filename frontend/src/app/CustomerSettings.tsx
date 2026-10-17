import { useRef, useState } from "react";
import { errorText, useApp } from "./state";
import { haptic } from "./telegram";
import { Seg, Switch } from "./ui";

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

  const languageCodes: string[] = me.shop.languages.filter((code) => code === "fa" || code === "en");
  const options = languageCodes.map((code) => ({ value: code, label: t(`lang_${code}`) }));
  if (!languageCodes.includes(locale)) options.unshift({ value: locale, label: t(`lang_${locale}`) });

  return (
    <section className="app-card p-4" aria-label={t("profile_settings")} aria-busy={pending}>
      <h2 className="mb-3 font-bold">{t("profile_settings")}</h2>
      <p className="mb-2 text-sm font-semibold">{t("language")}</p>
      <Seg
        options={options}
        value={locale}
        disabled={pending}
        onChange={(code) => {
          if (code !== locale) void save(() => setLocale(code));
        }}
      />
      {typeof me.alerts_muted === "boolean" && (
        <div className="mt-4 border-t border-[var(--line)] pt-4">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold">{t("profile_alerts")}</span>
            <Switch
              checked={!me.alerts_muted}
              busy={pending}
              label={t("profile_alerts")}
              onChange={() => void save(() => setAlerts(!me.alerts_muted))}
            />
          </div>
          <p className="mt-1 text-sm text-[var(--hint)]">{t("profile_alerts_hint")}</p>
        </div>
      )}
      {me.preferences && (
        <div className="mt-4 border-t border-[var(--line)] pt-4">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold">{t("profile_marketing")}</span>
            <Switch
              checked={!me.preferences.marketing_opt_out}
              busy={pending}
              label={t("profile_marketing")}
              onChange={() => void save(() => setMarketing(!me.preferences.marketing_opt_out))}
            />
          </div>
          <p className="mt-1 text-sm text-[var(--hint)]">{t("marketing_hint")}</p>
        </div>
      )}
      <p className="sr-only" role="status">{pending ? t("profile_saving") : ""}</p>
    </section>
  );
}
