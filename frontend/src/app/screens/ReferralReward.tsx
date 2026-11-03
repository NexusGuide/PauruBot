import { useRef, useState } from "react";
import { money } from "@shared/format";
import { api, type ReferralConfig } from "../api";
import { errorText, useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Panel } from "../ui";

/** A free config for every N counted invites: what it is, how far along, and taking it. */
export function ReferralReward({ config }: { config: ReferralConfig }) {
  const { t, locale, toast, reload } = useApp();
  const [busy, setBusy] = useState(false);
  // A double tap lands before the button re-renders as busy: only the first one may claim.
  const claiming = useRef(false);
  if (!config.reward) return null;
  const reward = config.reward.plan_title
    ?? t("referral_config_size", { gb: money(config.reward.gb, locale), days: money(config.reward.days, locale) });
  const claim = async () => {
    if (claiming.current) return;
    claiming.current = true;
    setBusy(true);
    try {
      await api.claimReferral();
      haptic("success");
      toast(t("referral_config_claimed"));
      await reload();
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      claiming.current = false;
      setBusy(false);
    }
  };
  const ratio = config.every ? Math.min(1, config.progress / config.every) : 0;
  return (
    <Panel className="mb-4">
      <p className="mb-1 text-sm font-bold">{t("referral_config_title")}</p>
      <p className="text-sm">
        {t(config.counts === "join" ? "referral_config_rule_join" : "referral_config_rule", {
          every: money(config.every, locale), reward,
        })}
      </p>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--line)]" role="progressbar"
        aria-valuemin={0} aria-valuemax={config.every} aria-valuenow={config.progress} aria-label={t("referral_config_title")}>
        <div className="h-full rounded-full bg-[var(--accent)]" style={{ width: `${ratio * 100}%` }} />
      </div>
      <p className="mt-1 text-xs text-[var(--hint)]">
        {t("referral_config_progress", { progress: money(config.progress, locale), every: money(config.every, locale) })}
      </p>
      {config.available > 0 && (
        <div className="mt-3">
          <p className="mb-2 text-sm font-semibold">{t("referral_config_available", { available: money(config.available, locale) })}</p>
          <Button busy={busy} disabled={busy} onClick={() => void claim()}>{t("referral_config_claim")}</Button>
        </div>
      )}
    </Panel>
  );
}
