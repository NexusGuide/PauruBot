import { TicketAttachment } from "../../shared/TicketAttachment";
import { useRef, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, call, uploadRaw } from "../api";
import { useSession } from "../session";
import { Avatar, Badge, Button, Chips, Icon, Page, Section, Segmented, Select, Textarea, Toggle } from "../ui";
import {
  PRIORITIES,
  listPath,
  parseDepartments,
  parseDetail,
  parseList,
  parseCannedList,
  parseTicket,
  record,
  positive,
  newId,
  ok,
  numeric,
  ticketPatch,
  validText,
  points,
  renderCanned,
  type Detail,
  type Department,
  type Message,
  type Priority,
  type ServiceContext,
  type Ticket,
} from "./model";
import { translator, type T, type Key } from "./copy";
import { useRead, Notice, Confirmation, stamp, type Proposal } from "./ui";
import { DeskSettings } from "./DeskSettings";
import "./tickets.css";

// The ticket desk, rebuilt on the panel's own components (2026 redesign): a clean list with status
// chips, and a ticket as a chat with its details beside it (below it on a phone).

const stateLabel = (status: string, t: T): string =>
  ["open", "answered", "closed"].includes(status) ? t(status as Key) : status;
const deptName = (id: number | null, rows: Department[] | null, t: T): string =>
  id === null ? t("unassignedDepartment") : (rows?.find((d) => d.id === id)?.title ?? `${t("unknownDepartment")} #${id}`);
const STATUS_TONE = { open: "brand", answered: "health", closed: "neutral" } as const;
const PRIORITY_TONE = { low: "neutral", normal: "neutral", high: "wait", urgent: "loss" } as const;

function StatusBadge({ status, t }: { status: string; t: T }) {
  return <Badge tone={STATUS_TONE[status as keyof typeof STATUS_TONE] ?? "neutral"}>{stateLabel(status, t)}</Badge>;
}

function PriorityBadge({ priority, t }: { priority: Priority; t: T }) {
  if (priority === "normal") return null;
  return <Badge tone={PRIORITY_TONE[priority]}>{t(priority)}</Badge>;
}

/** "3 hours ago" style, short; the exact time is in the title. */
function ago(value: string | null, locale: string): string {
  if (!value) return "—";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60_000));
  const rtf = new Intl.RelativeTimeFormat(locale === "fa" ? "fa-IR" : "en-GB", { numeric: "auto", style: "short" });
  if (minutes < 60) return rtf.format(-minutes, "minute");
  if (minutes < 60 * 24) return rtf.format(-Math.round(minutes / 60), "hour");
  return rtf.format(-Math.round(minutes / 1440), "day");
}

export function TicketDesk() {
  const { checking, can, locale, me } = useSession();
  const t = translator(locale);
  if (checking) return null;
  if (!can("tickets.read") || me?.role === "reseller")
    return (
      <Page title={t("title")}>
        <Notice error>{t("denied")}</Notice>
      </Page>
    );
  return <DeskList t={t} locale={locale} />;
}

