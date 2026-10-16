import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ApiError, call } from '../api';
import { Sheet } from '../ui';
import type { T } from './copy';
export function useRead<TData>(path: string | null, parse: (v: unknown) => TData) {
    const [data, setData] = useState<TData | null>(null), [error, setError] = useState(false), [loading, setLoading] = useState(!!path), [revision, setRevision] = useState(0);
    const seq = useRef(0);
    const reload = useCallback(async () => { if (!path)
        return; const current = ++seq.current; setLoading(true); setError(false); try {
        const value = parse(await call<unknown>(path));
        if (current === seq.current) {
            setData(value);
            setRevision(v => v + 1);
        }
    }
    catch {
        if (current === seq.current)
            setError(true);
    }
    finally {
        if (current === seq.current)
            setLoading(false);
    } }, [path, parse]);
    useEffect(() => { setData(null); void reload(); return () => { seq.current++; }; }, [reload]);
    return { data, error, loading, reload, revision };
}
export function Notice({ children, error = false }: {
    children: ReactNode;
    error?: boolean;
}) { const ref = useRef<HTMLDivElement>(null); useEffect(() => { ref.current?.focus(); ref.current?.scrollIntoView({ block: 'nearest' }); }, [children]); return <div className={`td-note ${error ? 'td-error' : ''}`} ref={ref} tabIndex={-1} role={error ? 'alert' : 'status'}>{children}</div>; }
export function Modal({ title, onClose, children }: {
    title: string;
    onClose: () => void;
    children: ReactNode;
}) { const root = useRef<HTMLDivElement>(null); useEffect(() => { const trap = (e: KeyboardEvent) => { if (e.key !== 'Tab')
    return; const dialog = root.current?.closest('[role="dialog"]'); const nodes = Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]') ?? []).filter(n => n.getClientRects().length); const first = nodes[0], last = nodes[nodes.length - 1]; if (!first || !last)
    return; if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
    e.preventDefault();
    last.focus();
}
else if (!e.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
    e.preventDefault();
    first.focus();
} }; document.addEventListener('keydown', trap); return () => document.removeEventListener('keydown', trap); }, []); return <div className="td-layer"><Sheet open title={title} onClose={onClose}><div className="td-modal" ref={root}>{children}</div></Sheet></div>; }
export interface Proposal {
    title: string;
    path: string;
    method: 'POST' | 'PATCH' | 'PUT' | 'DELETE';
    body?: unknown;
    validate: (v: unknown) => void;
    preview: ReactNode;
    success?: string;
}
export function Confirmation({ proposal, t, onClose, onSaved }: {
    proposal: Proposal;
    t: T;
    onClose: (failed?: boolean) => void;
    onSaved: (message: string) => void;
}) {
    const [ack, setAck] = useState(false), [busy, setBusy] = useState(false), [message, setMessage] = useState('');
    const lock = useRef(false), failure = useRef(false), closeRef = useRef(onClose);
    closeRef.current = onClose;
    const close = useCallback(() => { if (!lock.current)
        closeRef.current(failure.current); }, []);
    const send = async () => { if (lock.current || !ack || message)
        return; lock.current = true; setBusy(true); try {
        const response = await call<unknown>(proposal.path, { method: proposal.method, body: proposal.body });
        proposal.validate(response);
        onSaved(proposal.success ?? t('saved'));
    }
    catch (e) {
        failure.current = true;
        setMessage(t(e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 ? 'rejected' : 'uncertain'));
        setAck(false);
    }
    finally {
        lock.current = false;
        setBusy(false);
    } };
    return <Modal title={proposal.title} onClose={close}>{proposal.preview}{busy && <Notice>{t('busy')}</Notice>}{message && <Notice error>{message}</Notice>}<label className="td-check"><input type="checkbox" checked={ack} disabled={busy || !!message} onChange={e => setAck(e.target.checked)}/>{t('ack')}</label><div className="td-actions"><button disabled={busy} onClick={close}>{t('cancel')}</button><button className={proposal.method === 'DELETE' || proposal.path.endsWith('/close') ? 'td-danger' : 'td-primary'} disabled={busy || !!message || !ack} onClick={() => void send()}>{t('confirm')}</button></div></Modal>;
}
export const stamp = (value: string | null, locale: string): string => value ? new Intl.DateTimeFormat(locale === 'fa' ? 'fa-IR' : 'en-GB', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—';
