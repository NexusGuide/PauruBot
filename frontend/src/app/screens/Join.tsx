import { useState } from "react";
import { errorText, useApp } from "../state";
import { Button, Screen } from "../ui";

/** The join gate that stands between the customer and the rest of the app (GRW-07). */
export function Join() {
  const { t, join, checkJoin, toast } = useApp();
  const [busy, setBusy] = useState(false);
  const channels = join?.channels ?? [];

  const check = async () => {
    setBusy(true);
    try {
      if (!(await checkJoin())) toast(t("join_not_yet"), "error");
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen>
      <div className="flex min-h-[70dvh] flex-col items-center justify-center gap-4 px-2 text-center">
        <p className="text-xl font-bold">{t("join_title")}</p>
        <p className="max-w-xs text-sm leading-relaxed text-[var(--hint)]">{t("join_hint")}</p>
        <div className="flex w-full max-w-xs flex-col gap-2">
          {channels.map((channel, index) => (
            <a
              key={`${channel.title}-${index}`}
              href={channel.url || "https://t.me/"}
              target="_blank"
              rel="noreferrer"
              className="app-rip-host flex min-h-12 items-center justify-between gap-3 rounded-2xl bg-[var(--surface)] px-4 text-[0.95rem] font-semibold ring-1 ring-[var(--line)] active:opacity-80"
            >
              <span>{channel.title || t("join_channel")}</span>
              <span aria-hidden="true">↗</span>
            </a>
          ))}
        </div>
        <div className="w-full max-w-xs">
          <Button busy={busy} disabled={channels.length === 0} onClick={() => void check()}>
            {t("join_check")}
          </Button>
        </div>
      </div>
    </Screen>
  );
}
