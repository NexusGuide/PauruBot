// Subscription-link domains and their certificates: add a domain, see whether DNS, the certificate
// and the route to this server are right, and pick the one the shop's links use.

import { useState } from "react";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Empty, Field, Icon, Input, Page, Section, Sheet, When, useConfirm } from "../ui";

export interface DomainRow {
  id: number;
  host: string;
  for_links: boolean;
  status: "pending" | "ok" | "dns" | "tls" | "unreachable" | "not_ours";
  error: string | null;
  checked_at: string | null;
  cert_expires_at: string | null;
  cert_issuer: string | null;
}

interface DomainsData {
  main: string | null;
  domains: DomainRow[];
  limit: number;
}

const TONE: Record<DomainRow["status"], "health" | "wait" | "loss"> = {
  ok: "health",
  pending: "wait",
  dns: "loss",
  tls: "loss",
  unreachable: "loss",
  not_ours: "loss",
};

/** Whole days until a certificate expires; negative once it has. */
export function daysLeft(iso: string | null, now: number = Date.now()): number | null {
  if (!iso) return null;
  return Math.floor((new Date(iso).getTime() - now) / 86_400_000);
}

function DomainCard({ row, owner, onChange }: { row: DomainRow; owner: boolean; onChange: () => void }) {
  const { t, toast, errorText } = useSession();
  const confirm = useConfirm();
  const [busy, run] = useAction();
  const days = daysLeft(row.cert_expires_at);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    try {
      await run(fn);
      if (done) toast(done);
      onChange();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: t("domain_remove_title", { host: row.host }),
      body: t(row.for_links ? "domain_remove_links_body" : "domain_remove_body"),
      confirm: t("delete"),
      danger: true,
    });
    if (ok) await act(() => call(`/api/admin/domains/${row.id}`, { method: "DELETE" }), t("removed"));
  };

  return (
    <article className="flex flex-col gap-3 rounded-2xl bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
      <header className="flex items-start gap-3">
        <span
          className={`grid size-10 shrink-0 place-items-center rounded-xl ${
            row.status === "ok" ? "bg-pista-100 text-pista-700" : row.status === "pending" ? "bg-saffron-100 text-saffron-700" : "bg-anar-100 text-anar-700"
          }`}
        >
          <Icon name={row.status === "ok" ? "lock" : "globe"} size={20} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-bold">
            <bdi dir="ltr">{row.host}</bdi>
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge tone={TONE[row.status]}>{t(`domain_status_${row.status}`)}</Badge>
            {row.for_links && <Badge tone="brand">{t("domain_for_links")}</Badge>}
          </div>
        </div>
      </header>

      {row.status !== "ok" && (
        <p className="rounded-xl bg-[var(--bg)] px-3 py-2 text-[0.8rem] leading-relaxed text-[var(--hint)]">
          {t(`domain_fix_${row.status}`)}
          {row.error && (
            <span className="mt-1 block font-mono text-[0.74rem]" dir="ltr">
              {row.error}
            </span>
          )}
        </p>
      )}

      <dl className="grid grid-cols-2 gap-2 text-[0.78rem]">
        <div>
          <dt className="text-[var(--hint)]">{t("domain_cert")}</dt>
          <dd className="font-bold">
            {row.cert_expires_at ? (
              <span className={days !== null && days < 10 ? "text-anar-700" : ""}>
                {t("domain_cert_days", { n: Math.max(0, days ?? 0) })}
                {row.cert_issuer && <span className="font-normal text-[var(--hint)]"> · {row.cert_issuer}</span>}
              </span>
            ) : (
              "—"
            )}
          </dd>
        </div>
        <div>
          <dt className="text-[var(--hint)]">{t("conn_last_check")}</dt>
          <dd className="font-bold">{row.checked_at ? <When iso={row.checked_at} time /> : t("domain_never_checked")}</dd>
        </div>
      </dl>

      {owner && (
        <footer className="flex flex-wrap gap-2 border-t border-[var(--line)] pt-3">
          <Button small icon="refresh" busy={busy} onClick={() => void act(() => call(`/api/admin/domains/${row.id}/check`, { method: "POST" }))}>
            {t("domain_check_now")}
          </Button>
          {!row.for_links && row.status === "ok" && (
            <Button
              small
              tone="primary"
              icon="link"
              busy={busy}
              onClick={() => void act(() => call("/api/admin/domains/links", { method: "PUT", body: { domain_id: row.id } }), t("saved"))}
            >
              {t("domain_use_for_links")}
            </Button>
          )}
          <span className="flex-1" />
          <Button small tone="ghost" icon="trash" busy={busy} onClick={() => void remove()}>
            {t("delete")}
          </Button>
        </footer>
      )}
    </article>
  );
}