function DeskList({ t, locale }: { t: T; locale: string }) {
  const { can } = useSession();
  const [params, setParams] = useSearchParams();
  const [settings, setSettings] = useState(false);
  const moreActive = Boolean(params.get("priority") || params.get("department_id") || params.get("assigned"));
  const [more, setMore] = useState(moreActive);
  const { path, page } = listPath(params);
  const list = useRead(path, parseList);
  const departments = useRead("/api/admin/ticket-departments", parseDepartments);
  const stats = useRead("/api/admin/tickets/stats", (raw) => {
    const r = record(raw);
    if (!r) return null;
    return {
      open: Number(r.open ?? 0),
      answered: Number(r.answered ?? 0),
      closed: Number(r.closed ?? 0),
      awaiting: Number(r.awaiting ?? 0),
      avg: typeof r.avg_first_reply_minutes === "number" ? r.avg_first_reply_minutes : null,
      up: Number(r.reactions_up ?? 0),
      down: Number(r.reactions_down ?? 0),
      csat: typeof r.satisfaction_percent === "number" ? r.satisfaction_percent : null,
    };
  });
  const filter = (key: string, value: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        next.delete("page");
        return next;
      },
      { replace: true },
    );
  const changePage = (value: number) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set("page", String(value));
        else next.delete("page");
        return next;
      },
      { replace: true },
    );
  const status = params.get("status") || "all";
  const waiting = params.get("awaiting") === "true";
  const total = list.data?.total ?? 0;

  return (
    <Page
      title={t("title")}
      wide
      actions={
        <>
          <Button
            icon="refresh"
            aria-label={t("refresh")}
            title={t("refresh")}
            disabled={list.loading}
            onClick={() => {
              void list.reload();
              void departments.reload();
              void stats.reload();
            }}
          />
          {can("settings.write") && (
            <Button icon="settings" aria-expanded={settings} onClick={() => setSettings(!settings)}>
              {t("settings")}
            </Button>
          )}
        </>
      }
    >
      {stats.data && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="rounded-2xl bg-[var(--surface)] px-4 py-3 ring-1 ring-[var(--line)]">
            <p className="text-[0.76rem] text-[var(--hint)]">{t("statsOpen")}</p>
            <p className="num text-[1.25rem] font-extrabold">{stats.data.open}</p>
          </div>
          <div className="rounded-2xl bg-[var(--surface)] px-4 py-3 ring-1 ring-[var(--line)]">
            <p className="text-[0.76rem] text-[var(--hint)]">{t("statsAwaiting")}</p>
            <p className="num text-[1.25rem] font-extrabold text-saffron-700">{stats.data.awaiting}</p>
          </div>
          <div className="rounded-2xl bg-[var(--surface)] px-4 py-3 ring-1 ring-[var(--line)]">
            <p className="text-[0.76rem] text-[var(--hint)]">{t("statsAvgReply")}</p>
            <p className="num text-[1.25rem] font-extrabold">
              {stats.data.avg !== null ? `${stats.data.avg} ${t("minutesUnit")}` : "—"}
            </p>
          </div>
          <div className="rounded-2xl bg-[var(--surface)] px-4 py-3 ring-1 ring-[var(--line)]">
            <p className="text-[0.76rem] text-[var(--hint)]">{t("statsCsat")}</p>
            <p className="num text-[1.25rem] font-extrabold text-pista-700">
              {stats.data.csat !== null ? `${stats.data.csat}%` : "—"}
              {stats.data.up + stats.data.down > 0 && (
                <span className="ms-1.5 text-[0.74rem] font-normal text-[var(--hint)]">
                  (👍 {stats.data.up} / 👎 {stats.data.down})
                </span>
              )}
            </p>
          </div>
        </div>
      )}
            {settings && (
        <Section>
          <div className="td td-embed">
            <DeskSettings t={t} onChanged={() => void departments.reload()} />
          </div>
          <p className="mt-3 text-[0.84rem] text-[var(--hint)]">
            {t("autoCloseHint")}{" "}
            <Link className="font-semibold text-firouzeh-700" to="/settings?group=tickets">
              {t("autoCloseLink")}
            </Link>
          </p>
        </Section>
      )}

      <div className="flex flex-col gap-3">
        <Chips
          label={t("status")}
          value={status}
          onChange={(value) => filter("status", value === "all" ? "" : value)}
          options={[
            { value: "all", label: t("allStatuses") },
            { value: "open", label: stateLabel("open", t) },
            { value: "answered", label: stateLabel("answered", t) },
            { value: "closed", label: stateLabel("closed", t) },
          ]}
        />
        <div className="flex flex-wrap gap-2">
          <Button small tone={waiting ? "primary" : "quiet"} aria-pressed={waiting} onClick={() => filter("awaiting", waiting ? "" : "true")}>
            {t("waitingChip")}
          </Button>
          <Button small icon="sort" tone={moreActive ? "primary" : "quiet"} aria-expanded={more} onClick={() => setMore(!more)}>
            {t("moreFilters")}
          </Button>
        </div>
        {more && (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <Select aria-label={t("priority")} value={params.get("priority") || ""} onChange={(e) => filter("priority", e.target.value)}>
              <option value="">
                {t("priority")}: {t("all")}
              </option>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {t(p)}
                </option>
              ))}
            </Select>
            <Select aria-label={t("department")} value={params.get("department_id") || ""} onChange={(e) => filter("department_id", e.target.value)}>
              <option value="">
                {t("department")}: {t("all")}
              </option>
              <option value="0">{t("unassignedDepartment")}</option>
              {departments.data?.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                  {!d.is_active ? ` (${t("inactive")})` : ""}
                </option>
              ))}
            </Select>
            <Select aria-label={t("assigned")} value={params.get("assigned") || ""} onChange={(e) => filter("assigned", e.target.value)}>
              <option value="">
                {t("assigned")}: {t("all")}
              </option>
              <option value="me">{t("me")}</option>
              <option value="none">{t("none")}</option>
            </Select>
          </div>
        )}
      </div>

      {departments.error && <p className="text-[0.82rem] text-[var(--hint)]">{t("department")}: {t("error")}</p>}
      {list.error && <Notice error>{t(list.data ? "stale" : "error")}</Notice>}

      <Section flush>
        {list.loading && !list.data && <p className="px-5 py-8 text-center text-[var(--hint)]">{t("loading")}</p>}
        {!list.loading && !list.error && list.data?.items.length === 0 && (
          <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
            <span className="grid h-12 w-12 place-items-center rounded-full bg-pista-100 text-pista-700">
              <Icon name="check" size={24} />
            </span>
            <p className="font-bold">{t("empty")}</p>
          </div>
        )}
        {(list.data?.items.length ?? 0) > 0 && (
          <ul className="divide-y divide-[var(--line)]">
            {list.data?.items.map((row) => (
              <li key={row.id}>
                <TicketRow row={row} departments={departments.data} t={t} locale={locale} />
              </li>
            ))}
          </ul>
        )}
        {total > 25 && (
          <nav className="flex items-center justify-between gap-3 border-t border-[var(--line)] px-4 py-3 text-[0.82rem]" aria-label={t("page")}>
            <span className="text-[var(--hint)]">
              {t("page")} {(page + 1).toLocaleString(locale === "fa" ? "fa-IR" : "en-US")}
            </span>
            <span className="flex gap-2">
              <Button small disabled={page === 0 || list.loading} onClick={() => changePage(page - 1)}>
                {t("previous")}
              </Button>
              <Button small disabled={list.loading || list.error || (page + 1) * 25 >= total} onClick={() => changePage(page + 1)}>
                {t("next")}
              </Button>
            </span>
          </nav>
        )}
      </Section>
    </Page>
  );
}

