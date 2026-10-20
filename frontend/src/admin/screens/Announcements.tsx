import { useEffect, useRef, useState } from "react";
import { announcementCopy, type AnnouncementPage } from "@shared/announcements";
import { ApiError } from "@shared/http";
import { dateTime } from "@shared/format";
import { call } from "../api";
import { useSession } from "../session";
import { Button, Input, Page, Section, Select } from "../ui";

type Draft = {
  request_key: string;
  title: string;
  text: string;
  kind: "operational" | "marketing";
  starts_at: string | null;
  expires_at: string | null;
};
function saved(key: string): Draft | null {
  const raw = sessionStorage.getItem(key);
  if (!raw) return null;
  const d = JSON.parse(raw) as Draft;
  if (
    typeof d.request_key !== "string" ||
    typeof d.title !== "string" ||
    typeof d.text !== "string" ||
    !["operational", "marketing"].includes(d.kind) ||
    !(d.starts_at === null || typeof d.starts_at === "string") ||
    !(d.expires_at === null || typeof d.expires_at === "string")
  )
    throw Error("invalid saved publication");
  return d;
}
export function Announcements() {
  const { me } = useSession();
  return me ? (
    <Publisher
      key={`${me.shop_id}:${me.id}`}
      owner={`${me.shop_id}:${me.id}`}
    />
  ) : null;
}

