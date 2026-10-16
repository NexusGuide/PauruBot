import { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useSession } from '../session';
import { PRIORITIES, listPath, parseDepartments, parseDetail, parseList, parseCannedList, parseTicket, record, positive, newId, ok, numeric, ticketPatch, validText, points, renderCanned, type Detail, type Department, type Priority } from './model';
import { translator, type T, type Key } from './copy';
import { useRead, Notice, Confirmation, stamp, type Proposal } from './ui';
import { DeskSettings } from './DeskSettings';
import './tickets.css';
const stateLabel = (status: string, t: T): string => ['open', 'answered', 'closed'].includes(status) ? t(status as Key) : status;
const deptName = (id: number | null, rows: Department[] | null, t: T): string => id === null ? t('unassignedDepartment') : rows?.find(d => d.id === id)?.title ?? `${t('unknownDepartment')} #${id}`;
export function TicketDesk() {
    const { checking, can, locale, me } = useSession();
    const t = translator(locale);
    if (checking)
        return null;
    if (!can('tickets.read') || me?.role === 'reseller')
        return <main className="td"><Notice error>{t('denied')}</Notice></main>;
    return <DeskList t={t} locale={locale}/>;
}
function DeskList({ t, locale }: {
    t: T;
    locale: string;
}) {
    const { can } = useSession();
    const [params, setParams] = useSearchParams(), [settings, setSettings] = useState(false);
    const { path, page } = listPath(params);
    const list = useRead(path, parseList), departments = useRead('/api/admin/ticket-departments', parseDepartments);
    const filter = (key: string, value: string) => setParams(prev => { const next = new URLSearchParams(prev); if (value)
        next.set(key, value);
    else
        next.delete(key); next.delete('page'); return next; }, { replace: true });
    const changePage = (value: number) => setParams(prev => { const next = new URLSearchParams(prev); if (value)
        next.set('page', String(value));
    else
        next.delete('page'); return next; }, { replace: true });
    return <main className="td"><header className="td-top"><h1>{t('title')}</h1><div className="td-actions"><button disabled={list.loading} onClick={() => { void list.reload(); void departments.reload(); }}>{t('refresh')}</button>{can('settings.write') && <button onClick={() => setSettings(!settings)} aria-expanded={settings}>{t('settings')}</button>}</div></header>
 {settings && <DeskSettings t={t} onChanged={() => void departments.reload()}/>}
 <section className="td-card td-grid" aria-label={t('title')}>
 <label>{t('status')}<select aria-label={t('status')} value={params.get('status') || ''} onChange={e => filter('status', e.target.value)}><option value="">{t('all')}</option>{['open', 'answered', 'closed'].map(s => <option key={s} value={s}>{stateLabel(s, t)}</option>)}</select></label>
 <label>{t('priority')}<select aria-label={t('priority')} value={params.get('priority') || ''} onChange={e => filter('priority', e.target.value)}><option value="">{t('all')}</option>{PRIORITIES.map(p => <option key={p} value={p}>{t(p)}</option>)}</select></label>
 <label>{t('department')}<select aria-label={t('department')} value={params.get('department_id') || ''} onChange={e => filter('department_id', e.target.value)}><option value="">{t('all')}</option><option value="0">{t('unassignedDepartment')}</option>{departments.data?.map(d => <option key={d.id} value={d.id}>{d.title}{!d.is_active ? ` (${t('inactive')})` : ''}</option>)}</select></label>
 <label>{t('assigned')}<select aria-label={t('assigned')} value={params.get('assigned') || ''} onChange={e => filter('assigned', e.target.value)}><option value="">{t('all')}</option><option value="me">{t('me')}</option><option value="none">{t('none')}</option></select></label>
 <label>{t('waiting')}<select aria-label={t('waiting')} value={params.get('awaiting') || ''} onChange={e => filter('awaiting', e.target.value)}><option value="">{t('all')}</option><option value="true">{t('waiting')}</option><option value="false">{t('notWaiting')}</option></select></label>
 </section>
 {departments.error && <p className="td-muted">{t('department')}: {t('error')}</p>}{list.loading && <p role="status">{t('loading')}</p>}{list.error && <Notice error>{t(list.data ? 'stale' : 'error')}</Notice>}
 {!list.loading && !list.error && list.data?.items.length === 0 && <p className="td-card">{t('empty')}</p>}
 <div className="td-list">{list.data?.items.map(row => <article key={row.id} className="td-card"><h2><Link className="td-link" to={`/tickets/${row.id}`}>#{row.id} — <bdi>{row.subject}</bdi></Link></h2><div className="td-actions"><span className="td-badge">{stateLabel(row.status, t)}</span><span>{t(row.priority)}</span></div><p>{t('customer')}: <bdi>{row.customer ?? `#${row.customer_id}`}</bdi></p><p>{deptName(row.department_id, departments.data, t)} · {t('assigned')}: {row.assigned_staff_id === null ? t('none') : `#${row.assigned_staff_id}`}</p><p className="td-muted">{t('updated')}: {stamp(row.updated_at, locale)}</p>{row.awaiting && <p className="td-note">{t('waitingSince')}: {stamp(row.last_customer_at, locale)}</p>}</article>)}</div>
 <nav className="td-actions" aria-label={t('page')}><button disabled={page === 0 || list.loading} onClick={() => changePage(page - 1)}>{t('previous')}</button><span>{t('page')} {page + 1}</span><button disabled={list.loading || list.error || !list.data || (page + 1) * 25 >= list.data.total} onClick={() => changePage(page + 1)}>{t('next')}</button></nav>
 </main>;
}
export function StaffTicketPage() {
    const { id } = useParams();
    const { checking, can, me, locale } = useSession();
    const t = translator(locale);
    if (checking)
        return null;
    if (!can('tickets.read') || me?.role === 'reseller')
        return <main className="td"><Notice error>{t('denied')}</Notice></main>;
    const ticketId = id && /^\d+$/.test(id) ? Number(id) : 0;
    if (!positive(ticketId))
        return <main className="td"><Link className="td-link" to="/tickets">{t('back')}</Link><Notice error>{t('error')}</Notice></main>;
    return <Thread key={ticketId} id={ticketId} t={t} locale={locale}/>;
}
function Thread({ id, t, locale }: {
    id: number;
    t: T;
    locale: string;
}) {
    const state = useRead(`/api/admin/tickets/${id}`, parseDetail), departments = useRead('/api/admin/ticket-departments', parseDepartments);
    const [notice, setNotice] = useState('');
    return <main className="td"><div className="td-actions"><Link className="td-link" to="/tickets">{t('back')}</Link><button disabled={state.loading} onClick={() => { void state.reload(); void departments.reload(); }}>{t('refresh')}</button></div>
 {notice && <Notice>{notice}</Notice>}{state.loading && <p role="status">{t('loading')}</p>}{state.error && <Notice error>{t(state.data ? 'stale' : 'error')}</Notice>}
 {state.data && state.data.ticket.id !== id && <Notice error>{t('error')}</Notice>}
 {state.data && state.data.ticket.id === id && <ThreadBody key={id} detail={state.data} t={t} locale={locale} departments={departments.data} stale={state.error || state.loading} onSaved={msg => { setNotice(msg); void state.reload(); }}/>}
 </main>;
}
function ThreadBody({ detail, t, locale, departments, stale, onSaved }: {
    detail: Detail;
    t: T;
    locale: string;
    departments: Department[] | null;
    stale: boolean;
    onSaved: (msg: string) => void;
}) {
    const { can, me } = useSession();
    const row = detail.ticket;
    const writable = can('tickets.reply');
    const known = ['open', 'answered', 'closed'].includes(row.status);
    const [requiresCheck, setRequiresCheck] = useState<Detail | null>(null);
    const blocked = stale || requiresCheck !== null;
    const [draft, setDraft] = useState(''), [mode, setMode] = useState<'reply' | 'note'>(row.status === 'closed' ? 'note' : 'reply'), [error, setError] = useState(''), [proposal, setProposal] = useState<Proposal | null>(null), [selected, setSelected] = useState('');
    const canned = useRead(writable ? `/api/admin/canned-replies${row.department_id ? `?department_id=${row.department_id}` : ''}` : null, parseCannedList);
    const prepareMessage = () => { if (!validText(draft)) {
        setError(t('textError'));
        return;
    } if (blocked || !writable || !known || mode === 'reply' && row.status === 'closed')
        return; setError(''); const text = draft.trim(); setProposal({ title: t(mode === 'reply' ? 'reviewReply' : 'reviewNote'), path: `/api/admin/tickets/${row.id}/${mode}`, method: 'POST', body: { text }, validate: mode === 'reply' ? ok : v => { newId(v); }, preview: <><h2>#{row.id} — <bdi>{row.subject}</bdi></h2><p className="td-note">{t(mode === 'reply' ? 'publicHint' : 'noteHint')}</p><strong>{t(mode === 'reply' ? 'public' : 'internal')}</strong><pre dir="auto">{text}</pre></>, success: t(mode === 'reply' ? 'replySaved' : 'noteSaved') }); };
    const close = () => setProposal({ title: t('closeTicket'), path: `/api/admin/tickets/${row.id}/close`, method: 'POST', validate: ok, preview: <><h2>#{row.id} — {row.subject}</h2><p>{t('closeHint')}</p></> });
    return <>{requiresCheck && <section className="td-note"><p>{t('checkAgain')}</p><button disabled={stale || requiresCheck === detail} onClick={() => setRequiresCheck(null)}>{t('checked')}</button></section>}<header className="td-top"><h1>#{row.id} — <bdi>{row.subject}</bdi></h1>{writable && known && row.status !== 'closed' && <button disabled={blocked} onClick={close}>{t('closeTicket')}</button>}</header>
 <section className="td-card"><div className="td-actions"><span className="td-badge">{stateLabel(row.status, t)}</span><span>{t(row.priority)}</span></div><p>{t('customer')}: <bdi>{detail.customer ?? `#${row.customer_id}`}</bdi></p><p>{t('department')}: {deptName(row.department_id, departments, t)}</p><p>{t('assigned')}: {row.assigned_staff_id === null ? t('none') : `#${row.assigned_staff_id}`}</p><p>{t('complaint')}: {row.is_complaint ? t('yes') : t('no')}</p>{row.awaiting && <p>{t('waitingSince')}: {stamp(row.last_customer_at, locale)}</p>}</section>
 {!known && <Notice error>{t('unknownStatus')}</Notice>}
 {writable && known && <Routing key={`${row.id}-${row.updated_at}-${row.department_id}-${row.assigned_staff_id}-${row.priority}-${row.is_complaint}`} row={row} departments={departments} t={t} staffId={me?.id ?? 0} disabled={blocked} propose={setProposal}/>}
 <section aria-labelledby="td-conversation"><h2 id="td-conversation">{t('conversation')}</h2><p className="td-muted">{t('historyHint')}</p><ol>{detail.messages.map(m => <li key={m.id} className={`td-message ${m.is_internal ? 'td-internal' : ''}`}><h3>{m.is_internal ? t('internal') : m.author_type === 'staff' ? t('staff') : m.author_type === 'customer' ? t('customer') : m.author_type}</h3>{m.text && <pre dir="auto">{m.text}</pre>}{m.has_attachment && <p className="td-muted">{t('attachment')} {m.attachment_kind && <bdi>({m.attachment_kind})</bdi>}</p>}<p className="td-muted">{stamp(m.created_at, locale)}</p></li>)}</ol></section>
 {!writable && <p className="td-note">{t('writeDenied')}</p>}
 {writable && known && <section className="td-card"><h2>{t('message')}</h2><p className="td-muted">{t('unsaved')}</p>{row.status === 'closed' && <p>{t('closedHint')}</p>}
 <fieldset disabled={blocked}><label htmlFor="td-mode">{t('mode')}</label><select id="td-mode" value={mode} onChange={e => { setMode(e.target.value as 'reply' | 'note'); setError(''); }}><option value="reply" disabled={row.status === 'closed'}>{t('public')}</option><option value="note">{t('internal')}</option></select><p className="td-note">{t(mode === 'reply' ? 'publicHint' : 'noteHint')}</p>
 {canned.loading && <p>{t('loading')}</p>}{canned.error && <p role="alert">{t('canned')}: {t('error')}</p>}{canned.data && <><label htmlFor="td-canned">{t('canned')}</label><select id="td-canned" value={selected} onChange={e => setSelected(e.target.value)}><option value="">{canned.data.length ? t('canned') : t('noCanned')}</option>{canned.data.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}</select><button disabled={!selected || canned.error || canned.loading} onClick={() => { const item = canned.data?.find(c => String(c.id) === selected); if (item) {
            setDraft(prev => [prev, renderCanned(item.text, detail.customer ?? '', row.id)].filter(Boolean).join('\n'));
            setSelected('');
            setError('');
        } }}>{t('append')}</button><p className="td-muted">{t('cannedHint')}</p></>}
 <label htmlFor="td-draft">{t('message')}</label><textarea id="td-draft" dir="auto" value={draft} onChange={e => { setDraft(e.target.value); setError(''); }}/><p className="td-muted"><bdi dir="ltr">{points(draft)} / 4000</bdi></p>{error && <Notice error>{error}</Notice>}<button className="td-primary" disabled={!validText(draft) || mode === 'reply' && row.status === 'closed'} onClick={prepareMessage}>{t(mode === 'reply' ? 'reviewReply' : 'reviewNote')}</button></fieldset></section>}
 {proposal && <Confirmation proposal={proposal} t={t} onClose={failed => { setProposal(null); if (failed)
        setRequiresCheck(detail); }} onSaved={message => { if (proposal.path.endsWith('/reply') || proposal.path.endsWith('/note'))
        setDraft(''); setProposal(null); onSaved(message); }}/>}
 </>;
}
function Routing({ row, departments, t, staffId, disabled, propose }: {
    row: Detail['ticket'];
    departments: Department[] | null;
    t: T;
    staffId: number;
    disabled: boolean;
    propose: (p: Proposal) => void;
}) {
    const [department, setDepartment] = useState(row.department_id === null ? '' : String(row.department_id)), [priority, setPriority] = useState<Priority>(row.priority), [assigned, setAssigned] = useState(row.assigned_staff_id === null ? '' : String(row.assigned_staff_id)), [complaint, setComplaint] = useState(row.is_complaint), [error, setError] = useState('');
    const save = () => { try {
        const body = ticketPatch(row, numeric(department, { positiveOnly: true }), priority, numeric(assigned, { positiveOnly: true }), complaint);
        if (!Object.keys(body).length)
            return;
        propose({ title: t('metadata'), path: `/api/admin/tickets/${row.id}`, method: 'PATCH', body, validate: v => { if (!record(v) || parseTicket(v.ticket).id !== row.id)
                throw new Error('invalid'); }, preview: <><h2>#{row.id} — {row.subject}</h2><dl><dt>{t('department')}</dt><dd>{deptName(row.department_id, departments, t)} → {deptName(numeric(department), departments, t)}</dd><dt>{t('priority')}</dt><dd>{t(row.priority)} → {t(priority)}</dd><dt>{t('assigned')}</dt><dd>{row.assigned_staff_id ?? t('none')} → {assigned || t('none')}</dd><dt>{t('complaint')}</dt><dd>{row.is_complaint ? t('yes') : t('no')} → {complaint ? t('yes') : t('no')}</dd></dl></> });
        setError('');
    }
    catch {
        setError(t('numberError'));
    } };
    const changed = department !== (row.department_id === null ? '' : String(row.department_id)) || priority !== row.priority || assigned !== (row.assigned_staff_id === null ? '' : String(row.assigned_staff_id)) || complaint !== row.is_complaint;
    return <section className="td-card"><h2>{t('metadata')}</h2><fieldset disabled={disabled}><div className="td-grid"><label>{t('department')}<select aria-label={t('department')} value={department} onChange={e => setDepartment(e.target.value)}><option value="">{t('unassignedDepartment')}</option>{row.department_id && !departments?.some(d => d.id === row.department_id) && <option value={row.department_id}>#{row.department_id}</option>}{departments?.map(d => <option key={d.id} value={d.id}>{d.title}{!d.is_active ? ` (${t('inactive')})` : ''}</option>)}</select></label><label>{t('priority')}<select aria-label={t('priority')} value={priority} onChange={e => setPriority(e.target.value as Priority)}>{PRIORITIES.map(p => <option key={p} value={p}>{t(p)}</option>)}</select></label><label>{t('staffId')}<input aria-label={t('staffId')} dir="ltr" inputMode="numeric" value={assigned} onChange={e => setAssigned(e.target.value)}/></label><label><input type="checkbox" aria-label={t('complaint')} checked={complaint} onChange={e => setComplaint(e.target.checked)}/>{t('complaint')}</label></div><p className="td-muted">{t('staffHint')}</p><p className="td-muted">{t('complaintHint')}</p><div className="td-actions"><button onClick={() => setAssigned(String(staffId))}>{t('assignMe')}</button><button onClick={() => setAssigned('')}>{t('none')}</button><button className="td-primary" disabled={!changed} onClick={save}>{t('save')}</button></div>{error && <Notice error>{error}</Notice>}</fieldset></section>;
}
