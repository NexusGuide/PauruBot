import { useEffect, useRef, useState } from "react";
import {
  announcementCopy,
  type Announcement,
  type AnnouncementPage,
} from "@shared/announcements";
import { dateTime } from "@shared/format";
import { api } from "../api";
import { useApp } from "../state";
import { Button } from "../ui";

/** Parent keys this subtree by authenticated owner; no cross-account local read cache. */
export function AnnouncementInbox() {
  const { locale } = useApp();
  const copy = announcementCopy(locale);
  const [page, setPage] = useState<AnnouncementPage | null>(null);
  const [unread, setUnread] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [opened, setOpened] = useState<number | null>(null);
  const generation = useRef(0);
  const gate = useRef(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      ++generation.current;
      gate.current = false;
    };
  }, []);
  async function load(before?: number) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError(false);
    const mine = ++generation.current;
    try {
      const result = await api.announcements(before, unread);
      if (!alive.current || mine !== generation.current) return;
      setPage((previous) => ({
        ...result,
        items:
          before && previous
            ? [
                ...previous.items,
                ...result.items.filter(
                  (row) => !previous.items.some((old) => old.id === row.id),
                ),
              ]
            : result.items,
      }));
    } catch {
      if (alive.current && mine === generation.current) setError(true);
    } finally {
      if (mine === generation.current) {
        gate.current = false;
        if (alive.current) setBusy(false);
      }
    }
  }
  useEffect(() => {
    setPage(null);
    setOpened(null);
    void load();
  }, [unread]);

  async function mark(row: Announcement, action: "seen" | "read") {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError(false);
    const mine = ++generation.current;
    try {
      const result = await api.announcementState(row.id, action);
      if (!alive.current || mine !== generation.current) return;
      setPage(
        (previous) =>
          previous && {
            ...previous,
            items: previous.items.flatMap((old) =>
              old.id !== row.id
                ? [old]
                : unread && result.item.read_at
                  ? []
                  : [result.item],
            ),
          },
      );
      if (action === "seen") setOpened(row.id);
    } catch {
      if (alive.current && mine === generation.current) setError(true);
    } finally {
      if (mine === generation.current) {
        gate.current = false;
        if (alive.current) setBusy(false);
      }
    }
  }
  return (
    <section aria-label={copy.title} className="app-card">
      <h2>{copy.title}</h2>
      <div className="app-chips">
        <Button
          disabled={busy}
          aria-pressed={!unread}
          onClick={() => {
            if (!gate.current) setUnread(false);
          }}
        >
          {copy.all}
        </Button>
        <Button
          disabled={busy}
          aria-pressed={unread}
          onClick={() => {
            if (!gate.current) setUnread(true);
          }}
        >
          {copy.unread}
        </Button>
        <Button disabled={busy} onClick={() => void load()}>
          {copy.refresh}
        </Button>
      </div>
      {busy && <p role="status">{copy.loading}</p>}
      {error && <p role="alert">{copy.failure}</p>}
      {!page && !busy && error && (
        <Button onClick={() => void load()}>{copy.retry}</Button>
      )}
      {page?.items.length === 0 && <p>{copy.empty}</p>}
      {page?.items.map((row) => (
        <article key={row.id}>
          <h3>{row.title}</h3>
          <p className="app-hint">
            {copy[row.kind]} · {dateTime(row.starts_at, locale)} ·{" "}
            {row.read_at
              ? copy.readDone
              : row.seen_at
                ? copy.seen
                : copy.unseen}
          </p>
          <Button disabled={busy} onClick={() => void mark(row, "seen")}>
            {copy.open}
          </Button>
          {opened === row.id && (
            <>
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {row.text}
              </p>
              {!row.read_at && (
                <Button disabled={busy} onClick={() => void mark(row, "read")}>
                  {copy.read}
                </Button>
              )}
            </>
          )}
        </article>
      ))}
      {page?.next_before != null && (
        <Button
          disabled={busy}
          onClick={() => void load(page.next_before ?? undefined)}
        >
          {copy.more}
        </Button>
      )}
    </section>
  );
}