function TicketRow({ row, departments, t, locale }: { row: Ticket; departments: Department[] | null; t: T; locale: string }) {
  const name = row.customer ?? `#${row.customer_id}`;
  const when = row.awaiting ? row.last_customer_at : row.updated_at;
  return (
    <Link to={`/tickets/${row.id}`} className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-[var(--surface-2)] md:px-5">
      <span className="relative">
        <Avatar id={row.customer_id} name={name} size={40} />
        {row.awaiting && (
          <span className="absolute -end-0.5 -top-0.5 h-3 w-3 rounded-full bg-saffron-500 ring-2 ring-[var(--surface)]" title={t("waiting")} />
        )}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex min-w-0 items-center gap-2">
          <bdi className={`truncate text-[0.93rem] ${row.awaiting ? "font-extrabold" : "font-semibold"}`}>{row.subject}</bdi>
        </span>
        <span className="truncate text-[0.78rem] text-[var(--hint)]">
          <bdi>{name}</bdi> · #{row.id}
          {row.department_id !== null && <> · {deptName(row.department_id, departments, t)}</>}
        </span>
      </span>
      <span className="flex shrink-0 flex-col items-end gap-1">
        <span className={`text-[0.74rem] ${row.awaiting ? "font-bold text-saffron-700" : "text-[var(--hint)]"}`} title={stamp(when, locale)}>
          {ago(when, locale)}
        </span>
        <span className="flex gap-1">
          <PriorityBadge priority={row.priority} t={t} />
          <StatusBadge status={row.status} t={t} />
        </span>
      </span>
    </Link>
  );
}