function AddDomain({ main, onClose, onSaved }: { main: string | null; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [host, setHost] = useState("");
  const [busy, run] = useAction();
  const save = async () => {
    try {
      const row = await run(() => call<DomainRow>("/api/admin/domains", { method: "POST", body: { host } }));
      if (!row) return;
      // Check right away: Caddy gets the certificate on this first visit.
      await call(`/api/admin/domains/${row.id}/check`, { method: "POST" }).catch(() => undefined);
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Sheet
      open
      onClose={onClose}
      title={t("domain_add")}
      footer={
        <Button tone="primary" busy={busy} disabled={!host.trim()} onClick={() => void save()}>
          {t("domain_add")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("domain_host")}>
          {(id) => (
            <Input
              id={id}
              dir="ltr"
              autoComplete="off"
              placeholder="sub.example.com"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && host.trim() && void save()}
            />
          )}
        </Field>
        <ol className="flex list-decimal flex-col gap-2 rounded-2xl bg-[var(--bg)] p-4 ps-8 text-[0.84rem] leading-relaxed ring-1 ring-[var(--line)]">
          <li>{t("domain_step_dns", { main: main ?? "—" })}</li>
          <li>{t("domain_step_add")}</li>
          <li>{t("domain_step_links")}</li>
        </ol>
      </div>
    </Sheet>
  );
}

export function Domains() {
  const { t, can } = useSession();
  const owner = can("panels.secrets");
  const loaded = useLoad<DomainsData>("/api/admin/domains");
  const [adding, setAdding] = useState(false);
  const { reload } = loaded;

  return (
    <Page
      title={t("nav_domains")}
      actions={
        owner && (
          <Button tone="primary" icon="plus" onClick={() => setAdding(true)}>
            {t("domain_add")}
          </Button>
        )
      }
    >
      <Async {...loaded}>
        {(data) => {
          const links = data.domains.find((d) => d.for_links && d.status === "ok");
          return (
            <>
              <Section title={t("domain_links_title")}>
                <div className="flex flex-col gap-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-firouzeh-100 text-firouzeh-700">
                      <Icon name="link" size={20} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-[0.8rem] text-[var(--hint)]">{t("domain_links_now")}</p>
                      <p className="break-all font-bold">
                        <bdi dir="ltr">https://{links?.host ?? data.main ?? "…"}/s/…</bdi>
                      </p>
                    </div>
                    {links && owner && (
                      <Button
                        small
                        tone="ghost"
                        onClick={() => void call("/api/admin/domains/links", { method: "PUT", body: { domain_id: null } }).then(() => reload())}
                      >
                        {t("domain_back_to_main")}
                      </Button>
                    )}
                  </div>
                  <p className="text-[0.8rem] leading-relaxed text-[var(--hint)]">{t("domain_links_hint")}</p>
                </div>
              </Section>

              <Section title={t("domain_main")}>
                <div className="flex items-center gap-3">
                  <span
                    className={`grid size-10 shrink-0 place-items-center rounded-xl ${
                      data.main ? "bg-pista-100 text-pista-700" : "bg-saffron-100 text-saffron-700"
                    }`}
                  >
                    <Icon name={data.main ? "lock" : "alert"} size={20} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-bold">
                      <bdi dir="ltr">{data.main ?? t("domain_main_missing")}</bdi>
                    </p>
                    <p className="text-[0.8rem] text-[var(--hint)]">{t(data.main ? "domain_main_hint" : "domain_main_missing_hint")}</p>
                  </div>
                </div>
              </Section>

              {data.domains.length === 0 ? (
                <Section>
                  <Empty
                    title={t("domain_empty")}
                    hint={t("domain_empty_hint")}
                    action={
                      owner && (
                        <Button tone="primary" icon="plus" onClick={() => setAdding(true)}>
                          {t("domain_add")}
                        </Button>
                      )
                    }
                  />
                </Section>
              ) : (
                <div className="grid grid-cols-1 items-start gap-3 md:grid-cols-2">
                  {data.domains.map((row) => (
                    <DomainCard key={row.id} row={row} owner={owner} onChange={() => void reload()} />
                  ))}
                </div>
              )}

              {adding && (
                <AddDomain
                  main={data.main}
                  onClose={() => setAdding(false)}
                  onSaved={() => {
                    setAdding(false);
                    void reload();
                  }}
                />
              )}
            </>
          );
        }}
      </Async>
    </Page>
  );
}
