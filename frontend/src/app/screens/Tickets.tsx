import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "@shared/http";
import { api, type Ticket, type TicketList, type TicketThreadData } from "../api";
import { errorText, useApp } from "../state";
import { Panel, Screen, Spinner } from "../ui";

const control = "min-h-12 w-full rounded-2xl bg-[var(--surface)] p-3 text-start font-semibold disabled:opacity-50";
const input = "mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--bg)] p-3";

// Abort + generation guard: a previous route/page/read never replaces a newer result.
function useRead<T>(key: string, read: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<{ key: string; data: T | null; error: unknown; busy: boolean }>({ key, data: null, error: null, busy: true });
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const reload = useCallback(async () => {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    const id = ++sequence.current;
    setState(previous => ({ key, data: previous.key === key ? previous.data : null, error: null, busy: true }));
    try {
      const data = await read(current.signal);
      if (!current.signal.aborted && id === sequence.current) setState({ key, data, error: null, busy: false });
    } catch (error) {
      if (!current.signal.aborted && id === sequence.current) {
        setState(previous => ({ ...previous, error, busy: false }));
      }
    }
  }, [key, read]);
  useEffect(() => { void reload(); return () => { controller.current?.abort(); sequence.current += 1; }; }, [reload]);
  return { ...state, data: state.key === key ? state.data : null, busy: state.key !== key || state.busy, reload };
}

function useLive() {
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  return live;
}

function TicketStatus({ ticket }: { ticket: Ticket }) {
  const { t } = useApp();
  return <span className="text-sm font-semibold">{t(`ticket_status_${ticket.status}`)}</span>;
}

function ReadError({ error, retry }: { error: unknown; retry: () => void }) {
  const { t } = useApp();
  if (!error) return null;
  return <div className="my-3" role="alert"><p>{errorText(t, error)}</p>
    <button type="button" className={control} onClick={retry}>{t("tickets_retry")}</button></div>;
}

export function Tickets() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const [before, setBefore] = useState<number | undefined>();
  const read = useCallback((signal: AbortSignal) => api.tickets(before, signal), [before]);
  const { data, error, busy, reload } = useRead<TicketList>(String(before ?? "latest"), read);
  return <Screen title={t("tickets_title")}>
    <button type="button" className={control} onClick={() => navigate("/tickets/new")}>{t("tickets_new")}</button>
    <p className="my-3 text-sm text-[var(--hint)]">{t("tickets_order_hint")}</p>
    <button type="button" className={control} disabled={busy} onClick={() => void reload()}>{t("tickets_refresh")}</button>
    <ReadError error={error} retry={() => void reload()} />
    {busy && <div role="status" className="p-4"><Spinner /><span>{t("tickets_loading")}</span></div>}
    {!busy && data?.items.length === 0 && <Panel><p>{t("tickets_empty")}</p></Panel>}
    <ul className="my-3 space-y-3">{data?.items.map(ticket => <li key={ticket.id}>
      <button type="button" className={`${control} break-words`} onClick={() => navigate(`/tickets/${ticket.id}`)}>
        <span className="block font-bold">#{ticket.id} — {ticket.subject}</span>
        <TicketStatus ticket={ticket} />
        <span className="block text-xs text-[var(--hint)]">{ticket.updated_at ? new Date(ticket.updated_at).toLocaleString(locale) : ""}</span>
      </button>
    </li>)}</ul>
    {data?.next_before && <button type="button" className={control} disabled={busy} onClick={() => setBefore(data.next_before!)}>{t("tickets_older")}</button>}
    {before && <button type="button" className={control} disabled={busy} onClick={() => setBefore(undefined)}>{t("tickets_latest")}</button>}
    <button type="button" className={control} onClick={() => navigate("/more")}>{t("profile_back")}</button>
  </Screen>;
}