export function StaffTicketPage() {
  const { id } = useParams();
  const { checking, can, me, locale } = useSession();
  const t = translator(locale);
  if (checking) return null;
  if (!can("tickets.read") || me?.role === "reseller")
    return (
      <Page title={t("title")}>
        <Notice error>{t("denied")}</Notice>
      </Page>
    );
  const ticketId = id && /^\d+$/.test(id) ? Number(id) : 0;
  if (!positive(ticketId))
    return (
      <Page title={t("title")}>
        <Notice error>{t("error")}</Notice>
      </Page>
    );
  return <Thread key={ticketId} id={ticketId} t={t} locale={locale} />;
}

function Thread({ id, t, locale }: { id: number; t: T; locale: string }) {
  const state = useRead(`/api/admin/tickets/${id}`, parseDetail);
  const departments = useRead("/api/admin/ticket-departments", parseDepartments);
  const [notice, setNotice] = useState("");
  const reload = () => {
    void state.reload();
    void departments.reload();
  };
  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-28 pt-4 md:px-8 md:pb-12 md:pt-6">
      <Link to="/tickets" className="mb-3 inline-flex items-center gap-1.5 text-[0.85rem] font-semibold text-[var(--hint)] hover:text-[var(--text)]">
        <Icon name="chevron" size={16} className="rtl:rotate-0 ltr:rotate-180" />
        {t("back")}
      </Link>
      {notice && <Notice>{notice}</Notice>}
      {state.loading && !state.data && <p className="py-10 text-center text-[var(--hint)]">{t("loading")}</p>}
      {state.error && <Notice error>{t(state.data ? "stale" : "error")}</Notice>}
      {state.data && state.data.ticket.id !== id && <Notice error>{t("error")}</Notice>}
      {state.data && state.data.ticket.id === id && (
        <ThreadBody
          key={id}
          detail={state.data}
          t={t}
          locale={locale}
          departments={departments.data}
          stale={state.error || state.loading}
          onRefresh={reload}
          onSaved={(msg) => {
            setNotice(msg);
            void state.reload();
          }}
        />
      )}
    </div>
  );
}

