import { useState } from 'react';
import { useSession } from '../session';
import { numeric, ok, parseCanned, parseCannedList, parseDepartment, parseDepartments, points, validText, type Canned, type Department } from './model';
import type { T } from './copy';
import { Confirmation, Notice, useRead, type Proposal } from './ui';
type Editor = {
    kind: 'department';
    row: Department | null;
} | {
    kind: 'canned';
    row: Canned | null;
};
export function DeskSettings({ t, onChanged }: {
    t: T;
    onChanged: () => void;
}) {
    const { can, me } = useSession();
    if (!can('settings.write') || me?.role === 'reseller')
        return null;
    return <Settings t={t} onChanged={onChanged} canCanned={can('tickets.reply')}/>;
}
function Settings({ t, onChanged, canCanned }: {
    t: T;
    onChanged: () => void;
    canCanned: boolean;
}) {
    const departments = useRead('/api/admin/ticket-departments', parseDepartments), canned = useRead(canCanned ? '/api/admin/canned-replies' : null, parseCannedList);
    const [editor, setEditor] = useState<Editor | null>(null), [proposal, setProposal] = useState<Proposal | null>(null), [notice, setNotice] = useState('');
    const [requiresCheck, setRequiresCheck] = useState<{
        departments: number;
        canned: number;
    } | null>(null);
    const refresh = () => { void departments.reload(); void canned.reload(); onChanged(); };
    const sections: [
        Editor['kind'],
        {
            data: (Department | Canned)[] | null;
            loading: boolean;
            error: boolean;
        }
    ][] = [['department', departments]];
    if (canCanned)
        sections.push(['canned', canned]);
    const remove = (kind: Editor['kind'], row: Department | Canned) => setProposal({ title: `${t('delete')} — ${row.title}`, path: `/api/admin/${kind === 'department' ? 'ticket-departments' : 'canned-replies'}/${row.id}`, method: 'DELETE', validate: ok, preview: <><h2>{row.title}</h2><p>{t(kind === 'department' ? 'deleteDepartmentHint' : 'deleteCannedHint')}</p></> });
    return <section className="td-settings"><header className="td-top"><h2>{t('settings')}</h2><button onClick={refresh} disabled={departments.loading || canned.loading}>{t('refresh')}</button></header><p className="td-muted">{t('manageHint')}</p>{notice && <Notice>{notice}</Notice>}{requiresCheck && <div className="td-note"><p>{t('checkAgain')}</p><button disabled={departments.loading || departments.error || departments.revision <= requiresCheck.departments || (canCanned && (canned.loading || canned.error || canned.revision <= requiresCheck.canned))} onClick={() => setRequiresCheck(null)}>{t('checked')}</button></div>}
 {sections.map(([rawKind, state]) => {
            const kind = rawKind as Editor['kind'];
            return <section key={kind} className="td-card"><div className="td-actions"><h2>{t(kind === 'department' ? 'departments' : 'canned')}</h2><button disabled={!!requiresCheck} onClick={() => setEditor(kind === 'department' ? { kind, row: null } : { kind, row: null })}>{t(kind === 'department' ? 'newDepartment' : 'newCanned')}</button></div>{state.loading && <p role="status">{t('loading')}</p>}{state.error && <Notice error>{t('error')}</Notice>}{!state.loading && !state.error && state.data?.length === 0 && <p>{t('emptySettings')}</p>}
 <ul>{state.data?.map(row => <li key={row.id} className="td-card"><h3><bdi>{row.title}</bdi> · #{row.id}</h3>{'is_active' in row ? <p>{t(row.is_active ? 'active' : 'inactive')}</p> : <pre dir="auto">{row.text}</pre>}<div className="td-actions"><button disabled={state.loading || state.error || !!requiresCheck} onClick={() => setEditor(kind === 'department' ? { kind, row: row as Department } : { kind, row: row as Canned })}>{t('edit')}</button><button disabled={state.loading || state.error || !!requiresCheck} onClick={() => remove(kind, row)}>{t('delete')}</button></div></li>)}</ul></section>;
        })}
 {editor && <fieldset disabled={!!requiresCheck}><EditorForm key={`${editor.kind}-${editor.row?.id ?? 0}`} editor={editor} departments={departments.data} t={t} onClose={() => setEditor(null)} propose={setProposal}/></fieldset>}
 {proposal && <Confirmation proposal={proposal} t={t} onClose={failed => { setProposal(null); if (failed)
        setRequiresCheck({ departments: departments.revision, canned: canned.revision }); }} onSaved={message => { setProposal(null); setEditor(null); setNotice(message); refresh(); }}/>}
 </section>;
}
function EditorForm({ editor, departments, t, onClose, propose }: {
    editor: Editor;
    departments: Department[] | null;
    t: T;
    onClose: () => void;
    propose: (p: Proposal) => void;
}) {
    const row = editor.row;
    const department = editor.kind === 'department' ? editor.row : null, canned = editor.kind === 'canned' ? editor.row : null;
    const [title, setTitle] = useState(row?.title ?? ''), [text, setText] = useState(canned?.text ?? ''), [sort, setSort] = useState(String(row?.sort ?? 0)), [active, setActive] = useState(department?.is_active ?? true), [chat, setChat] = useState(department?.log_chat_id === null || department?.log_chat_id === undefined ? '' : String(department.log_chat_id)), [thread, setThread] = useState(department?.log_thread_id === null || department?.log_thread_id === undefined ? '' : String(department.log_thread_id)), [dep, setDep] = useState(canned?.department_id ? String(canned.department_id) : ''), [error, setError] = useState('');
    const review = () => {
        if (!validText(title, 64) || (editor.kind === 'canned' && !validText(text))) {
            setError(t('textError'));
            return;
        }
        try {
            const order = numeric(sort);
            if (order === null)
                throw new Error('number');
            const body = editor.kind === 'department' ? { title: title.trim(), sort: order, is_active: active, log_chat_id: numeric(chat), log_thread_id: numeric(thread, { positiveOnly: true }) } : { title: title.trim(), sort: order, text, department_id: numeric(dep, { positiveOnly: true }) };
            propose({ title: t('save'), path: `/api/admin/${editor.kind === 'department' ? 'ticket-departments' : 'canned-replies'}${row ? `/${row.id}` : ''}`, method: row ? 'PUT' : 'POST', body, validate: value => { const result = editor.kind === 'department' ? parseDepartment(value) : parseCanned(value); if (row && result.id !== row.id)
                    throw new Error('invalid'); }, preview: <><h2>{title}</h2><p>{t('sort')}: {order}</p>{editor.kind === 'department' ? <><p>{t(active ? 'active' : 'inactive')}</p><p>{t('logChat')}: <bdi>{chat || '—'}</bdi></p><p>{t('logThread')}: <bdi>{thread || '—'}</bdi></p><p className="td-note">{t('logHint')}</p></> : <><p>{t('department')}: {departments?.find(d => String(d.id) === dep)?.title ?? (dep ? `#${dep}` : t('shared'))}</p><pre dir="auto">{text}</pre></>}</> });
            setError('');
        }
        catch {
            setError(t('numberError'));
        }
    };
    return <section className="td-card"><h2>{t(editor.kind === 'department' ? 'departments' : 'canned')} — {row ? `${t('edit')} #${row.id}` : t(editor.kind === 'department' ? 'newDepartment' : 'newCanned')}</h2><p className="td-muted">{t('unsaved')}</p>
 <label htmlFor="td-setting-title">{t('titleLabel')}</label><input id="td-setting-title" value={title} onChange={e => setTitle(e.target.value)}/><p className="td-muted"><bdi>{points(title)} / 64</bdi></p><label htmlFor="td-setting-sort">{t('sort')}</label><input id="td-setting-sort" dir="ltr" inputMode="numeric" value={sort} onChange={e => setSort(e.target.value)}/>
 {editor.kind === 'department' ? <><label className="td-check"><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)}/>{t('active')}</label><label htmlFor="td-setting-chat">{t('logChat')}</label><input id="td-setting-chat" dir="ltr" value={chat} onChange={e => setChat(e.target.value)}/><label htmlFor="td-setting-thread">{t('logThread')}</label><input id="td-setting-thread" dir="ltr" value={thread} onChange={e => setThread(e.target.value)}/><p className="td-muted">{t('logHint')}</p></> : <><label htmlFor="td-setting-department">{t('department')}</label><select id="td-setting-department" value={dep} onChange={e => setDep(e.target.value)}><option value="">{t('shared')}</option>{canned?.department_id && !departments?.some(d => d.id === canned.department_id) && <option value={canned.department_id}>#{canned.department_id}</option>}{departments?.map(d => <option key={d.id} value={d.id}>{d.title}</option>)}</select><label htmlFor="td-setting-text">{t('message')}</label><textarea id="td-setting-text" value={text} onChange={e => setText(e.target.value)}/><p className="td-muted"><bdi dir="ltr">{points(text)} / 4000</bdi></p><p className="td-muted">{t('cannedHint')}</p></>}
 {error && <Notice error>{error}</Notice>}<div className="td-actions"><button onClick={onClose}>{t('cancel')}</button><button className="td-primary" onClick={review}>{t('save')}</button></div></section>;
}
