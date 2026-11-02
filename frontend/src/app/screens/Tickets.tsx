import { TicketAttachment } from "@shared/TicketAttachment";
import { TicketUpload } from "../ticketMedia/TicketUpload";
import { intent } from "../ticketMedia/model";
import { initData } from "../telegram";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError } from "@shared/http";
import { api, type Ticket, type TicketList, type TicketThreadData } from "../api";
import { Icon, type IconName } from "../icons";
import { errorText, useApp } from "../state";
import { tg } from "../telegram";
import { ActionBar, Button, Item, Screen, Spinner } from "../ui";
import { readTicketPage, ticketListPath, ticketNumber, ticketStatus, ticketsReturnTo, type TicketFilter } from "../ticket-pages";

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
  return { ...state, error: state.key === key ? state.error : null, data: state.key === key ? state.data : null, busy: state.key !== key || state.busy, reload };
}

function useLive() {
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  return live;
}

function StatusBadge({ status }: { status: Ticket["status"] }) {
  const { t } = useApp();
  return (
    <span className="app-badge" data-tone={status === "answered" ? "green" : status === "closed" ? "gray" : undefined}>
      {t(`ticket_status_${status}`)}
    </span>
  );
}

function ReadError({ error, retry }: { error: unknown; retry: () => void }) {
  const { t } = useApp();
  if (!error) return null;
  return (
    <div className="app-note" data-tone="danger" role="alert">
      <Icon name="help" size={20} />
      <span className="app-grow">
        {errorText(t, error)}
        <Button tone="quiet" className="mt-2" onClick={retry}>{t("tickets_retry")}</Button>
      </span>
    </div>
  );
}



const DEPT_ICONS: IconName[] = ["chat", "globe", "wallet", "bag", "headset"];

export function Tickets() {
  const { me, t, locale } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const before = ticketNumber(params.get("before"));
  const status = ticketStatus(params.get("status"));
  const department = ticketNumber(params.get("department_id"), true);
  const requestKey = JSON.stringify([before ?? null, status, department ?? null]);
  const returnTo = ticketListPath(before, { status, department });
  const read = useCallback(async (signal: AbortSignal) => readTicketPage(
    await api.tickets(before, signal, { status, department }), before, { status, department },
  ), [before, status, department]);
  const { data, error, busy, reload } = useRead<TicketList>(requestKey, read);
  const [knownDesks, setKnownDesks] = useState<TicketList["departments"]>([]);
  useEffect(() => { if (data) setKnownDesks(data.filter_departments ?? data.departments); }, [data]);
  const desks = data?.filter_departments ?? data?.departments ?? knownDesks;
  const items = data?.items ?? [];
  const filtered = Boolean(status || department !== undefined);
  const change = (nextStatus: TicketFilter, nextDepartment: number | undefined, nextBefore?: number) => {
    const path = ticketListPath(nextBefore, { status: nextStatus, department: nextDepartment });
    setParams(path.includes("?") ? path.slice(path.indexOf("?") + 1) : "", { replace: true });
  };

  return (
    <Screen
      title={t("tickets_title")}
      subtitle={t("tickets_order_hint")}
      action={
        <button
          type="button"
          className="app-iconbtn app-rip-host"
          data-busy={busy}
          disabled={busy}
          aria-label={t("tickets_refresh")}
          onClick={() => void reload()}
        >
          <Icon name="refresh" size={20} />
        </button>
      }
    >
      <div className="app-card">
        <Item icon={<Icon name="plus" />} tone="green" title={t("tickets_new")} sub={t("tickets_new_sub")} to="/tickets/new" />
        {me?.shop.support && (
          <Item
            icon={<Icon name="headset" />}
            title={t("tickets_admin")}
            sub={t("tickets_admin_sub")}
            onClick={() => tg?.openTelegramLink(`https://t.me/${me.shop.support}`)}
          />
        )}
      </div>

      <div className="app-chips" role="group" aria-label={t("tickets_status_label")}>
        {[{ id: "" as TicketFilter, label: t("all") }, ...(["open", "answered", "closed"] as const).map((value) => ({ id: value, label: t(`ticket_status_${value}`) }))].map((chip) => (
          <button key={chip.id ?? "all"} type="button" aria-pressed={status === chip.id} className="app-chip" onClick={() => change(chip.id, department)}>
            {chip.label}
          </button>
        ))}
      </div>
      <div className="app-chips" role="group" aria-label={t("tickets_department")}>
          {[{ id: undefined, title: t("all") }, { id: 0, title: t("tickets_general") }, ...desks, ...(department !== undefined && department > 0 && !desks.some(desk => desk.id === department) ? [{ id: department, title: `#${department}` }] : [])].map((chip) => (
            <button key={chip.id ?? "all"} type="button" aria-pressed={department === chip.id} className="app-chip" onClick={() => change(status, chip.id)}>
              {chip.title}
            </button>
          ))}
      </div>

      {filtered && <Button tone="quiet" className="mt-2" onClick={() => change("", undefined)}>{t("tickets_clear_filters")}</Button>}

      <ReadError error={error} retry={() => void reload()} />

      {busy && (
        <div role="status" className="flex items-center justify-center gap-2 py-6 text-[var(--hint)]">
          <Spinner /> <span className="text-sm">{t("tickets_loading")}</span>
        </div>
      )}
      {!busy && !error && data && items.length === 0 && <div className="app-card"><div className="app-empty">{t(filtered ? "tickets_no_matches" : "tickets_empty")}</div></div>}
      {items.length > 0 && (
        <div className="app-card">
          {items.map((ticket) => (
            <Item
              key={ticket.id}
              icon={<Icon name={ticket.status === "closed" ? "check" : ticket.status === "answered" ? "chat" : "clock"} />}
              tone={ticket.status === "answered" ? "green" : ticket.status === "closed" ? "gray" : undefined}
              title={`#${ticket.id} — ${ticket.subject}`}
              sub={ticket.updated_at ? new Date(ticket.updated_at).toLocaleString(locale) : ""}
              badge={<StatusBadge status={ticket.status} />}
              onClick={() => navigate(`/tickets/${ticket.id}`, { state: { ticketsReturnTo: returnTo } })}
            />
          ))}
        </div>
      )}

      {data?.next_before != null && (
        <Button tone="quiet" className="mt-3" disabled={busy} onClick={() => change(status, department, data?.next_before ?? undefined)}>
          {t("tickets_older")}
        </Button>
      )}
      {before !== undefined && (
        <Button tone="quiet" className="mt-2" disabled={busy} onClick={() => change(status, department)}>
          {t("tickets_latest")}
        </Button>
      )}
    </Screen>
  );
}

