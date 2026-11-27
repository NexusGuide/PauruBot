import { useEffect, useRef, useState } from "react";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Facts, Field, Input, Page, Section, Select, useConfirm } from "../ui";

interface UpdateStatus {
  agent_online: boolean;
  app_version: string;
  installed: string | null;
  channel: string | null;
  previous: string | null;
  previous_channel?: string | null;
  has_pre_dev_backup?: boolean;
  latest: string | null;
  state: string | null;
  last_action: string | null;
  log: string | null;
  pending: Record<string, string> | null;
  update_available: boolean;
}

const STATE_TONE: Record<string, "health" | "wait" | "loss"> = { running: "wait", done: "health", failed: "loss" };

function displayVersion(value: UpdateStatus): string {
  if (!value.installed || value.installed === "dev" || value.installed === "main") {
    return value.app_version;
  }
  return value.installed;
}

export function Updates() {
  const { t, toast, errorText } = useSession();
  const confirm = useConfirm();
  const { data, error, reload } = useLoad<UpdateStatus>("/api/admin/updates");
  const [channel, setChannel] = useState("");
  const [version, setVersion] = useState("");
  const [busy, run] = useAction();

  const busyOnServer = Boolean(data?.pending) || data?.state === "running";
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    if (!busyOnServer) return;
    const timer = window.setInterval(() => void reloadRef.current(), 5000);
    return () => window.clearInterval(timer);
  }, [busyOnServer]);

  const send = async (body: Record<string, string>) => {
    try {
      await run(() => call("/api/admin/updates", { method: "POST", body }));
      toast(t("updates_requested"));
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const update = async (value: UpdateStatus) => {
    const target = version.trim() || (channel && channel !== value.channel ? t(`updates_channel_${channel}`) : value.latest);
    const ok = await confirm({
      title: t("updates_confirm_title"),
      body: t("updates_confirm_body", { target: target ?? "" }),
      confirm: t("updates_update"),
    });
    if (!ok) return;
    const body: Record<string, string> = { action: "update" };
    if (channel) body.channel = channel;
    if (version.trim()) body.version = version.trim();
    await send(body);
  };

  const rollback = async (value: UpdateStatus) => {
    const prevLabel = value.previous_channel
      ? `${value.previous ?? ""} (${t(`updates_channel_${value.previous_channel}`)})`
      : (value.previous ?? "");
    const ok = await confirm({
      title: t("updates_rollback_title"),
      body: t("updates_rollback_body", { version: prevLabel }),
      confirm: t("updates_rollback"),
      danger: true,
    });
    if (ok) await send({ action: "rollback" });
  };

  const describe = (action: string | null) => {
    if (!action) return "";
    const [kind, ...rest] = action.split(" ");
    const target = rest.join(" ");
    return `${t(`updates_action_${kind}`)}${target ? ` ${target}` : ""}`;
  };

  const cancel = async () => {
    try {
      await run(() => call("/api/admin/updates", { method: "DELETE" }));
      await reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_updates")}
      actions={
        <Button icon="refresh" onClick={() => void reload()}>
          {t("check_now")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => {
          const switchingDevToMain = value.channel === "dev" && (channel === "stable" || channel === "beta");
          const switchingBetaToStable = value.channel === "beta" && channel === "stable";
          const switchingAwayFromNewer = switchingDevToMain || switchingBetaToStable;
          const switchingMainToDev = value.channel !== "dev" && channel === "dev";
          const previousText = value.previous
            ? value.previous_channel
              ? `${value.previous} (${t(`updates_channel_${value.previous_channel}`)})`
              : value.previous
            : "—";
          return (
            <>
              {!value.agent_online && (
                <Section>
                  <p className="font-semibold">{t("updates_agent_off")}</p>
                  <p className="mt-2 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("updates_agent_hint")}</p>
                  <code className="mt-3 block rounded-xl bg-[var(--bg)] ring-1 ring-[var(--line)] px-3 py-2 text-[0.85rem]" dir="ltr">
                    pauru enable-agent
                  </code>
                </Section>
              )}

              <Section title={t("updates_versions")}>
                <Facts
                  items={[
                    [t("updates_installed"), <span dir="ltr" key="i">{displayVersion(value)}</span>],
                    [t("updates_channel"), value.channel ? t(`updates_channel_${value.channel}`) : "—"],
                    [t("updates_latest"), <span dir="ltr" key="l">{value.latest ?? "—"}</span>],
                    [t("updates_previous"), <span dir="ltr" key="p">{previousText}</span>],
                  ]}
                />
                {value.update_available && (
                  <p className="mt-4 rounded-2xl bg-firouzeh-50 px-4 py-3 text-[0.88rem] font-semibold text-firouzeh-700">
                    {t("updates_available", { version: value.latest ?? "" })}
                  </p>
                )}
                {(value.channel === "dev" || value.channel === "beta") && value.has_pre_dev_backup && (
                  <p className="mt-3 text-[0.82rem] leading-relaxed text-[var(--hint)]">
                    {t("updates_pre_dev_backup_ready")}
                  </p>
                )}
              </Section>

              {(value.pending || value.state) && (
                <Section title={t("updates_progress")}>
                  {value.pending ? (
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p>{t("updates_waiting", { action: t(`updates_action_${value.pending.action}`) })}</p>
                      <Button busy={busy} onClick={() => void cancel()}>
                        {t("cancel")}
                      </Button>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p>{describe(value.last_action)}</p>
                      <Badge tone={STATE_TONE[value.state ?? ""] ?? "neutral"}>{t(`updates_state_${value.state}`)}</Badge>
                    </div>
                  )}
                  {value.log && !value.pending && (
                    <pre
                      className="mt-3 max-h-72 overflow-auto rounded-xl bg-[var(--bg)] ring-1 ring-[var(--line)] p-3 text-[0.75rem] leading-relaxed whitespace-pre-wrap break-words"
                      dir="ltr"
                    >
                      {value.log}
                    </pre>
                  )}
                </Section>
              )}

              <Section title={t("updates_update")}>
                <p className="mb-4 text-[0.85rem] leading-relaxed text-[var(--hint)]">{t("updates_update_hint")}</p>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Field label={t("updates_channel")}>
                    {(id) => (
                      <Select id={id} value={channel} onChange={(e) => setChannel(e.target.value)}>
                        <option value="">{t("updates_keep_channel")}</option>
                        <option value="stable">{t("updates_channel_stable")}</option>
                        <option value="beta">{t("updates_channel_beta")}</option>
                        <option value="dev">{t("updates_channel_dev")}</option>
                      </Select>
                    )}
                  </Field>
                  <Field label={t("updates_version")} hint={t("updates_version_hint")}>
                    {(id) => (
                      <Input id={id} dir="ltr" value={version} placeholder="v1.2.3" onChange={(e) => setVersion(e.target.value)} />
                    )}
                  </Field>
                </div>
                {switchingMainToDev && (
                  <p className="mt-3 rounded-xl bg-[var(--bg)] ring-1 ring-[var(--line)] px-3 py-2.5 text-[0.82rem] leading-relaxed text-[var(--hint)]">
                    {t("updates_main_to_dev_info")}
                  </p>
                )}
                {switchingAwayFromNewer && (
                  <div className="mt-3 rounded-xl bg-amber-500/10 ring-1 ring-amber-500/30 px-3 py-2.5 text-[0.82rem] leading-relaxed">
                    <p>{t("updates_dev_to_main_warn", { target: t(`updates_channel_${channel}`) })}</p>
                    <code className="mt-2 block rounded-lg bg-[var(--bg)] ring-1 ring-[var(--line)] px-2.5 py-1.5 text-[0.8rem]" dir="ltr">
                      {`pauru update --channel ${channel}`}
                    </code>
                  </div>
                )}
                <div className="mt-5 flex flex-wrap gap-2">
                  <Button
                    tone="primary"
                    busy={busy}
                    disabled={!value.agent_online || busyOnServer}
                    onClick={() => void update(value)}
                  >
                    {t("updates_update")}
                  </Button>
                  <Button
                    tone="danger"
                    busy={busy}
                    disabled={!value.agent_online || busyOnServer || !value.previous}
                    onClick={() => void rollback(value)}
                  >
                    {t("updates_rollback")}
                  </Button>
                </div>
                <p className="mt-3 text-[0.8rem] leading-relaxed text-[var(--hint)]">{t("updates_safety")}</p>
              </Section>
            </>
          );
        }}
      </Async>
    </Page>
  );
}