function ThreadBody({
  detail,
  t,
  locale,
  departments,
  stale,
  onRefresh,
  onSaved,
}: {
  detail: Detail;
  t: T;
  locale: string;
  departments: Department[] | null;
  stale: boolean;
  onRefresh: () => void;
  onSaved: (msg: string) => void;
}) {
  const { can, me, toast } = useSession();
  const row = detail.ticket;
  const writable = can("tickets.reply");
  const known = ["open", "answered", "closed"].includes(row.status);
  const [requiresCheck, setRequiresCheck] = useState<Detail | null>(null);
  const blocked = stale || requiresCheck !== null;
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const name = detail.customer ?? `#${row.customer_id}`;
  const close = () =>
    setProposal({
      title: t("closeTicket"),
      path: `/api/admin/tickets/${row.id}/close`,
      method: "POST",
      validate: ok,
      preview: (
        <>
          <p className="font-bold">
            #{row.id} — <bdi>{row.subject}</bdi>
          </p>
          <p className="mt-2 text-[0.9rem] text-[var(--hint)]">{t("closeHint")}</p>
        </>
      ),
    });
  const reopen = () =>
    setProposal({
      title: t("reopenTicket"),
      path: `/api/admin/tickets/${row.id}/reopen`,
      method: "POST",
      validate: ok,
      preview: (
        <>
          <p className="font-bold">
            #{row.id} — <bdi>{row.subject}</bdi>
          </p>
          <p className="mt-2 text-[0.9rem] text-[var(--hint)]">{t("reopenHint")}</p>
        </>
      ),
    });

  return (
    <>
      <header className="mb-4 flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <h1 className="text-[1.25rem] font-extrabold leading-snug md:text-[1.5rem]">
            <bdi>{row.subject}</bdi>
          </h1>
          <div className="flex flex-wrap items-center gap-1.5 text-[0.8rem] text-[var(--hint)]">
            <StatusBadge status={row.status} t={t} />
            <PriorityBadge priority={row.priority} t={t} />
            {row.is_complaint && <Badge tone="loss">{t("complaint")}</Badge>}
            <span>#{row.id}</span>
            <span>·</span>
            <span title={stamp(row.created_at, locale)}>{ago(row.created_at, locale)}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <Button icon="refresh" aria-label={t("refresh")} title={t("refresh")} disabled={stale} onClick={onRefresh} />
          {writable && known && row.status !== "closed" && (
            <Button tone="danger" icon="close" disabled={blocked} onClick={close}>
              {t("closeTicket")}
            </Button>
          )}
          {writable && known && row.status === "closed" && (
            <Button tone="primary" icon="refresh" disabled={blocked} onClick={reopen}>
              {t("reopenTicket")}
            </Button>
          )}
        </div>
      </header>

      {requiresCheck && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-2xl bg-saffron-100 px-4 py-3 text-[0.88rem] text-saffron-700">
          <span className="flex-1">{t("checkAgain")}</span>
          <Button small disabled={stale || requiresCheck === detail} onClick={() => setRequiresCheck(null)}>
            {t("checked")}
          </Button>
        </div>
      )}
      {!known && <Notice error>{t("unknownStatus")}</Notice>}

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[20px] bg-[var(--surface)] ring-1 ring-[var(--line)]" aria-label={t("conversation")}>
          <ol className="flex flex-col gap-3 px-3 py-4 md:px-5">
            {detail.messages.map((m) => (
              <Bubble key={m.id} m={m} ticketId={row.id} name={name} t={t} locale={locale} />
            ))}
          </ol>
          {detail.messages.length >= 200 && <p className="px-5 pb-3 text-center text-[0.72rem] text-[var(--hint)]">{t("historyHint")}</p>}
          {!writable && <p className="border-t border-[var(--line)] px-5 py-3 text-[0.85rem] text-[var(--hint)]">{t("writeDenied")}</p>}
          {writable && known && (
            <Composer
              row={row}
              customer={detail.customer ?? ""}
              t={t}
              blocked={blocked}
              onSent={(message) => {
                toast(message);
                onSaved(message);
              }}
              onUncertain={() => setRequiresCheck(detail)}
            />
          )}
        </section>

        <aside className="flex w-full flex-col gap-4 lg:sticky lg:top-6 lg:w-[320px] lg:shrink-0">
          <section className="rounded-[20px] bg-[var(--surface)] p-4 ring-1 ring-[var(--line)]">
            <Link to={`/customers/${row.customer_id}`} className="flex items-center gap-3 rounded-xl p-1 hover:bg-[var(--surface-2)]">
              <Avatar id={row.customer_id} name={name} size={44} />
              <span className="flex min-w-0 flex-col">
                <bdi className="truncate font-bold">{name}</bdi>
                <span className="text-[0.76rem] text-firouzeh-700">{t("openCustomer")}</span>
              </span>
            </Link>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 border-t border-[var(--line)] pt-3 text-[0.84rem]">
              <dt className="text-[var(--hint)]">{t("department")}</dt>
              <dd className="font-semibold">{deptName(row.department_id, departments, t)}</dd>
              <dt className="text-[var(--hint)]">{t("assigned")}</dt>
              <dd className="font-semibold">{row.assigned_staff_id === null ? t("none") : `#${row.assigned_staff_id}`}</dd>
              {row.awaiting && (
                <>
                  <dt className="text-[var(--hint)]">{t("waitingSince")}</dt>
                  <dd className="font-semibold text-saffron-700">{ago(row.last_customer_at, locale)}</dd>
                </>
              )}
            </dl>
            {row.service_context && <ServiceLine context={row.service_context} t={t} locale={locale} />}
          </section>
          {writable && known && (
            <Routing
              key={`${row.id}-${row.updated_at}-${row.department_id}-${row.assigned_staff_id}-${row.priority}-${row.is_complaint}`}
              row={row}
              departments={departments}
              t={t}
              staffId={me?.id ?? 0}
              disabled={blocked}
              propose={setProposal}
            />
          )}
        </aside>
      </div>

      {proposal && (
        <Confirmation
          proposal={proposal}
          t={t}
          onClose={(failed) => {
            setProposal(null);
            if (failed) setRequiresCheck(detail);
          }}
          onSaved={(message) => {
            setProposal(null);
            onSaved(message);
          }}
        />
      )}
    </>
  );
}

