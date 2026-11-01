import { useState } from "react";
import { api } from "../api";
import { errorText, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Button, Screen } from "../ui";

const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** The phone and terms gate (SEC-F11): what the shop requires before anything else, and the way through. */
export function Gate() {
  const { t, gate, checkGate, toast } = useApp();
  const [busy, setBusy] = useState(false);
  const requirement = gate?.requirement ?? null;

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  // Telegram hands the contact to the bot, which saves it; give it a moment, then ask again.
  const settle = async () => {
    for (let attempt = 0; attempt < 5; attempt++) {
      if (await checkGate()) return true;
      await wait(1200);
    }
    return false;
  };

  const sharePhone = () =>
    run(async () => {
      if (!tg?.requestContact) {
        openBot();
        return;
      }
      const sent = await new Promise<boolean>((resolve) => tg?.requestContact?.((ok) => resolve(Boolean(ok))));
      if (!sent) {
        toast(t("gate_phone_cancelled"), "error");
        return;
      }
      if (!(await settle())) toast(t("gate_phone_not_yet"), "error");
    });

  const openBot = () => {
    const bot = gate?.bot_username;
    if (bot && tg) tg.openTelegramLink(`https://t.me/${bot}`);
    else if (tg) tg.close();
  };

  const check = () =>
    run(async () => {
      if (!(await checkGate())) toast(t(requirement === "terms_required" ? "gate_terms_not_yet" : "gate_phone_not_yet"), "error");
    });

  const accept = () =>
    run(async () => {
      const version = gate?.terms?.version;
      if (!version) {
        await checkGate();
        return;
      }
      try {
        if (await checkGate(await api.acceptTerms(version))) haptic("success");
      } catch (err) {
        if (err instanceof Error && err.message === "terms_changed") {
          await checkGate(); // show the new text; acceptance is only for what was read
          toast(t("err_terms_changed"), "error");
          return;
        }
        throw err;
      }
    });

  if (requirement === "terms_required") {
    return (
      <Screen>
        <div className="flex min-h-[70dvh] flex-col items-center justify-center gap-4 px-2">
          <p className="text-xl font-bold">{t("gate_terms_title")}</p>
          <div
            className="max-h-[50dvh] w-full max-w-md overflow-y-auto whitespace-pre-line rounded-2xl bg-[var(--surface)] p-4 text-sm leading-relaxed ring-1 ring-[var(--line)]"
            tabIndex={0}
            aria-label={t("gate_terms_title")}
          >
            {gate?.terms?.text || t("gate_terms_missing")}
          </div>
          <div className="w-full max-w-xs">
            <Button busy={busy} disabled={!gate?.terms?.version} onClick={() => void accept()}>
              {t("gate_terms_accept")}
            </Button>
          </div>
        </div>
      </Screen>
    );
  }

  return (
    <Screen>
      <div className="flex min-h-[70dvh] flex-col items-center justify-center gap-4 px-2 text-center">
        <p className="text-xl font-bold">{t("gate_phone_title")}</p>
        <p className="max-w-xs text-sm leading-relaxed text-[var(--hint)]">{t("gate_phone_hint")}</p>
        <div className="flex w-full max-w-xs flex-col gap-2">
          <Button busy={busy} onClick={() => void sharePhone()}>
            {t("gate_phone_share")}
          </Button>
          {gate?.bot_username && (
            <Button tone="quiet" disabled={busy} onClick={openBot}>
              {t("gate_open_bot")}
            </Button>
          )}
          <Button tone="quiet" disabled={busy} onClick={() => void check()}>
            {t("gate_check")}
          </Button>
        </div>
      </div>
    </Screen>
  );
}
