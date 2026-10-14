import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, call } from "../api";
import { useSession } from "../session";
import { Sheet } from "../ui";
import { AUDIENCE_KINDS, audiencePreviewPath, previewReady, submitPath, actions, audienceKey, buildBody, codepoints, emptyDraft, parsePage, parseReachable, parseRow, type Action, type Body, type BroadcastRow, type Draft } from "./model";
import { translator, type CopyKey } from "./copy";
import "./broadcasts.css";
type T = ReturnType<typeof translator>;
function useRead<TData>(path: string, parse: (v: unknown) => TData) {
    const [data, setData] = useState<TData | null>(null), [error, setError] = useState(false), [loading, setLoading] = useState(true);
    const [dataPath, setDataPath] = useState<string | null>(null);
    const seq = useRef(0);
    const reload = useCallback(async () => {
        const current = ++seq.current;
        setLoading(true);
        setError(false);
        try {
            const value = parse(await call<unknown>(path));
            if (current === seq.current) {
                setData(value);
                setDataPath(path);
            }
        }
        catch {
            if (current === seq.current)
                setError(true);
        }
        finally {
            if (current === seq.current)
                setLoading(false);
        }
    }, [path, parse]);
    useEffect(() => { setData(null); void reload(); return () => { seq.current++; }; }, [reload]);
    return { data, dataPath, error, loading, reload };
}
function Modal({ title, onClose, children }: {
    title: string;
    onClose: () => void;
    children: ReactNode;
}) {
    const root = useRef<HTMLDivElement>(null);
    useEffect(() => {
        const trap = (event: KeyboardEvent) => {
            if (event.key !== "Tab")
                return;
            const dialog = root.current?.closest('[role="dialog"]');
            const nodes = Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]') ?? []).filter(e => e.getClientRects().length > 0);
            const first = nodes[0], last = nodes[nodes.length - 1];
            if (!first || !last)
                return;
            if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener('keydown', trap);
        return () => document.removeEventListener('keydown', trap);
    }, []);
    return <div className="bc-layer"><Sheet open title={title} onClose={onClose}><div className="bc-modal" ref={root}>{children}</div></Sheet></div>;
}
function Notice({ text, error = false }: {
    text: string;
    error?: boolean;
}) {
    const ref = useRef<HTMLDivElement>(null);
    useEffect(() => { ref.current?.focus(); ref.current?.scrollIntoView({ block: "nearest" }); }, [text]);
    return <div ref={ref} tabIndex={-1} role={error ? "alert" : "status"} className={`bc-note ${error ? "bc-error" : ""}`}>{text}</div>;
}
function stamp(value: string | null, locale: string): string {
    if (!value)
        return "—";
    return new Intl.DateTimeFormat(locale === "fa" ? "fa-IR" : "en-GB", { dateStyle: "medium", timeStyle: "short", timeZoneName: undefined }).format(new Date(value));
}
function statusKey(status: string): CopyKey | null {
    return ({ queued: "queuedStatus", running: "running", paused: "paused", done: "done", canceled: "canceled" } as Record<string, CopyKey>)[status] ?? null;
}
function errorKind(error: unknown): "uncertain" | "rejected" {
    return error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 ? "rejected" : "uncertain";
}
function Recovery({ t, onChecked }: {
    t: T;
    onChecked: () => void;
}) {
    const history = useRead("/api/admin/broadcasts?limit=25&offset=0", parsePage);
    return <section className="bc-note"><p>{t("recovery")}</p>
    <button disabled={history.loading} onClick={() => void history.reload()}>{t("refresh")}</button>
    {history.loading && <p role="status">{t("loading")}</p>}
    {history.error && <p role="alert">{t("loadError")}</p>}
    {!history.loading && !history.error && history.data && <>
      {history.data.items.length === 0 ? <p>{t("empty")}</p> : <ul>{history.data.items.map(row => <li key={row.id}>#{row.id} · <bdi>{row.status}</bdi> · <bdi>{row.created_at}</bdi><pre dir="auto">{row.text}</pre></li>)}</ul>}
      <button onClick={onChecked}>{t("checked")}</button>
    </>}
  </section>;
}
function Composer({ t, locale, onClose, onSaved }: {
    t: T;
    locale: string;
    onClose: () => void;
    onSaved: () => void;
}) {
    const { errorText } = useSession();
    const [draft, setDraft] = useState<Draft>(emptyDraft), [review, setReview] = useState<{
        kind: "test" | "create";
        body: Body;
    } | null>(null);
    const previewPath = audiencePreviewPath(draft);
    const preview = useRead(previewPath, parseReachable);
    const canQueue = previewReady(previewPath, preview.dataPath, preview.loading, preview.error, preview.data);
    const [ack, setAck] = useState(false), [busy, setBusy] = useState(false), [discard, setDiscard] = useState(false), [testOK, setTestOK] = useState(false);
    const [message, setMessage] = useState(""), [blocked, setBlocked] = useState(false), [failed, setFailed] = useState(false);
    const reviewHeading = useRef<HTMLHeadingElement>(null);
    useEffect(() => { reviewHeading.current?.focus(); }, [review]);
    const lock = useRef(false), draftRef = useRef(draft), closeRef = useRef(onClose);
    draftRef.current = draft;
    closeRef.current = onClose;
    const close = useCallback(() => { if (lock.current)
        return; const d = draftRef.current; if (d.text || d.buttons.length || d.schedule || d.audience !== "all" || d.locale)
        setDiscard(true);
    else
        closeRef.current(); }, []);
    const change = (next: Draft) => { if (lock.current)
        return; setDraft(next); setTestOK(false); setMessage(""); setFailed(false); };
    const prepare = (kind: "test" | "create") => {
        if (lock.current || blocked)
            return;
        if (kind === "create" && !canQueue) {
            setMessage(t("previewUnavailable")); setFailed(true); return;
        }
        try {
            // Test never inherits schedule/audience: its only recipient is the current staff member.
            const body = buildBody(kind === "test" ? { ...draft, schedule: "" } : draft);
            setReview({ kind, body: kind === "test" ? { ...body, kind: "all", buyers_only: false, resellers_only: false, locale: null } : body });
            setAck(false);
            setMessage("");
            setFailed(false);
        }
        catch (err) {
            const key = err instanceof Error ? err.message : "text";
            setMessage(t(key === "schedule" ? "scheduleError" : key === "buttons" ? "buttonsError" : "textError"));
            setFailed(true);
        }
    };
    const send = async () => {
        if (lock.current || !review || !ack || blocked)
            return;
        if (review.kind === "create" && !canQueue) {
            setMessage(t("previewUnavailable")); setFailed(true); return;
        }
        // Recheck schedule at the final click, not only when the review was opened.
        if (review.kind === "create")
            try {
                buildBody(draft);
            }
            catch {
                setMessage(t("scheduleError"));
                setFailed(true);
                return;
            }
        lock.current = true;
        setBusy(true);
        setMessage("");
        try {
            const response = await call<unknown>(submitPath(review.kind), { method: "POST", body: review.body });
            if (review.kind === "test") {
                const sentTo = (response as {
                    sent_to?: unknown;
                } | null)?.sent_to;
                if (typeof sentTo !== "number" || !Number.isSafeInteger(sentTo) || sentTo <= 0)
                    throw new Error("invalid");
                setReview(null);
                setAck(false);
                setTestOK(true);
                setMessage(t("testOK"));
                setFailed(false);
            }
            else {
                parseRow(response);
                onSaved();
            }
        }
        catch (error) {
            const kind = errorKind(error);
            setBlocked(kind === "uncertain");
            setFailed(true);
            setAck(false);
            setMessage(t(kind) + (kind === "rejected" ? ` ${errorText(error)}` : ""));
        }
        finally {
            lock.current = false;
            setBusy(false);
        }
    };
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return <Modal title={t("new")} onClose={close}>
    <p className="bc-muted">{t("plain")}</p>
    {discard && <div className="bc-note"><p>{t("discard")}</p><div className="bc-actions"><button disabled={busy} onClick={() => closeRef.current()}>{t("discardYes")}</button><button onClick={() => setDiscard(false)}>{t("keep")}</button></div></div>}
    {busy && <Notice text={t("busy")}/>}
    {message && <Notice text={message} error={failed}/>}
    {blocked && <Recovery t={t} onChecked={() => { setBlocked(false); setReview(null); setAck(false); setMessage(""); }}/>}
    {!review ? <fieldset disabled={busy} style={{ border: 0, padding: 0 }}>
      <label htmlFor="bc-text">{t("text")}</label><textarea id="bc-text" value={draft.text} onChange={e => change({ ...draft, text: e.target.value })} aria-describedby="bc-count"/>
      <p id="bc-count" className="bc-muted" aria-live="polite"><bdi dir="ltr">{codepoints(draft.text)} / 4000</bdi></p>
      <label htmlFor="bc-audience">{t("audience")}</label><select id="bc-audience" value={draft.audience} onChange={e => change({ ...draft, audience: e.target.value as Draft["audience"] })}>{AUDIENCE_KINDS.map(k => <option key={k} value={k}>{t(k)}</option>)}</select>
      <label htmlFor="bc-language">{t("language")}</label><select id="bc-language" value={draft.locale} onChange={e => change({ ...draft, locale: e.target.value as Draft["locale"] })}><option value="">{t("anyLanguage")}</option><option value="fa">فارسی</option><option value="en">English</option></select><p className="bc-muted">{t("languageHint")}</p>
      <section className="bc-note" aria-live="polite"><p>{t("selectedEstimate")}: {preview.loading ? t("loading") : canQueue ? new Intl.NumberFormat(locale).format(preview.data!) : t("unknown")}</p><p className="bc-muted">{t("previewHint")}</p><button disabled={preview.loading} onClick={() => void preview.reload()}>{t("refresh")}</button></section>
      <fieldset><legend>{t("buttons")}</legend>{draft.buttons.map((b, i) => <div className="bc-link" key={i}>
        <label htmlFor={`bc-label-${i}`}>{t("label")} {i + 1}</label><input id={`bc-label-${i}`} value={b.text} onChange={e => change({ ...draft, buttons: draft.buttons.map((v, j) => j === i ? { ...v, text: e.target.value } : v) })}/>
        <label htmlFor={`bc-url-${i}`}>{t("url")} {i + 1}</label><input id={`bc-url-${i}`} dir="ltr" type="url" value={b.url} onChange={e => change({ ...draft, buttons: draft.buttons.map((v, j) => j === i ? { ...v, url: e.target.value } : v) })}/>
        <button onClick={() => change({ ...draft, buttons: draft.buttons.filter((_, j) => j !== i) })} aria-label={`${t("remove")} ${i + 1}`}>{t("remove")}</button>
      </div>)}<button disabled={busy || draft.buttons.length >= 6} onClick={() => change({ ...draft, buttons: [...draft.buttons, { text: "", url: "" }] })}>{t("add")}</button></fieldset>
      <label htmlFor="bc-schedule">{t("schedule")}</label><input id="bc-schedule" type="datetime-local" value={draft.schedule} onChange={e => change({ ...draft, schedule: e.target.value })}/><p className="bc-muted"><bdi>{zone}</bdi> · {t("scheduleHint")}</p>
      {testOK && <p className="bc-note">{t("testOK")}</p>}
      <div className="bc-actions"><button disabled={busy || blocked} onClick={() => prepare("test")}>{t("test")}</button><button className="bc-primary" disabled={busy || blocked || !canQueue} onClick={() => prepare("create")}>{t("review")}</button></div>
    </fieldset> : <section aria-label={t(review.kind === "test" ? "testTitle" : "reviewTitle")}>
      <h2 tabIndex={-1} ref={reviewHeading}>{t(review.kind === "test" ? "testTitle" : "reviewTitle")}</h2><p>{t(review.kind === "test" ? "testHint" : "reviewHint")}</p>
      {review.kind === "create" && <dl><dt>{t("audience")}</dt><dd>{t(draft.audience)}</dd><dt>{t("language")}</dt><dd>{draft.locale || t("anyLanguage")}</dd><dt>{t("scheduled")}</dt><dd>{review.body.scheduled_at ? <><time>{stamp(review.body.scheduled_at, locale)}</time> · <bdi>{zone}</bdi><br /><bdi>{review.body.scheduled_at}</bdi></> : t("now")}</dd></dl>}
      <pre dir="auto">{review.body.text}</pre>{review.body.buttons.length > 0 && <ul>{review.body.buttons.map((b, i) => <li key={i}>{b.text} — <bdi>{b.url}</bdi></li>)}</ul>}
      <label className="bc-check"><input type="checkbox" checked={ack} disabled={busy || blocked} onChange={e => setAck(e.target.checked)}/>{t("ack")}</label>
      <div className="bc-actions"><button disabled={busy} onClick={() => { setReview(null); setAck(false); setMessage(""); }}>{t("edit")}</button><button className="bc-primary" disabled={busy || blocked || !ack || (review.kind === "create" && !canQueue)} onClick={() => void send()}>{t(review.kind === "test" ? "confirmTest" : review.body.scheduled_at ? "queueScheduled" : "queue")}</button></div>
    </section>}
  </Modal>;
}
function ActionModal({ row, action, t, onClose, onSaved }: {
    row: BroadcastRow;
    action: Action;
    t: T;
    onClose: () => void;
    onSaved: () => void;
}) {
    const { errorText } = useSession();
    const [busy, setBusy] = useState(false), [ack, setAck] = useState(false), [message, setMessage] = useState("");
    const lock = useRef(false), closeRef = useRef(onClose);
    closeRef.current = onClose;
    const close = useCallback(() => { if (!lock.current)
        closeRef.current(); }, []);
    const execute = async () => {
        if (lock.current || !ack || message)
            return;
        lock.current = true;
        setBusy(true);
        try {
            const value = parseRow(await call<unknown>(`/api/admin/broadcasts/${row.id}/${action}`, { method: "POST" }));
            if (value.id !== row.id)
                throw new Error("invalid");
            onSaved();
        }
        catch (error) {
            setMessage(t(errorKind(error)) + (errorKind(error) === "rejected" ? ` ${errorText(error)}` : ""));
            setAck(false);
        }
        finally {
            lock.current = false;
            setBusy(false);
        }
    };
    return <Modal title={`${t(action)} · #${row.id}`} onClose={close}><p>{t("actionHint")}</p><pre dir="auto">{row.text}</pre>{busy && <Notice text={t("busy")}/>} {message && <Notice text={message} error/>}<label className="bc-check"><input type="checkbox" disabled={busy || !!message} checked={ack} onChange={e => setAck(e.target.checked)}/>{t("ack")}</label><div className="bc-actions"><button disabled={busy} onClick={close}>{t("back")}</button><button className="bc-primary" disabled={busy || !!message || !ack} onClick={() => void execute()}>{t("confirm")}</button></div></Modal>;
}
function Manager({ t, locale }: {
    t: T;
    locale: string;
}) {
    const [params, setParams] = useSearchParams();
    const raw = Number(params.get("page") ?? 0);
    const page = Number.isSafeInteger(raw) && raw >= 0 && raw < 1000000 ? raw : 0;
    const history = useRead(`/api/admin/broadcasts?limit=25&offset=${page * 25}`, parsePage), audience = useRead("/api/admin/broadcasts/audience", parseReachable);
    const [compose, setCompose] = useState(false), [action, setAction] = useState<{
        row: BroadcastRow;
        action: Action;
    } | null>(null), [notice, setNotice] = useState("");
    const closeComposer = useCallback(() => setCompose(false), []), closeAction = useCallback(() => setAction(null), []);
    const refresh = () => { void history.reload(); void audience.reload(); };
    const saved = (kind: "queued" | "savedAction") => { setCompose(false); setAction(null); setNotice(t(kind)); refresh(); };
    const changePage = (value: number) => { setParams(prev => { const next = new URLSearchParams(prev); if (value)
        next.set("page", String(value));
    else
        next.delete("page"); return next; }, { replace: true }); };
    return <main className="bc"><header className="bc-top"><h1>{t("title")}</h1><button className="bc-primary" onClick={() => { setCompose(true); setNotice(""); }}>{t("new")}</button></header>
    <section className="bc-card"><h2>{t("estimate")}: {audience.loading ? t("loading") : audience.error || audience.data === null ? t("unknown") : new Intl.NumberFormat(locale).format(audience.data)}</h2><p className="bc-muted">{t("estimateHint")}</p></section>
    <div className="bc-actions"><p className="bc-muted">{t("plain")}</p><button disabled={history.loading || audience.loading} onClick={refresh}>{t("refresh")}</button></div>
    {notice && <Notice text={notice}/>}{history.loading && <p role="status">{t("loading")}</p>}{history.error && <Notice text={t(history.data ? "stale" : "loadError")} error/>}
    {!history.loading && !history.error && history.data?.items.length === 0 && <p className="bc-card">{t("empty")}</p>}
    <div className="bc-list">{history.data?.items.map(row => <article className="bc-card" key={row.id} aria-label={`#${row.id}`}>
      <header className="bc-top"><h2>#{row.id}</h2><span className="bc-badge">{statusKey(row.status) ? t(statusKey(row.status)!) : row.status}</span></header><pre dir="auto">{row.text}</pre>
      <p className="bc-muted">{t(audienceKey(row.audience))} · {typeof row.audience.locale === "string" ? row.audience.locale : t("anyLanguage")}</p>
      <dl className="bc-grid">{(["sent", "failed", "blocked", "deleted", "skipped", "total"] as const).map(key => <div key={key}><dt>{t(key)}</dt><dd>{key === "total" && row.total === 0 && ["queued", "paused"].includes(row.status) ? t("uncounted") : row[key] === null ? t("unknown") : new Intl.NumberFormat(locale).format(row[key])}</dd></div>)}</dl>
      <p className="bc-muted">{t("counterHint")}</p><p className="bc-muted">{t("skippedHint")}</p><p className="bc-muted">{t("created")}: <time>{stamp(row.created_at, locale)}</time>{row.scheduled_at && <><br />{t("scheduled")}: <time>{stamp(row.scheduled_at, locale)}</time></>}{row.finished_at && <><br />{t("finished")}: <time>{stamp(row.finished_at, locale)}</time></>}</p>
      <div className="bc-actions">{actions(row.status).map(value => <button key={value} disabled={history.loading || history.error} onClick={() => { setAction({ row, action: value }); setNotice(""); }}>{t(value)}</button>)}</div>
    </article>)}</div>
    <nav className="bc-actions" aria-label={t("page")}><button disabled={page === 0 || history.loading} onClick={() => changePage(page - 1)}>{t("previous")}</button><span>{t("page")} {new Intl.NumberFormat(locale).format(page + 1)}</span><button disabled={!history.data || history.error || history.loading || (page + 1) * 25 >= history.data.total} onClick={() => changePage(page + 1)}>{t("next")}</button></nav>
    {compose && <Composer t={t} locale={locale} onClose={closeComposer} onSaved={() => saved("queued")}/>}{action && <ActionModal key={`${action.row.id}-${action.action}`} {...action} t={t} onClose={closeAction} onSaved={() => saved("savedAction")}/>}
  </main>;
}
export function Broadcasts() {
    const { can, checking, locale } = useSession();
    const t = translator(locale);
    if (checking)
        return <main className="bc"><p role="status">{t("loading")}</p></main>;
    if (!can("broadcast.send"))
        return <main className="bc"><h1>{t("title")}</h1><p role="alert">{t("denied")}</p></main>;
    return <Manager t={t} locale={locale}/>;
}