export function NewTicket() {
  const [params] = useSearchParams();
  const raw = params.get("service");
  const service = raw === null ? null : /^\d+$/.test(raw) && Number.isSafeInteger(Number(raw)) && Number(raw) > 0 ? Number(raw) : -1;
  return <NewTicketForm key={String(service)} serviceId={service} />;
}

function NewTicketForm({ serviceId }: { serviceId: number | null }) {
  const { t, toast } = useApp();
  const navigate = useNavigate();
  const read = useCallback((signal: AbortSignal) => api.tickets(undefined, signal), []);
  const { data, error, busy, reload } = useRead<TicketList>("departments", read);
  const [subject, setSubject] = useState("");
  const request = useRef<{ body: string; key: string } | null>(null);
  const serviceRead = useCallback((signal: AbortSignal) => serviceId === null ? Promise.resolve(null) : api.service(serviceId).then(r => { if (signal.aborted) throw new Error("aborted"); return r.service; }), [serviceId]);
  const linked = useRead(String(serviceId), serviceRead);
  const [text, setText] = useState("");
  const [department, setDepartment] = useState("");
  const [saving, setSaving] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const guard = useRef(false);
  const live = useLive();
  const blocked = saving || busy || !data || !subject.trim() || !text.trim() || (serviceId !== null && (linked.busy || !!linked.error || !linked.data));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (guard.current || blocked) return;
    guard.current = true; setSaving(true); setWriteError(null);
    try {
      const body = { subject: subject.trim(), text: text.trim(), department_id: department ? Number(department) : null, service_id: serviceId };
      request.current = intent(request.current, JSON.stringify(body));
      const result = await api.newTicket({ ...body, request_key: request.current.key });
      if (!live.current) return;
      toast(t("tickets_created"));
      navigate(`/tickets/${result.ticket.id}`, { replace: true });
    } catch (err) {
      if (live.current) setWriteError(err instanceof ApiError && err.status < 500 ? errorText(t, err) : t("tickets_uncertain"));
    } finally { guard.current = false; if (live.current) setSaving(false); }
  };
  return (
    <Screen title={t("tickets_new")}>
      <div className="app-note">
        <Icon name="help" size={20} />
        <span>{t("tickets_media_hint")}</span>
      </div>
      <ReadError error={error} retry={() => void reload()} />
      {serviceId !== null && <div className="app-note"><span>{t("tickets_service")} #{serviceId} {linked.data ? `— ${linked.data.label}` : ""}</span><ReadError error={linked.error} retry={() => void linked.reload()} /></div>}
      <form onSubmit={(event) => void submit(event)}>
        <fieldset disabled={saving} className="min-w-0">
          <p className="app-lbl">{t("tickets_department")}</p>
          <div className="app-depts">
            <button type="button" className="app-dept" aria-pressed={department === ""} onClick={() => setDepartment("")} disabled={busy || !data}>
              <Icon name="headset" size={22} />
              <b>{t("tickets_general")}</b>
            </button>
            {data?.departments.map((desk, index) => (
              <button key={desk.id} type="button" className="app-dept" aria-pressed={department === String(desk.id)}
                disabled={busy || !data} onClick={() => setDepartment(String(desk.id))}>
                <Icon name={DEPT_ICONS[index % DEPT_ICONS.length] ?? "chat"} size={22} />
                <b>{desk.title}</b>
              </button>
            ))}
          </div>
          <p className="app-lbl">{t("tickets_subject")}</p>
          <input aria-label={t("tickets_subject")} value={subject} maxLength={128} required
            placeholder={t("tickets_subject_placeholder")} onChange={(event) => setSubject(event.target.value)} />
          <p className="app-lbl mt-4">{t("tickets_message")}</p>
          <textarea aria-label={t("tickets_message")} value={text} rows={8} maxLength={4000} required
            placeholder={t("tickets_message_placeholder")} onChange={(event) => setText(event.target.value)} />
          <p className="app-hint mt-2 px-1 text-xs">{t("tickets_characters", { count: text.length, max: 4000 })}</p>
        </fieldset>
        {writeError && <p role="alert" className="app-note mt-4" data-tone="danger">{writeError}</p>}
        <ActionBar
          action={t(saving ? "tickets_sending" : "tickets_send")}
          submit
          busy={saving}
          disabled={blocked}
        />
      </form>
    </Screen>
  );
}

