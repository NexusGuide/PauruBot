import { useEffect, useState } from "react";
import { money } from "@shared/format";
import { api, type LotteryInfo, type WheelInfo, type WheelSpinResult } from "../api";
import { Icon } from "../icons";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { Button } from "../ui";

export function FunCards() {
  const { t, locale, reload, toast } = useApp();
  const [wheel, setWheel] = useState<WheelInfo | null>(null);
  const [lottery, setLottery] = useState<LotteryInfo | null>(null);
  const [spinning, setSpinning] = useState(false);
  const [result, setResult] = useState<WheelSpinResult | null>(null);
  const [joining, setJoining] = useState(false);

  useEffect(() => {
    let active = true;
    void api.wheel().then((d) => {
      if (active) setWheel(d);
    }).catch(() => {});
    void api.lottery().then((d) => {
      if (active) setLottery(d.lottery);
    }).catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  const spin = async () => {
    if (spinning || !wheel?.can_spin) return;
    setSpinning(true);
    haptic("select");
    try {
      const res = await api.spinWheel();
      setResult(res);
      setWheel((prev) =>
        prev ? { ...prev, can_spin: false, reason: "wheel_cooldown", next_spin_at: res.next_spin_at } : prev,
      );
      if (res.won) haptic("success");
      await reload();
    } catch {
      haptic("error");
      toast(t("wheel_error"), "error");
    } finally {
      setSpinning(false);
    }
  };

  const joinLottery = async () => {
    if (joining || !lottery || lottery.joined) return;
    setJoining(true);
    haptic("select");
    try {
      await api.joinLottery();
      const updated = await api.lottery();
      setLottery(updated.lottery);
      haptic("success");
      toast(t("lottery_joined_toast"));
    } catch {
      haptic("error");
      toast(t("lottery_join_error"), "error");
    } finally {
      setJoining(false);
    }
  };

  const showWheel = Boolean(wheel?.enabled);
  const showLottery = Boolean(lottery);
  if (!showWheel && !showLottery) return null;

  return (
    <div className="mb-4 flex flex-col gap-3">
      {showWheel && wheel && (
        <div className="app-card p-4">
          <div className="flex items-center gap-3">
            <span className="app-ico" data-tone="orange">
              <Icon name="gift" />
            </span>
            <div className="app-grow min-w-0">
              <p className="text-[0.96rem] font-extrabold">{t("wheel_title")}</p>
              <p className="app-hint text-[0.8rem]">
                {wheel.can_spin
                  ? t("wheel_ready")
                  : wheel.reason === "wheel_buyers_only"
                    ? t("wheel_buyers_only")
                    : wheel.next_spin_at
                      ? t("wheel_next_at", {
                          time: new Date(wheel.next_spin_at).toLocaleString(locale, {
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                          }),
                        })
                      : t("wheel_cooldown")}
              </p>
            </div>
            <Button
              type="button"
              className="!w-auto shrink-0 !px-4 !py-2 !text-[0.84rem]"
              busy={spinning}
              disabled={!wheel.can_spin || spinning}
              onClick={() => void spin()}
            >
              {t("wheel_spin_btn")}
            </Button>
          </div>
          {result && (
            <div className="mt-3 rounded-xl bg-[var(--surface-2,rgba(127,127,127,0.1))] px-3 py-2.5 text-[0.86rem]">
              <p className="font-bold">
                {result.won
                  ? t("wheel_won", { label: result.prize.label })
                  : t("wheel_lost")}
              </p>
              {result.code && (
                <div className="mt-2 flex items-center justify-between gap-2">
                  <code className="font-mono text-[0.9rem] font-bold" dir="ltr">
                    {result.code}
                  </code>
                  <Button
                    type="button"
                    tone="quiet"
                    className="!w-auto !px-3 !py-1.5 !text-[0.8rem]"
                    onClick={() => {
                      void navigator.clipboard.writeText(result.code ?? "");
                      toast(t("copied"));
                    }}
                  >
                    {t("copy")}
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {showLottery && lottery && (
        <div className="app-card p-4">
          <div className="flex items-start gap-3">
            <span className="app-ico" data-tone="green">
              <Icon name="dice" />
            </span>
            <div className="app-grow min-w-0">
              <p className="text-[0.96rem] font-extrabold">{lottery.title}</p>
              <p className="app-hint text-[0.8rem]">
                {t("lottery_meta", {
                  entrants: money(lottery.entrants, locale),
                  ends: new Date(lottery.ends_at).toLocaleDateString(locale, {
                    month: "short",
                    day: "numeric",
                  }),
                })}
              </p>
            </div>
            {lottery.mode === "join" && !lottery.joined ? (
              <Button
                type="button"
                className="!w-auto shrink-0 !px-4 !py-2 !text-[0.84rem]"
                busy={joining}
                disabled={joining}
                onClick={() => void joinLottery()}
              >
                {t("lottery_join_btn")}
              </Button>
            ) : lottery.joined ? (
              <span className="app-pill shrink-0">{t("lottery_joined_badge")}</span>
            ) : null}
          </div>
          {lottery.my_rank !== null && (
            <p className="mt-2 text-[0.84rem] font-semibold">
              {t("lottery_my_rank", {
                rank: money(lottery.my_rank, locale),
                points: money(lottery.my_points, locale),
              })}
            </p>
          )}
          {lottery.board.length > 0 && (
            <ul className="mt-2 divide-y divide-black/5 text-[0.82rem] dark:divide-white/10">
              {lottery.board.slice(0, 5).map((row) => (
                <li
                  key={row.rank}
                  className={`flex items-center justify-between py-1.5 ${row.is_me ? "font-bold" : ""}`}
                >
                  <span>
                    #{row.rank} {row.name}
                  </span>
                  <span className="num">{money(row.points, locale)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
