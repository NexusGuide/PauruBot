import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { money } from "@shared/format";
import {
  call,
  type ResellerData,
  type ResellerGroup,
  type ResellerProfile,
  type StaffRow,
  type Ticket,
  type TicketMessage,
} from "../api";
import { PAGE_SIZE, useAction, useList, useLoad } from "../hooks";
import { useSession } from "../session";
import {
  Async,
  Badge,
  Button,
  Checkbox,
  Empty,
  Field,
  Input,
  NumberInput,
  Page,
  Pager,
  Section,
  Select,
  Sheet,
  Status,
  Table,
  Textarea,
  Toggle,
  Toolbar,
  When,
  type Column,
} from "../ui";

// ---------------------------------------------------------------- resellers

type GroupDraft = Omit<ResellerGroup, "id"> & { id?: number };

function GroupSheet({ draft, onClose, onSaved }: { draft: GroupDraft; onClose: () => void; onSaved: () => void }) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState(draft);
  const [busy, run] = useAction();
  const save = async () => {
    const { id, ...body } = form;
    try {
      await run(() => call(id ? `/api/admin/reseller-groups/${id}` : "/api/admin/reseller-groups", { method: id ? "PUT" : "POST", body }));
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
      title={form.id ? t("edit_group") : t("new_group")}
      footer={
        <Button tone="primary" busy={busy} disabled={!form.title.trim()} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("title")}>
          {(id) => <Input id={id} value={form.title} maxLength={64} onChange={(e) => setForm({ ...form, title: e.target.value })} />}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("discount_percent")}>
            {(id) => (
              <NumberInput id={id} value={form.discount_percent} onChange={(v) => setForm({ ...form, discount_percent: Math.min(100, v ?? 0) })} />
            )}
          </Field>
          <Field label={t("min_topup")}>
            {(id) => <NumberInput id={id} value={form.min_topup} onChange={(v) => setForm({ ...form, min_topup: v ?? 0 })} />}
          </Field>
        </div>
        <Field label={t("max_active_services")} hint={t("empty_no_limit")}>
          {(id) => (
            <NumberInput id={id} value={form.max_active_services} onChange={(v) => setForm({ ...form, max_active_services: v || null })} allowEmpty />
          )}
        </Field>
        <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} />
      </div>
    </Sheet>
  );
}