function Bubble({ m, ticketId, name, t, locale }: { m: Message; ticketId: number; name: string; t: T; locale: string }) {
  const staff = m.author_type === "staff";
  const who = m.is_internal ? t("internal") : staff ? t("staff") : m.author_type === "customer" ? name : m.author_type;
  const tone = m.is_internal
    ? "bg-saffron-100/70 text-[var(--text)] ring-1 ring-dashed ring-saffron-500/50"
    : staff
      ? "bg-firouzeh-100 text-[var(--text)]"
      : "bg-[var(--surface-2)] text-[var(--text)]";
  return (
    <li className={`flex flex-col gap-1 ${staff ? "items-end" : "items-start"}`}>
      <span className="px-1 text-[0.72rem] font-semibold text-[var(--hint)]">
        <bdi>{who}</bdi>
      </span>
      <div className={`max-w-[88%] rounded-2xl px-3.5 py-2.5 md:max-w-[75%] ${tone} ${staff ? "rounded-ee-md" : "rounded-es-md"}`}>
        {m.text && (
          <p className="whitespace-pre-wrap break-words text-[0.92rem] leading-relaxed" dir="auto">
            {m.text}
          </p>
        )}
        {m.has_attachment && (
          <div className="mt-1 text-[0.82rem]">
            {m.attachment_expired ? (
              <span className="text-[var(--hint)]">{t("attachmentExpired")}</span>
            ) : (
              <TicketAttachment
                key={`${ticketId}-${m.id}`}
                path={`/api/admin/tickets/${ticketId}/messages/${m.id}/attachment`}
                label={t("attachmentOpen")}
                failure={t("attachmentFailed")}
                loading={t("loading")}
              />
            )}
          </div>
        )}
      </div>
      <span className="px-1 text-[0.7rem] text-[var(--hint)]" title={stamp(m.created_at, locale)}>
        {ago(m.created_at, locale)}
        {m.reaction && <> · {t(m.reaction === "up" ? "reactedUp" : "reactedDown")}</>}
      </span>
    </li>
  );
}

/** Reply or note box under the conversation. A reply goes out at once; a failure that leaves the
 * outcome unknown asks for a refresh before another send, as before. */