export function Publisher({ owner }: { owner: string }) {
  const { locale } = useSession();
  const copy = announcementCopy(locale);
  const key = `pauru-announcement:${owner}`;
  const [pending, setPending] = useState<Draft | null>(null);
  const [preview, setPreview] = useState<Draft | null>(null);
  const [form, setForm] = useState({
    title: "",
    text: "",
    kind: "operational" as "operational" | "marketing",
    start: "",
    end: "",
  });
  const [page, setPage] = useState<AnnouncementPage | null>(null);
  const [before, setBefore] = useState<number | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [storageOK, setStorageOK] = useState(false);
  const gate = useRef(false),
    alive = useRef(true),
    generation = useRef(0);
  useEffect(() => {
    alive.current = true;
    try {
      setPending(saved(key));
      setStorageOK(true);
    } catch {
      setStorageOK(false);
      setError(true);
    }
    return () => {
      alive.current = false;
      ++generation.current;
    };
  }, [key]);
  async function load(cursor = before) {
    const mine = ++generation.current;
    try {
      const value = await call<AnnouncementPage>(
        `/api/admin/announcements${cursor ? `?before=${cursor}` : ""}`,
      );
      if (alive.current && mine === generation.current) {
        setPage(value);
        setError(false);
      }
    } catch {
      if (alive.current && mine === generation.current) setError(true);
    }
  }
  useEffect(() => {
    setPage(null);
    void load(before);
  }, [before]);
  function makePreview() {
    if (gate.current || pending) return;
    try {
      const starts_at = form.start ? new Date(form.start).toISOString() : null;
      const expires_at = form.end ? new Date(form.end).toISOString() : null;
      if (
        expires_at &&
        Date.parse(expires_at) <=
          (starts_at ? Date.parse(starts_at) : Date.now())
      )
        throw Error("expiry");
      setPreview({
        request_key: crypto.randomUUID(),
        title: form.title.trim(),
        text: form.text.trim(),
        kind: form.kind,
        starts_at,
        expires_at,
      });
      setError(false);
    } catch {
      setError(true);
    }
  }
  async function publish() {
    const body = pending ?? preview;
    if (!body || gate.current || !storageOK) return;
    gate.current = true;
    setBusy(true);
    setError(false);
    try {
      // Persist exact immutable payload BEFORE a request. Unknown results cannot start a new publication.
      sessionStorage.setItem(key, JSON.stringify(body));
      setPending(body);
      setPreview(null);
      await call("/api/admin/announcements", { method: "POST", body });
      sessionStorage.removeItem(key);
      if (!alive.current) return;
      setPending(null);
      setForm({ title: "", text: "", kind: "operational", start: "", end: "" });
      await load();
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.status === 422 || error.code === "announcement_window")
      ) {
        // These explicit pre-write rejections have no published result. Keep the draft editable.
        try {
          sessionStorage.removeItem(key);
          if (alive.current) setPending(null);
        } catch {
          /* retain exact payload */
        }
      }
      if (alive.current) setError(true);
    } finally {
      gate.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function revoke(id: number) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError(false);
    try {
      await call(`/api/admin/announcements/${id}/revoke`, { method: "POST" });
      if (alive.current) await load();
    } catch {
      if (alive.current) setError(true);
    } finally {
      gate.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const change = (field: keyof typeof form, value: string) =>
    setForm((old) => ({ ...old, [field]: value }));
  return (
    <Page title={copy.title}>
      <p>{copy.notice}</p>
      {error && <p role="alert">{pending ? copy.unknown : copy.failure}</p>}
      <Section>
        {pending ? (
          <>
            <p role="status">{copy.unknown}</p>
            <h2>{pending.title}</h2>
            <p style={{ whiteSpace: "pre-wrap" }}>{pending.text}</p>
            <Button
              busy={busy}
              disabled={!storageOK}
              onClick={() => void publish()}
            >
              {copy.retryPublish}
            </Button>
          </>
        ) : (
          <>
            <label>
              {copy.heading}
              <Input
                value={form.title}
                maxLength={128}
                disabled={busy || !!preview}
                onChange={(e) => change("title", e.target.value)}
              />
            </label>
            <label>
              {copy.body}
              <textarea
                className="w-full rounded-xl border p-3"
                value={form.text}
                maxLength={4096}
                disabled={busy || !!preview}
                onChange={(e) => change("text", e.target.value)}
              />
            </label>
            <label>
              {copy.operational} / {copy.marketing}
              <Select
                value={form.kind}
                disabled={busy || !!preview}
                onChange={(e) => change("kind", e.target.value)}
              >
                <option value="operational">{copy.operational}</option>
                <option value="marketing">{copy.marketing}</option>
              </Select>
            </label>
            <label>
              {copy.start}
              <Input
                type="datetime-local"
                value={form.start}
                disabled={busy || !!preview}
                onChange={(e) => change("start", e.target.value)}
              />
            </label>
            <label>
              {copy.end}
              <Input
                type="datetime-local"
                value={form.end}
                disabled={busy || !!preview}
                onChange={(e) => change("end", e.target.value)}
              />
            </label>
            {preview ? (
              <>
                <h2>{preview.title}</h2>
                <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {preview.text}
                </p>
                <p>
                  {copy[preview.kind]} · {preview.starts_at ?? "now"} ·{" "}
                  {preview.expires_at ?? "—"}
                </p>
                <Button busy={busy} onClick={() => void publish()}>
                  {copy.confirm}
                </Button>
                <Button disabled={busy} onClick={() => setPreview(null)}>
                  {copy.cancel}
                </Button>
              </>
            ) : (
              <Button
                disabled={!storageOK || !form.title.trim() || !form.text.trim()}
                onClick={makePreview}
              >
                {copy.publish}
              </Button>
            )}
          </>
        )}
      </Section>
      <Section>
        <Button disabled={busy} onClick={() => void load()}>
          {error ? copy.retry : copy.refresh}
        </Button>
        {!page && !error && <p role="status">{copy.loading}</p>}
        {page?.items.length === 0 && <p>{copy.empty}</p>}
        {page?.items.map((row) => (
          <article key={row.id}>
            <h2>{row.title}</h2>
            <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {row.text}
            </p>
            <p>
              {copy[row.kind]} · {dateTime(row.starts_at, locale)} ·{" "}
              {row.expires_at ? dateTime(row.expires_at, locale) : "—"}
            </p>
            {row.revoked_at ? (
              <p>{copy.revoked}</p>
            ) : (
              <Button busy={busy} onClick={() => void revoke(row.id)}>
                {copy.revoke}
              </Button>
            )}
          </article>
        ))}
        {before != null && (
          <Button disabled={busy} onClick={() => setBefore(undefined)}>
            {copy.first}
          </Button>
        )}
        {page?.next_before != null && (
          <Button
            disabled={busy}
            onClick={() => setBefore(page.next_before ?? undefined)}
          >
            {copy.next}
          </Button>
        )}
      </Section>
    </Page>
  );
}
