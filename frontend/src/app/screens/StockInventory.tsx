import { useEffect, useRef, useState } from "react";
import { request } from "@shared/http";
import { errorText, useApp } from "../state";
import { initData } from "../telegram";
import { Button, Panel } from "../ui";

interface Unit {
  id: number;
  name: string;
  parts: number;
}
interface Part {
  text: string;
  filename?: string;
  file: boolean;
  as_file?: boolean;
  subscription_url?: string;
}
const auth = () => ({ "X-Telegram-Init-Data": initData() });

/** Manual deliveries remain accessible even when there is no remote-panel Service row. */
export function StockInventory() {
  const { locale, t } = useApp();
  const tr = (a: string, b: string) => (locale === "fa" ? a : b);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<Unit[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [content, setContent] = useState<{ id: number; parts: Part[] } | null>(
    null,
  );
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const active = useRef(true);
  const action = useRef(false);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setBusy(true);
    setContent(null);
    void request<{ items: Unit[]; next_offset: number | null }>(
      `/api/webapp/stock?offset=${offset}`,
      { headers: auth(), signal: controller.signal },
    )
      .then((data) => {
        if (!controller.signal.aborted) {
          setItems(data.items);
          setNext(data.next_offset);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(err);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [offset, attempt]);
  const show = async (id: number) => {
    if (action.current || busy) return;
    action.current = true;
    setBusy(true);
    setError(null);
    setContent(null);
    try {
      const result = await request<{ id: number; parts: Part[] }>(
        `/api/webapp/stock/${id}`,
        { headers: auth() },
      );
      if (active.current) setContent(result);
    } catch (err) {
      if (active.current) setError(err);
    } finally {
      action.current = false;
      if (active.current) setBusy(false);
    }
  };
  const download = async (id: number, index: number, name: string) => {
    if (action.current || busy) return;
    action.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/webapp/stock/${id}/files/${index}`, {
        headers: auth(),
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`http_${res.status}`);
      const blob = await res.blob();
      if (!active.current) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      if (active.current) setError(err);
    } finally {
      action.current = false;
      if (active.current) setBusy(false);
    }
  };
  return (
    <Panel className="mb-5">
      <h2 className="mb-2 text-lg font-bold">
        {tr("کانفیگ‌های آمادهٔ من", "My ready-made configurations")}
      </h2>
      <p className="mb-4 text-sm text-[var(--hint)]">
        {tr(
          "خریدهای فروش دستی و جایگزین نت ملی؛ متن و فایل‌ها همیشه از اینجا در دسترس‌اند.",
          "Manual purchases and national-network replacements. Retrieve their text and files here.",
        )}
      </p>
      {busy && <p role="status">{tr("در حال دریافت…", "Loading…")}</p>}
      {Boolean(error) && (
        <div role="alert">
          <p>{errorText(t, error)}</p>
          <Button
            tone="quiet"
            disabled={busy}
            onClick={() => setAttempt((x) => x + 1)}
          >
            {tr("تلاش دوباره", "Retry")}
          </Button>
        </div>
      )}
      {!items.length && !busy && !error && (
        <p className="text-sm text-[var(--hint)]">
          {tr(
            "هنوز کانفیگ آماده‌ای نخریده‌اید.",
            "No ready-made purchases yet.",
          )}
        </p>
      )}
      <ul className="divide-y divide-[var(--line)]">
        {items.map((i) => (
          <li
            key={i.id}
            className="flex items-center justify-between gap-3 py-3"
          >
            <span className="min-w-0 break-words font-semibold">
              {i.name}
              <small className="block text-sm font-normal text-[var(--hint)]">
                {i.parts} {tr("محتوا", "parts")}
              </small>
            </span>
            <Button
              tone="quiet"
              style={{ width: "auto", flexShrink: 0 }}
              disabled={busy}
              onClick={() => void show(i.id)}
            >
              {tr("دریافت", "Retrieve")}
            </Button>
          </li>
        ))}
      </ul>
      {(offset > 0 || next !== null) && (
        <div className="mt-3 flex gap-3">
          <Button
            tone="quiet"
            disabled={busy || offset === 0}
            onClick={() => setOffset((x) => Math.max(0, x - 25))}
          >
            {tr("قبلی", "Previous")}
          </Button>
          <Button
            tone="quiet"
            disabled={busy || next === null}
            onClick={() => {
              if (next !== null) setOffset(next);
            }}
          >
            {tr("بعدی", "Next")}
          </Button>
        </div>
      )}
      {content && (
        <section className="mt-5 border-t border-[var(--line)] pt-4">
          <h3 className="mb-3 font-bold">#{content.id}</h3>
          {content.parts.map((p, index) => (
            <div key={index} className="mb-4 rounded-xl bg-[var(--bg)] p-3">
              {p.file || p.as_file || p.text.length > 3500 ? (
                <Button
                  disabled={busy}
                  onClick={() =>
                    void download(
                      content.id,
                      index,
                      p.filename || `config-${content.id}-${index + 1}.txt`,
                    )
                  }
                >
                  {p.filename || tr("دریافت فایل", "Download file")}
                </Button>
              ) : (
                <pre
                  dir="ltr"
                  className="max-h-72 overflow-auto whitespace-pre-wrap break-all text-sm"
                >
                  {p.text}
                </pre>
              )}
              {p.subscription_url && (
                <pre
                  dir="ltr"
                  className="mt-3 whitespace-pre-wrap break-all text-sm"
                >
                  {p.subscription_url}
                </pre>
              )}
            </div>
          ))}
        </section>
      )}
    </Panel>
  );
}