function Composer({
  row,
  customer,
  t,
  blocked,
  onSent,
  onUncertain,
}: {
  row: Ticket;
  customer: string;
  t: T;
  blocked: boolean;
  onSent: (message: string) => void;
  onUncertain: () => void;
}) {
  const closed = row.status === "closed";
  const [mode, setMode] = useState<"reply" | "note">(closed ? "note" : "reply");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const canned = useRead(`/api/admin/canned-replies${row.department_id ? `?department_id=${row.department_id}` : ""}`, parseCannedList);
  const send = async () => {
    if (lock.current || blocked || !validText(draft) || (mode === "reply" && closed)) {
      if (!validText(draft)) setError(t("textError"));
      return;
    }
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const response = await call<unknown>(`/api/admin/tickets/${row.id}/${mode}`, { method: "POST", body: { text: draft.trim() } });
      if (mode === "reply") ok(response);
      else newId(response);
      setDraft("");
      onSent(t(mode === "reply" ? "replySaved" : "noteSaved"));
    } catch (e) {
      const rejected = e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408;
      setError(t(rejected ? "rejected" : "uncertain"));
      if (!rejected) onUncertain();
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <div className={`flex flex-col gap-2.5 border-t border-[var(--line)] p-3 md:p-4 ${mode === "note" ? "bg-saffron-100/30" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <Segmented
            label={t("mode")}
            value={mode}
            onChange={(value) => {
              if (value === "reply" && closed) return;
              setMode(value);
              setError("");
            }}
            options={[
              { value: "reply", label: t("replyTab") },
              { value: "note", label: t("noteTab") },
            ]}
          />
        </div>
        {canned.data && canned.data.length > 0 && (
          <Select
            className="w-auto max-w-[45%]"
            aria-label={t("canned")}
            value=""
            onChange={(e) => {
              const item = canned.data?.find((c) => String(c.id) === e.target.value);
              if (item) setDraft((prev) => [prev, renderCanned(item.text, customer, row.id)].filter(Boolean).join("\n"));
            }}
          >
            <option value="">{t("canned")}</option>
            {canned.data.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </Select>
        )}
      </div>
      {closed && <p className="text-[0.8rem] text-[var(--hint)]">{t("closedHint")}</p>}
      <Textarea
        dir="auto"
        aria-label={t("message")}
        placeholder={t(mode === "reply" ? "replyPlaceholder" : "notePlaceholder")}
        value={draft}
        disabled={blocked || busy}
        onChange={(e) => {
          setDraft(e.target.value);
          setError("");
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
        }}
        className="min-h-28"
      />
      {error && <p role="alert" className="text-[0.84rem] font-semibold text-anar-700">{error}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 text-[0.74rem] text-[var(--hint)]" dir="ltr">
          {points(draft)} / 4000
        </span>
        {mode === "reply" && !closed && (
          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-[var(--surface-2)] px-3 py-2 text-[0.84rem] font-semibold hover:opacity-85">
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp,text/plain,.log,.txt"
              className="hidden"
              disabled={blocked || busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (!file || lock.current) return;
                lock.current = true;
                setBusy(true);
                setError("");
                const headers: Record<string, string> = {};
                if (draft.trim()) headers["x-ticket-caption"] = encodeURIComponent(draft.trim().slice(0, 1000));
                void uploadRaw(`/api/admin/tickets/${row.id}/attachments`, file, headers)
                  .then(() => {
                    setDraft("");
                    onSent(t("attachSent"));
                  })
                  .catch(() => setError(t("attachFailed")))
                  .finally(() => {
                    lock.current = false;
                    setBusy(false);
                  });
              }}
            />
            <span>📎 {t("attachFile")}</span>
          </label>
        )}
        <Button
          tone="primary"
          icon="send"
          busy={busy}
          disabled={blocked || !validText(draft) || (mode === "reply" && closed)}
          onClick={() => void send()}
        >
          {t(mode === "reply" ? "sendReply" : "saveNote")}
        </Button>
      </div>
    </div>
  );
}

function Routing({
  row,
  departments,
  t,
  staffId,
  disabled,
  propose,
}: {
  row: Ticket;
  departments: Department[] | null;
  t: T;
  staffId: number;
  disabled: boolean;
  propose: (p: Proposal) => void;
}) {
  const [department, setDepartment] = useState(row.department_id === null ? "" : String(row.department_id));
  const [priority, setPriority] = useState<Priority>(row.priority);
  const [assigned, setAssigned] = useState(row.assigned_staff_id === null ? "" : String(row.assigned_staff_id));
  const [complaint, setComplaint] = useState(row.is_complaint);
  const [error, setError] = useState("");
  const save = () => {
    try {
      const body = ticketPatch(row, numeric(department, { positiveOnly: true }), priority, numeric(assigned, { positiveOnly: true }), complaint);
      if (!Object.keys(body).length) return;
      propose({
        title: t("metadata"),
        path: `/api/admin/tickets/${row.id}`,
        method: "PATCH",
        body,
        validate: (v) => {
          if (!record(v) || parseTicket(v.ticket).id !== row.id) throw new Error("invalid");
        },
        preview: (
          <dl className="grid grid-cols-2 gap-2 text-[0.88rem]">
            <dt className="text-[var(--hint)]">{t("department")}</dt>
            <dd>
              {deptName(row.department_id, departments, t)} → {deptName(numeric(department), departments, t)}
            </dd>
            <dt className="text-[var(--hint)]">{t("priority")}</dt>
            <dd>
              {t(row.priority)} → {t(priority)}
            </dd>
            <dt className="text-[var(--hint)]">{t("assigned")}</dt>
            <dd>
              {row.assigned_staff_id ?? t("none")} → {assigned || t("none")}
            </dd>
            <dt className="text-[var(--hint)]">{t("complaint")}</dt>
            <dd>
              {row.is_complaint ? t("yes") : t("no")} → {complaint ? t("yes") : t("no")}
            </dd>
          </dl>
        ),
      });
      setError("");
    } catch {
      setError(t("numberError"));
    }
  };
  const changed =
    department !== (row.department_id === null ? "" : String(row.department_id)) ||
    priority !== row.priority ||
    assigned !== (row.assigned_staff_id === null ? "" : String(row.assigned_staff_id)) ||
    complaint !== row.is_complaint;
  const field = (label: string, control: ReactNode) => (
    <label className="flex flex-col gap-1 text-[0.8rem] font-semibold">
      {label}
      {control}
    </label>
  );
  return (
    <details className="group rounded-[20px] bg-[var(--surface)] ring-1 ring-[var(--line)]" open={typeof window !== "undefined" && window.innerWidth >= 1024}>
      <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-2 px-4 font-bold">
        {t("metadata")}
        <Icon name="down" size={18} className="text-[var(--hint)] transition-transform group-open:rotate-180" />
      </summary>
      <fieldset disabled={disabled} className="flex flex-col gap-3 border-t border-[var(--line)] p-4">
        {field(
          t("department"),
          <Select value={department} onChange={(e) => setDepartment(e.target.value)}>
            <option value="">{t("unassignedDepartment")}</option>
            {row.department_id && !departments?.some((d) => d.id === row.department_id) && <option value={row.department_id}>#{row.department_id}</option>}
            {departments?.map((d) => (
              <option key={d.id} value={d.id}>
                {d.title}
                {!d.is_active ? ` (${t("inactive")})` : ""}
              </option>
            ))}
          </Select>,
        )}
        {field(
          t("priority"),
          <Select value={priority} onChange={(e) => setPriority(e.target.value as Priority)}>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {t(p)}
              </option>
            ))}
          </Select>,
        )}
        <div className="flex flex-col gap-1">
          {field(
            t("staffId"),
            <input
              className="min-h-11 rounded-xl bg-[var(--bg)] px-3.5 text-[0.92rem] ring-1 ring-[var(--line)] outline-none focus:ring-2 focus:ring-firouzeh-500"
              dir="ltr"
              inputMode="numeric"
              value={assigned}
              onChange={(e) => setAssigned(e.target.value)}
            />,
          )}
          <div className="flex gap-2">
            <Button small onClick={() => setAssigned(String(staffId))}>
              {t("assignMe")}
            </Button>
            <Button small tone="ghost" onClick={() => setAssigned("")}>
              {t("none")}
            </Button>
          </div>
          <p className="text-[0.74rem] text-[var(--hint)]">{t("staffHint")}</p>
        </div>
        <Toggle checked={complaint} onChange={setComplaint} label={t("complaint")} hint={t("complaintHint")} />
        {error && <p role="alert" className="text-[0.84rem] font-semibold text-anar-700">{error}</p>}
        <Button tone="primary" disabled={!changed} onClick={save}>
          {t("save")}
        </Button>
      </fieldset>
    </details>
  );
}

/** The ticket's service: status, usage, end date and how fresh the numbers are (REF-09). */
function ServiceLine({ context, t, locale }: { context: ServiceContext; t: T; locale: string }) {
  const gb = (bytes: number) =>
    `${(bytes / 1024 ** 3).toLocaleString(locale === "fa" ? "fa-IR" : "en-US", { maximumFractionDigits: 2 })} GB`;
  const parts: string[] = [];
  if (context.used_bytes !== undefined)
    parts.push(`${t("serviceUsed")} ${gb(context.used_bytes)} ${t("serviceOf")} ${context.data_limit ? gb(context.data_limit) : t("serviceUnlimited")}`);
  if (context.expire_at) parts.push(`${t("serviceEnds")} ${stamp(context.expire_at, locale)}`);
  if (context.panel_down) parts.push(t("servicePanelDown"));
  else if (context.checked) parts.push(t(context.checked === "live" ? "serviceLive" : "serviceStored"));
  return (
    <div className={`mt-3 rounded-xl px-3 py-2.5 text-[0.8rem] ${context.panel_down ? "bg-anar-100 text-anar-700" : "bg-[var(--surface-2)]"}`}>
      <p className="font-bold">
        {t("serviceContext")} #{context.id} · {context.status}
      </p>
      {parts.length > 0 && <p className="mt-1 text-[var(--hint)]">{parts.join(" · ")}</p>}
    </div>
  );
}