export function NewTicket() {
  const { t, toast } = useApp();
  const navigate = useNavigate();
  const read = useCallback((signal: AbortSignal) => api.tickets(undefined, signal), []);
  const { data, error, busy, reload } = useRead<TicketList>("departments", read);
  const [subject, setSubject] = useState("");
  const [text, setText] = useState("");
  const [department, setDepartment] = useState("");
  const [saving, setSaving] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const guard = useRef(false);
  const live = useLive();
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (guard.current || busy || !data || !subject.trim() || !text.trim()) return;
    guard.current = true; setSaving(true); setWriteError(null);
    try {
      const result = await api.newTicket({ subject: subject.trim(), text: text.trim(), department_id: department ? Number(department) : null });
      if (!live.current) return;
      toast(t("tickets_created"));
      navigate(`/tickets/${result.ticket.id}`, { replace: true });
    } catch (err) {
      if (live.current) setWriteError(err instanceof ApiError && err.status < 500 ? errorText(t, err) : t("tickets_uncertain"));
    } finally { guard.current = false; if (live.current) setSaving(false); }
  };
  return <Screen title={t("tickets_new")}>
    <p className="mb-3 text-sm text-[var(--hint)]">{t("tickets_text_only")}</p>
    <ReadError error={error} retry={() => void reload()} />
    {busy && <p role="status">{t("tickets_loading")}</p>}
    <form onSubmit={event => void submit(event)}>
      <fieldset disabled={saving} className="min-w-0 space-y-4">
        <label className="block">{t("tickets_subject")}<input aria-label={t("tickets_subject")} className={input} value={subject} maxLength={128} required onChange={event => setSubject(event.target.value)} /></label>
        <label className="block">{t("tickets_department")}<select aria-label={t("tickets_department")} className={input} value={department} disabled={busy || !data} onChange={event => setDepartment(event.target.value)}>
          <option value="">{t("tickets_general")}</option>{data?.departments.map(desk => <option key={desk.id} value={desk.id}>{desk.title}</option>)}
        </select></label>
        <label className="block">{t("tickets_message")}<textarea aria-label={t("tickets_message")} className={input} value={text} rows={6} maxLength={4000} required onChange={event => setText(event.target.value)} /></label>
        <p className="text-xs text-[var(--hint)]">{t("tickets_characters", { count: text.length, max: 4000 })}</p>
        <button type="submit" className={control} disabled={saving || busy || !data || !subject.trim() || !text.trim()}>{t(saving ? "tickets_sending" : "tickets_send")}</button>
      </fieldset>
    </form>
    {writeError && <p role="alert" className="my-3">{writeError}</p>}
    <button type="button" className={control} disabled={saving} onClick={() => navigate("/tickets")}>{t("tickets_back")}</button>
  </Screen>;
}

export function TicketThread() {
  const { id } = useParams();
  const { t } = useApp();
  const navigate = useNavigate();
  const value = Number(id);
  if (!id || !/^\d+$/.test(id) || !Number.isSafeInteger(value) || value <= 0) {
    return <Screen title={t("tickets_title")}><p role="alert">{t("err_ticket_not_found")}</p><button type="button" className={control} onClick={() => navigate("/tickets")}>{t("tickets_back")}</button></Screen>;
  }
  // A route change discards the former ticket's draft/confirmation and all local response state.
  return <Thread key={value} id={value} />;
}