export function TicketThread() {
  const { id } = useParams();
  const { t } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const backTo = ticketsReturnTo(location.state?.ticketsReturnTo);
  const value = Number(id);
  if (!id || !/^\d+$/.test(id) || !Number.isSafeInteger(value) || value <= 0) {
    return <Screen title={t("tickets_title")} backTo={backTo}><p role="alert">{t("err_ticket_not_found")}</p><Button tone="quiet" onClick={() => navigate(backTo)}>{t("tickets_back")}</Button></Screen>;
  }
  // A route change discards the former ticket's draft/confirmation and all local response state.
  return <Thread key={value} id={value} backTo={backTo} />;
}

function Thread({ id, backTo }: { id: number; backTo: string }) {
  const { t, locale, toast } = useApp();
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
  const [mediaBusy, setMediaBusy] = useState(false);
  const replyIntent = useRef<{ body: string; key: string } | null>(null);
  const unavailable = saving || busy || mediaBusy;
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
        if (action === "reply") replyIntent.current = intent(replyIntent.current, text.trim());
        const result = action === "close" ? await api.closeTicket(id) : await api.replyTicket(id, text.trim(), replyIntent.current?.key);
        if (!live.current) return;
        setConfirmed(result.ticket); setConfirmClose(false);
        if (action === "reply") { setText(""); replyIntent.current = null; }
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
  return (
    <Screen
      backTo={backTo}
      title={ticket?.subject ?? t("tickets_title")}
      subtitle={ticket ? `#${ticket.id}` : undefined}
      action={
        <button type="button" className="app-iconbtn app-rip-host" data-busy={busy} aria-label={t("tickets_refresh")} onClick={refresh}>
          <Icon name="refresh" size={20} />
        </button>
      }
    >
      {ticket && (
        <div className="app-card p-4">
          <div className="app-ln">
            <span className="app-hint">{t("tickets_status_label")}</span>
            <StatusBadge status={ticket.status} />
          </div>
          <div className="app-ln">
            <span className="app-hint">{t("tickets_created_label")}</span>
            <span className="num font-semibold">
              {ticket.created_at ? new Date(ticket.created_at).toLocaleString(locale) : "—"}
            </span>
          </div>
          {ticket.status === "closed" ? null : confirmClose ? (
            <>
              <p className="app-hint mt-2 text-xs">{t("tickets_close_confirm")}</p>
              <div className="mt-2 flex gap-2">
                <Button tone="danger" busy={saving} onClick={() => void run("close")}>{t("tickets_close_yes")}</Button>
                <Button tone="quiet" onClick={() => setConfirmClose(false)}>{t("tickets_cancel")}</Button>
              </div>
            </>
          ) : (
            <button type="button" className="mt-1 min-h-11 font-bold text-[var(--danger)]" disabled={unavailable}
              onClick={() => setConfirmClose(true)}>
              {t("tickets_close")}
            </button>
          )}
        </div>
      )}

      {ticket?.service_context && <p className="app-note">{t("tickets_service")} #{ticket.service_context.id} — {ticket.service_context.status}</p>}
      <ReadError error={error} retry={refresh} />
      {(cursor === undefined ? data?.next_before : cursor) != null && (
        <Button tone="quiet" className="mt-3" disabled={unavailable} onClick={() => void run("older")}>
          {t("tickets_older_messages")}
        </Button>
      )}

      <ol aria-label={t("tickets_conversation")} className="app-chat my-2">
        {messages.map((message) => (
          <li key={message.id} className="app-msg" data-from={message.from}>
            <span className="whitespace-pre-wrap break-words" dir="auto">{message.text}</span>
            {message.has_attachment && (message.attachment_expired ? <p>{t("tickets_file_expired")}</p> : <TicketAttachment key={`${id}-${message.id}`} path={`/api/webapp/tickets/${id}/messages/${message.id}/attachment`} headers={{ "X-Telegram-Init-Data": initData() }} label={t("tickets_file_open")} failure={t("tickets_file_failed")} loading={t("tickets_loading")} />)}
            <time className="num">
              {message.from === "support" ? `${t("tickets_support")} · ` : ""}
              {message.created_at ? new Date(message.created_at).toLocaleString(locale) : ""}
            </time>
            {message.from === "support" && <Reaction key={`r-${message.id}`} ticketId={id} messageId={message.id} initial={message.reaction ?? null} />}
          </li>
        ))}
      </ol>

      {writeError && <p role="alert" className="app-note" data-tone="danger">{writeError}</p>}
      {ticket?.status === "closed" ? (
        <p className="app-hint mx-auto mt-4 max-w-xs text-center text-sm">{t("tickets_closed_hint")}</p>
      ) : data && (
        <form
          className="app-cmp"
          onSubmit={(event) => { event.preventDefault(); void run("reply"); }}
        >
          <textarea
            aria-label={t("tickets_reply")}
            className="max-h-40"
            value={text}
            disabled={unavailable || confirmClose}
            rows={2}
            maxLength={4000}
            required
            placeholder={t("tickets_reply_placeholder")}
            onChange={(event) => setText(event.target.value)}
          />
          <button
            type="submit"
            className="app-send app-rip-host"
            aria-label={t(saving ? "tickets_sending" : "tickets_send_reply")}
            disabled={unavailable || confirmClose || !text.trim()}
          >
            <Icon name="send" size={19} />
          </button>
        </form>
      )}
      {data && ticket?.status !== "closed" && <TicketUpload key={id} id={id} disabled={saving || busy || confirmClose} onBusy={setMediaBusy} onSaved={() => { setOlder([]); setCursor(undefined); void reload(); }} />}
      {saving && <p role="status" className="sr-only">{t("tickets_loading")}</p>}
    </Screen>
  );
}

/** A thumbs up or down on a support reply (F-22); pressing the chosen one again takes it back. */
function Reaction({ ticketId, messageId, initial }: { ticketId: number; messageId: number; initial: "up" | "down" | null }) {
  const { t, toast } = useApp();
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);
  const choose = async (next: "up" | "down") => {
    if (busy) return;
    const wanted = value === next ? null : next;
    setBusy(true);
    try {
      setValue((await api.reactTicket(ticketId, messageId, wanted)).reaction);
    } catch (err) {
      toast(errorText(t, err), "error");
    } finally {
      setBusy(false);
    }
  };
  return <span className="mt-1 flex gap-1" role="group" aria-label={t("tickets_rate")}>
    {(["up", "down"] as const).map((kind) => (
      <button key={kind} type="button" className="app-chip px-2 py-1" aria-pressed={value === kind} disabled={busy}
        aria-label={t(`tickets_rate_${kind}`)} onClick={() => void choose(kind)}>
        {kind === "up" ? "👍" : "👎"}
      </button>
    ))}
  </span>;
}