function ProfileSheet({
  profile,
  data,
  onClose,
  onSaved,
}: {
  profile: ResellerProfile;
  data: ResellerData;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toast, errorText } = useSession();
  const [form, setForm] = useState(profile);
  const [busy, run] = useAction();
  const save = async () => {
    try {
      await run(() =>
        call("/api/admin/resellers", {
          method: "PUT",
          body: {
            customer_id: form.customer_id,
            group_id: form.group_id,
            permissions: form.permissions,
            is_active: form.is_active,
            expires_at: form.expires_at,
            max_purchases_per_day: form.max_purchases_per_day,
          },
        }),
      );
      toast(t("saved"));
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const toggle = (perm: string, on: boolean) =>
    setForm({ ...form, permissions: on ? [...form.permissions, perm] : form.permissions.filter((p) => p !== perm) });
  return (
    <Sheet
      open
      onClose={onClose}
      title={form.customer}
      footer={
        <Button tone="primary" busy={busy} onClick={() => void save()}>
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label={t("reseller_group")}>
          {(id) => (
            <Select id={id} value={form.group_id ?? ""} onChange={(e) => setForm({ ...form, group_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">—</option>
              {data.groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.title}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("max_purchases_per_day")} hint={t("empty_no_limit")}>
          {(id) => (
            <NumberInput
              id={id}
              value={form.max_purchases_per_day}
              onChange={(v) => setForm({ ...form, max_purchases_per_day: v || null })}
              allowEmpty
            />
          )}
        </Field>
        <Field label={t("reseller_expires")} hint={t("reseller_expires_hint")}>
          {(id) => (
            <Input
              id={id}
              type="date"
              value={form.expires_at ? form.expires_at.slice(0, 10) : ""}
              onChange={(e) => setForm({ ...form, expires_at: e.target.value ? new Date(`${e.target.value}T23:59:59`).toISOString() : null })}
            />
          )}
        </Field>
        <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} />
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-[0.82rem] font-semibold">{t("reseller_permissions")}</legend>
          <p className="text-[0.78rem] text-[var(--hint)]">{t("reseller_permissions_hint")}</p>
          {data.permissions.map((perm) => (
            <label key={perm} className="flex items-center gap-2 text-[0.88rem]">
              <Checkbox label={perm} checked={form.permissions.includes(perm)} onChange={(on) => toggle(perm, on)} />
              {t(`perm_${perm.replace(".", "_")}`)}
            </label>
          ))}
        </fieldset>
      </div>
    </Sheet>
  );
}

export function Resellers() {
  const { t, locale, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<ResellerData>("/api/admin/resellers");
  const [group, setGroup] = useState<GroupDraft | null>(null);
  const [profile, setProfile] = useState<ResellerProfile | null>(null);
  const [decision, setDecision] = useState<{ id: number; group_id: number | null } | null>(null);
  const [busy, run] = useAction();

  const decide = async (id: number, approve: boolean, groupId: number | null) => {
    try {
      await run(() => call(`/api/admin/reseller-applications/${id}`, { method: "POST", body: { approve, group_id: groupId } }));
      toast(approve ? t("reseller_approved") : t("reseller_rejected"));
      setDecision(null);
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Page
          title={t("nav_resellers")}
          actions={
            <Button
              tone="primary"
              icon="plus"
              onClick={() => setGroup({ title: "", discount_percent: 10, min_topup: 0, max_active_services: null, is_active: true })}
            >
              {t("new_group")}
            </Button>
          }
        >
          {value.applications.length > 0 && (
            <Section title={t("reseller_applications")} flush>
              <ul className="divide-y divide-[var(--line)]">
                {value.applications.map((app) => (
                  <li key={app.id} className="flex flex-col gap-2 border-s-4 border-s-saffron-500 px-4 py-3 sm:flex-row sm:items-center">
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <Link className="font-semibold text-firouzeh-700" to={`/customers/${app.customer_id}`}>
                        {app.customer}
                      </Link>
                      {app.text && <p className="text-[0.85rem] leading-relaxed" dir="auto">{app.text}</p>}
                      <span className="text-[0.75rem] text-[var(--hint)]">
                        <When iso={app.created_at} time />
                      </span>
                    </div>
                    <div className="flex gap-2">
                      <Button small tone="danger" busy={busy} onClick={() => void decide(app.id, false, null)}>
                        {t("reject")}
                      </Button>
                      <Button small tone="approve" onClick={() => setDecision({ id: app.id, group_id: value.groups[0]?.id ?? null })}>
                        {t("approve")}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title={t("reseller_groups")} flush>
            {value.groups.length === 0 ? (
              <Empty title={t("no_groups")} hint={t("no_groups_hint")} />
            ) : (
              <ul className="divide-y divide-[var(--line)]">
                {value.groups.map((g) => (
                  <li key={g.id}>
                    <button
                      type="button"
                      onClick={() => setGroup({ ...g })}
                      className="flex w-full items-center justify-between gap-3 px-4 py-3 text-start hover:bg-[var(--bg)]"
                    >
                      <span className="flex items-center gap-2 font-semibold">
                        {g.title}
                        {!g.is_active && <Badge>{t("inactive")}</Badge>}
                      </span>
                      <span className="text-[0.85rem] text-[var(--hint)]">
                        {t("group_summary", { percent: money(g.discount_percent, locale), min: money(g.min_topup, locale) })}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title={t("resellers")} flush>
            {value.profiles.length === 0 ? (
              <Empty title={t("no_resellers")} hint={t("no_resellers_hint")} />
            ) : (
              <ul className="divide-y divide-[var(--line)]">
                {value.profiles.map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => setProfile(p)}
                      className="flex w-full items-center justify-between gap-3 px-4 py-3 text-start hover:bg-[var(--bg)]"
                    >
                      <span className="flex items-center gap-2 font-semibold">
                        {p.customer}
                        {!p.is_active && <Badge tone="loss">{t("inactive")}</Badge>}
                      </span>
                      <span className="text-[0.85rem] text-[var(--hint)]">
                        {value.groups.find((g) => g.id === p.group_id)?.title ?? "—"}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          {group && (
            <GroupSheet
              draft={group}
              onClose={() => setGroup(null)}
              onSaved={() => {
                setGroup(null);
                void reload();
              }}
            />
          )}
          {profile && (
            <ProfileSheet
              profile={profile}
              data={value}
              onClose={() => setProfile(null)}
              onSaved={() => {
                setProfile(null);
                void reload();
              }}
            />
          )}
          <Sheet
            open={decision !== null}
            onClose={() => setDecision(null)}
            title={t("approve_reseller")}
            footer={
              <Button
                tone="approve"
                busy={busy}
                disabled={!decision?.group_id}
                onClick={() => decision && void decide(decision.id, true, decision.group_id)}
              >
                {t("approve")}
              </Button>
            }
          >
            {value.groups.length === 0 ? (
              <p className="text-[var(--hint)]">{t("no_groups_hint")}</p>
            ) : (
              <Field label={t("reseller_group")}>
                {(id) => (
                  <Select
                    id={id}
                    value={decision?.group_id ?? ""}
                    onChange={(e) => decision && setDecision({ ...decision, group_id: Number(e.target.value) })}
                  >
                    {value.groups.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.title}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            )}
          </Sheet>
        </Page>
      )}
    </Async>
  );
}

// ---------------------------------------------------------------- staff

interface StaffData {
  items: StaffRow[];
  roles: string[];
  matrix: Record<string, string[]>;
}

function StaffSheet({
  row,
  roles,
  onClose,
  onSaved,
}: {
  row: StaffRow | null;
  roles: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, me, toast, errorText } = useSession();
  const [form, setForm] = useState({
    role: row?.role ?? "support",
    username: row?.username ?? "",
    password: "",
    telegram_id: row?.telegram_id ? String(row.telegram_id) : "",
    display_name: row?.display_name ?? "",
    is_active: row?.is_active ?? true,
    reseller_profile_id: row?.reseller_profile_id ?? null,
  });
  const [busy, run] = useAction();
  const isSelf = row?.id === me?.id;
  const isOwner = row?.role === "owner";

  const save = async () => {
    const telegram = form.telegram_id.trim() ? Number(form.telegram_id.trim()) : null;
    try {
      if (row) {
        const body: Record<string, unknown> = { display_name: form.display_name || null };
        if (!isSelf && !isOwner) {
          body.role = form.role;
          body.is_active = form.is_active;
        }
        if (form.password) body.password = form.password;
        if (telegram) body.telegram_id = telegram;
        await run(() => call(`/api/admin/staff/${row.id}`, { method: "PATCH", body }));
      } else {
        await run(() =>
          call("/api/admin/staff", {
            method: "POST",
            body: {
              role: form.role,
              username: form.username.trim(),
              password: form.password || null,
              telegram_id: telegram,
              display_name: form.display_name || null,
              reseller_profile_id: form.reseller_profile_id,
            },
          }),
        );
      }
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
      title={row ? row.username : t("new_staff")}
      footer={
        <Button
          tone="primary"
          busy={busy}
          disabled={!row && (!/^[A-Za-z0-9_.-]{3,64}$/.test(form.username) || (!form.password && !form.telegram_id))}
          onClick={() => void save()}
        >
          {t("save")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        {!row && (
          <Field label={t("username")}>
            {(id) => <Input id={id} dir="ltr" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />}
          </Field>
        )}
        <Field label={t("display_name")}>
          {(id) => <Input id={id} value={form.display_name} maxLength={96} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />}
        </Field>
        <Field label={t("role")} hint={isOwner ? t("owner_fixed") : isSelf ? t("own_role_fixed") : t(`role_${form.role}_hint`)}>
          {(id) => (
            <Select id={id} value={form.role} disabled={isSelf || isOwner} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              {roles
                .filter((role) => role !== "owner" || isOwner)
                .map((role) => (
                  <option key={role} value={role}>
                    {t(`role_${role}`)}
                  </option>
                ))}
            </Select>
          )}
        </Field>
        {form.role === "reseller" && !row && (
          <Field label={t("reseller_profile_id")} hint={t("reseller_profile_id_hint")}>
            {(id) => (
              <NumberInput
                id={id}
                value={form.reseller_profile_id}
                onChange={(v) => setForm({ ...form, reseller_profile_id: v })}
                allowEmpty
              />
            )}
          </Field>
        )}
        <Field label={row ? t("new_password") : t("password")} hint={t("password_hint")}>
          {(id) => (
            <Input
              id={id}
              type="password"
              dir="ltr"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          )}
        </Field>
        <Field label={t("telegram_id")} hint={t("staff_telegram_hint")}>
          {(id) => (
            <Input id={id} dir="ltr" inputMode="numeric" value={form.telegram_id} onChange={(e) => setForm({ ...form, telegram_id: e.target.value })} />
          )}
        </Field>
        {row && !isSelf && !isOwner && (
          <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label={t("active")} hint={t("deactivate_hint")} />
        )}
      </div>
    </Sheet>
  );
}

export function Staff() {
  const { t } = useSession();
  const { data, error, reload } = useLoad<StaffData>("/api/admin/staff");
  const [editing, setEditing] = useState<StaffRow | "new" | null>(null);
  const columns: Column<StaffRow>[] = [
    {
      key: "name",
      label: t("username"),
      mobile: "title",
      render: (s) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-semibold" dir="ltr">
            {s.username}
          </span>
          {s.display_name && <span className="text-[var(--hint)]">{s.display_name}</span>}
          {!s.is_active && <Badge tone="loss">{t("inactive")}</Badge>}
        </span>
      ),
    },
    { key: "role", label: t("role"), mobile: "end", render: (s) => <Badge tone="brand">{t(`role_${s.role}`)}</Badge> },
    { key: "login", label: t("last_login"), mobile: "meta", render: (s) => <When iso={s.last_login_at} time /> },
  ];
  return (
    <Page
      title={t("nav_staff")}
      actions={
        <Button tone="primary" icon="plus" onClick={() => setEditing("new")}>
          {t("new_staff")}
        </Button>
      }
    >
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <Section flush>
              <Table columns={columns} rows={value.items} onRow={setEditing} />
            </Section>
            <Section title={t("role_matrix")}>
              <div className="overflow-x-auto">
                <table className="w-full text-[0.8rem]">
                  <thead>
                    <tr className="text-[var(--hint)]">
                      <th className="py-1.5 text-start font-semibold">{t("permission")}</th>
                      {value.roles.map((role) => (
                        <th key={role} className="px-2 py-1.5 text-center font-semibold">
                          {t(`role_${role}`)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(value.matrix.owner ?? []).map((perm) => (
                      <tr key={perm} className="border-t border-[var(--line)]">
                        <td className="py-1.5">{t(`perm_${perm.replace(".", "_")}`)}</td>
                        {value.roles.map((role) => (
                          <td key={role} className="px-2 py-1.5 text-center">
                            {value.matrix[role]?.includes(perm) ? (
                              <span className="text-pista-700" aria-label={t("yes")}>
                                ✓
                              </span>
                            ) : (
                              <span className="text-[var(--hint)]" aria-label={t("no")}>
                                –
                              </span>
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
            {editing && (
              <StaffSheet
                row={editing === "new" ? null : editing}
                roles={value.roles}
                onClose={() => setEditing(null)}
                onSaved={() => {
                  setEditing(null);
                  void reload();
                }}
              />
            )}
          </>
        )}
      </Async>
    </Page>
  );
}

// ---------------------------------------------------------------- tickets

export function Tickets() {
  const { t } = useSession();
  const navigate = useNavigate();
  const list = useList<Ticket>("/api/admin/tickets", ["status"]);
  const columns: Column<Ticket>[] = [
    { key: "subject", label: t("ticket_subject"), mobile: "title", render: (x) => <span className="font-semibold" dir="auto">{x.subject}</span> },
    { key: "customer", label: t("col_customer"), mobile: "meta", render: (x) => x.customer ?? "—" },
    { key: "status", label: t("col_status"), mobile: "end", render: (x) => <Status value={x.status} /> },
    { key: "updated", label: t("updated_at"), mobile: "meta", render: (x) => <When iso={x.updated_at} time /> },
  ];
  return (
    <Page title={t("nav_tickets")}>
      <Section flush>
        <Toolbar>
          <Select className="w-auto" aria-label={t("filter_status")} value={list.get("status")} onChange={(e) => list.set({ status: e.target.value })}>
            <option value="">{t("filter_all_statuses")}</option>
            {["open", "answered", "closed"].map((s) => (
              <option key={s} value={s}>
                {t(`status_${s}`)}
              </option>
            ))}
          </Select>
        </Toolbar>
        <Async data={list.data} error={list.error} reload={list.reload}>
          {(data) => (
            <>
              <Table columns={columns} rows={data.items} onRow={(x) => navigate(`/tickets/${x.id}`)} />
              <Pager page={list.page} total={data.total} size={PAGE_SIZE} onPage={list.setPage} />
            </>
          )}
        </Async>
      </Section>
    </Page>
  );
}

export function TicketPage() {
  const { id } = useParams();
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ ticket: Ticket; customer: string | null; messages: TicketMessage[] }>(
    `/api/admin/tickets/${id}`,
  );
  const [reply, setReply] = useState("");
  const [busy, run] = useAction();

  const send = async () => {
    try {
      await run(() => call(`/api/admin/tickets/${id}/reply`, { method: "POST", body: { text: reply.trim() } }));
      setReply("");
      toast(t("reply_sent"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const close = async () => {
    try {
      await run(() => call(`/api/admin/tickets/${id}/close`, { method: "POST" }));
      toast(t("ticket_closed"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Async data={data} error={error} reload={reload}>
      {(value) => (
        <Page
          title={value.ticket.subject}
          actions={
            can("tickets.reply") && value.ticket.status !== "closed" ? (
              <Button busy={busy} onClick={() => void close()}>
                {t("close_ticket")}
              </Button>
            ) : null
          }
        >
          <p className="-mt-3 flex flex-wrap items-center gap-2 text-[0.85rem] text-[var(--hint)]">
            <Status value={value.ticket.status} />
            <Link className="font-semibold text-firouzeh-700" to={`/customers/${value.ticket.customer_id}`}>
              {value.customer}
            </Link>
          </p>
          <ol className="flex flex-col gap-3">
            {value.messages.map((m) => {
              const mine = m.author_type === "staff";
              return (
                <li key={m.id} className={`flex ${mine ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[85%] rounded-2xl px-4 py-2.5 ${
                      mine ? "bg-firouzeh-600 text-white" : "bg-[var(--surface)] ring-1 ring-[var(--line)]"
                    }`}
                  >
                    {m.text && <p className="whitespace-pre-wrap leading-relaxed" dir="auto">{m.text}</p>}
                    {m.has_attachment && <p className="text-[0.8rem] opacity-80">{t("attachment_in_telegram", { kind: m.attachment_kind ?? "" })}</p>}
                    <p className={`mt-1 text-[0.72rem] ${mine ? "text-white/75" : "text-[var(--hint)]"}`}>
                      <When iso={m.created_at} time />
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
          {can("tickets.reply") && value.ticket.status !== "closed" && (
            <Section>
              <div className="flex flex-col gap-3">
                <Textarea
                  aria-label={t("reply")}
                  placeholder={t("reply_placeholder")}
                  value={reply}
                  maxLength={4000}
                  onChange={(e) => setReply(e.target.value)}
                />
                <Button tone="primary" className="self-end" busy={busy} disabled={!reply.trim()} onClick={() => void send()}>
                  {t("send_reply")}
                </Button>
              </div>
            </Section>
          )}
        </Page>
      )}
    </Async>
  );
}
