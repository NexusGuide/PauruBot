import { useCallback, useEffect, useId, useRef, useState } from "react";
import { money } from "@shared/format";
import { call } from "../api";
import { useSession } from "../session";
import { Sheet } from "../ui";
import { copy } from "./copy";
import { hubPath, mayRead, parseHub, type Event, type Hub, type Mode } from "./model";
import "./hub.css";
export function ResellerHub({ mode, profileId }: {
    mode: Mode;
    profileId?: number;
}) {
    const { me, can, locale, checking } = useSession();
    const c = copy(locale);
    const path = hubPath(mode, profileId);
    const allowed = mayRead(mode, me?.role, can(mode === "own" ? "dashboard.view" : "resellers.manage"));
    if (checking)
        return <div className="rh" role="status">{c.loading}</div>;
    if (!allowed || !path)
        return <div className="rh" role="alert">{c.denied}</div>;
    return <HubBody key={`${mode}:${path}:${me?.id}`} mode={mode} path={path}/>;
}
function HubBody({ mode, path }: {
    mode: Mode;
    path: string;
}) {
    const { locale } = useSession();
    const c = copy(locale);
    const id = useId();
    const [snapshot, setSnapshot] = useState<{
        hub: Hub;
        received: string;
    } | null>(null);
    const [busy, setBusy] = useState(true), [error, setError] = useState(false), [version, setVersion] = useState(0);
    const [filter, setFilter] = useState("all");
    useEffect(() => {
        let active = true;
        setBusy(true);
        setError(false);
        setSnapshot(null);
        void call<unknown>(path).then(raw => { const hub = parseHub(raw, mode); if (active)
            setSnapshot({ hub, received: new Date().toISOString() }); }).catch(() => { if (active)
            setError(true); }).finally(() => { if (active)
            setBusy(false); });
        return () => { active = false; };
    }, [path, mode, version]);
    const stamp = (value: string | null) => value ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : c.unavailable;
    const currency = (n: number) => `${money(n, locale)} ${c.unit}`;
    const events = snapshot?.hub.timeline.filter(e => filter === "all" || (filter === "other" ? !["ledger", "audit"].includes(e.type) : e.type === filter)) ?? [];
    return <section className="rh" aria-label={c.title} aria-busy={busy}>
  <header className="rh-toolbar"><h2>{c.title}</h2><button type="button" disabled={busy} onClick={() => setVersion(v => v + 1)}>{c.refresh}</button></header>
  <p className="rh-muted">{c.readonly}</p>
  {busy && <p role="status">{c.loading}</p>}
  {error && <p className="rh-error" role="alert">{c.error}</p>}
  {snapshot && <>
   <p className="rh-muted">{c.snapshot}: <time dateTime={snapshot.received}>{stamp(snapshot.received)}</time></p>
   <div className="rh-metrics">{([[c.balance, snapshot.hub.runway.balance], [c.debt, snapshot.hub.runway.debt], [c.spend, snapshot.hub.runway.daily_spend]] as const).map(([label, n]) => <div className="rh-card" key={label}><span className="rh-muted">{label}</span><strong>{currency(n)}</strong></div>)}</div>
   <section className={`rh-card rh-runway ${snapshot.hub.runway.low ? "rh-low" : ""}`}><h3>{c.estimate}</h3><strong className="rh-number">{snapshot.hub.runway.days === null ? c.none : snapshot.hub.runway.days < 1 ? (snapshot.hub.runway.hours === 0 && snapshot.hub.runway.days > 0 ? c.lessHour : `${money(snapshot.hub.runway.hours ?? 0, locale)} ${c.hours}`) : `${new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(snapshot.hub.runway.days)} ${c.days}`}</strong><p>{snapshot.hub.runway.low ? c.low : c.normal}</p><p className="rh-muted">{c.method.replace("{n}", money(snapshot.hub.runway.window_days, locale))}</p></section>
   <section><h3>{c.timeline}</h3><p className="rh-muted">{c.scope}</p><p className="rh-muted">{mode === "own" ? c.ownPrivacy : c.staffPrivacy}</p>
    <label htmlFor={id}>{c.filter}</label><select id={id} value={filter} onChange={e => setFilter(e.target.value)}>{["all", "ledger", "audit", "other"].map(k => <option key={k} value={k}>{c[k as "all" | "ledger" | "audit" | "other"]}</option>)}</select>
    {events.length === 0 ? <p className="rh-card">{snapshot.hub.timeline.length === 0 ? c.empty : c.noMatch}</p> : <ol className="rh-events">{events.map((e, i) => <li key={`${e.type}:${e.at}:${i}`}><article className="rh-card"><header><span className="rh-tag">{e.type === "ledger" ? c.ledger : e.type === "audit" ? c.audit : c.other}</span><h4><bdi>{e.kind}</bdi></h4><time dateTime={e.at ?? undefined}>{stamp(e.at)}</time></header>
    {e.type === "ledger" && <dl><dt>{c.amount}</dt><dd dir="auto">{currency(e.amount!)}</dd><dt>{c.afterBalance}</dt><dd>{currency(e.balance_after!)}</dd>{e.memo && <><dt>{c.memo}</dt><dd dir="auto">{e.memo}</dd></>}{e.reference_type && <><dt>{c.reference}</dt><dd><bdi>{e.reference_type}{e.reference_id !== null ? ` #${e.reference_id}` : ""}</bdi></dd></>}</dl>}
    {mode === "staff" && e.type === "audit" && <AuditDetails event={e}/>}
    </article></li>)}</ol>}
   </section>
  </>}
 </section>;
}
function AuditDetails({ event: e }: {
    event: Event;
}) {
    const { locale } = useSession();
    const c = copy(locale);
    const keys = Array.from(new Set([...Object.keys(e.before ?? {}), ...Object.keys(e.after ?? {})]));
    const value = (v: unknown) => v === undefined ? c.unavailable : v === null ? "—" : typeof v === "boolean" ? (v ? c.true : c.false) : String(v);
    return <>{(e.actor_type || e.actor_id !== null) && <p>{c.actor}: <bdi>{e.actor_type ?? c.unavailable}{e.actor_id !== null ? ` #${e.actor_id}` : ""}</bdi></p>}{keys.length > 0 && <details><summary>{c.details}</summary><dl>{keys.map(k => <div key={k}><dt><bdi>{k}</bdi></dt><dd>{c.before}: <bdi>{value(e.before?.[k])}</bdi> → {c.after}: <bdi>{value(e.after?.[k])}</bdi></dd></div>)}</dl></details>}</>;
}
/** Owns a stable Sheet close handler; renders no editor or mutation controls. */
export function StaffHubButton({ profileId, name }: {
    profileId: number;
    name: string;
}) {
    const { me, can, locale } = useSession();
    const c = copy(locale);
    const [open, setOpen] = useState(false);
    const root = useRef<HTMLDivElement>(null);
    const trap = (e: import("react").KeyboardEvent) => { if (e.key !== "Tab")
        return; const dialog = root.current?.querySelector('[role="dialog"]'); const els = dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),select,summary,[tabindex="0"]'); if (!els?.length)
        return; const first = els[0], last = els[els.length - 1]; if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        e.preventDefault();
        last?.focus();
    }
    else if (!e.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        e.preventDefault();
        first?.focus();
    } };
    const close = useCallback(() => setOpen(false), []);
    if (!mayRead("staff", me?.role, can("resellers.manage")) || !hubPath("staff", profileId))
        return null;
    return <><div className="rh rh-entry"><button type="button" onClick={() => setOpen(true)} aria-label={`${c.open} — ${name}`}>{c.open}</button></div>{open && <div className="rh-modal" ref={root} onKeyDown={trap}><Sheet open title={`${c.title} — ${name}`} onClose={close}><ResellerHub mode="staff" profileId={profileId}/></Sheet></div>}</>;
}