function Thread({ id }: { id: number }) {
  const { t, locale, toast } = useApp();
  const navigate = useNavigate();
  const read = useCallback((signal: AbortSignal) => api.ticket(id, undefined, signal), [id]);
  const { data, error, busy, reload } = useRead<TicketThreadData>(String(id), read);
  const [older, setOlder] = useState<TicketThreadData["messages"]>([]);
  const [cursor, setCursor] = useState<number | null | undefined>();
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState<Ticket | null>(null);
  const guard = useRef(false);
  const live = useLive();
  const ticket = confirmed ?? data?.ticket;
  const unavailable = saving || busy;
  const run = async (action: "reply" | "close" | "older") => {
    if (guard.current || unavailable || !data || (action === "reply" && (!text.trim() || ticket?.status === "closed"))) return;
    guard.current = true; setSaving(true); setWriteError(null);
    try {
      if (action === "older") {
        const before = cursor === undefined ? data.next_before : cursor;
        if (!before) return;
        const result = await api.ticket(id, before);
        if (!live.current) return;
        setOlder(previous => [...result.messages, ...previous]); setCursor(result.next_before);
      } else {
        const result = action === "close" ? await api.closeTicket(id) : await api.replyTicket(id, text.trim());
        if (!live.current) return;
        setConfirmed(result.ticket); setConfirmClose(false);
        if (action === "reply") setText("");
        toast(t(action === "close" ? "tickets_closed_ok" : "tickets_replied"));
        setOlder([]); setCursor(undefined);
        await reload();
      }
    } catch (err) {
      if (live.current) {
        setWriteError(action === "older" || (err instanceof ApiError && err.status < 500) ? errorText(t, err) : t("tickets_uncertain"));
        if (err instanceof ApiError && err.code === "ticket_closed") { setConfirmClose(false); await reload(); }
      }
    } finally { guard.current = false; if (live.current) setSaving(false); }
  };
  const refresh = () => { if (unavailable || guard.current) return; setOlder([]); setCursor(undefined); setConfirmClose(false); void reload(); };
  // The latest read takes precedence over the last write after refresh, while a failed read
  // still displays the server-confirmed closed status and does not re-enable reply.
  useEffect(() => { if (data) setConfirmed(null); }, [data]);
  const messages = [...new Map([...older, ...(data?.messages ?? [])].map(message => [message.id, message])).values()].sort((a, b) => a.id - b.id);
  return <Screen title={t("tickets_title")}>
    {ticket && <h2 className="mb-3 break-words text-xl font-bold">#{ticket.id} — {ticket.subject}</h2>}
    <button type="button" className={control} disabled={saving} onClick={() => navigate("/tickets")}>{t("tickets_back")}</button>
    <button type="button" className={control} disabled={unavailable} onClick={refresh}>{t("tickets_refresh")}</button>
    <ReadError error={error} retry={refresh} />
    {busy && <p role="status">{t("tickets_loading")}</p>}
    {ticket && <p className="my-3"><TicketStatus ticket={ticket} /></p>}
    {(cursor === undefined ? data?.next_before : cursor) && <button type="button" className={control} disabled={unavailable} onClick={() => void run("older")}>{t("tickets_older_messages")}</button>}
    <ol aria-label={t("tickets_conversation")} className="my-4 space-y-3">{messages.map(message => <li key={message.id}>
      <Panel><p className="text-sm font-bold">{t(message.from === "me" ? "tickets_me" : "tickets_support")}</p>
        <p className="mt-2 whitespace-pre-wrap break-words" dir="auto">{message.text}</p>
        {message.has_attachment && <p className="mt-2 text-sm text-[var(--hint)]">{t("tickets_attachment")}</p>}
        <time className="mt-2 block text-xs text-[var(--hint)]">{message.created_at ? new Date(message.created_at).toLocaleString(locale) : ""}</time>
      </Panel>
    </li>)}</ol>
    {writeError && <p role="alert">{writeError}</p>}
    {ticket?.status === "closed" ? <p role="status">{t("tickets_closed_hint")}</p> : data && <>
      <form onSubmit={event => { event.preventDefault(); void run("reply"); }}>
        <label className="block">{t("tickets_reply")}<textarea aria-label={t("tickets_reply")} className={input} value={text} disabled={unavailable || confirmClose} rows={5} maxLength={4000} required onChange={event => setText(event.target.value)} /></label>
        <p className="text-xs text-[var(--hint)]">{t("tickets_characters", { count: text.length, max: 4000 })}</p>
        <button type="submit" className={control} disabled={unavailable || confirmClose || !text.trim()}>{t(saving ? "tickets_sending" : "tickets_send_reply")}</button>
      </form>
      {confirmClose ? <section role="group" aria-label={t("tickets_close_confirm")} className="my-4 rounded-2xl border border-[var(--line)] p-4">
        <p>{t("tickets_close_confirm")}</p>
        <button type="button" className={control} disabled={unavailable} onClick={() => void run("close")}>{t("tickets_close_yes")}</button>
        <button type="button" className={control} disabled={unavailable} onClick={() => setConfirmClose(false)}>{t("tickets_cancel")}</button>
      </section> : <button type="button" className={control} disabled={unavailable} onClick={() => setConfirmClose(true)}>{t("tickets_close")}</button>}
    </>}
    {saving && <p role="status">{t("tickets_loading")}</p>}
    <p className="my-3 text-sm text-[var(--hint)]">{t("tickets_text_only")}</p>
  </Screen>;
}
