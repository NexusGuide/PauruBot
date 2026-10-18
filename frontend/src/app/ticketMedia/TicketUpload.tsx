import { useEffect, useRef, useState } from "react";
import { useApp } from "../state";
import { initData } from "../telegram";
import { validFile } from "./model";

export function TicketUpload({ id, disabled, onSaved, onBusy }: { id: number; disabled: boolean; onSaved: () => void; onBusy: (busy: boolean) => void }) {
  const { t } = useApp();
  const [draft, setDraft] = useState<{ file: File; key: string } | null>(null);
  const [progress, setProgress] = useState<number | null>(null), [error, setError] = useState("");
  const xhr = useRef<XMLHttpRequest | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; const current = xhr.current; xhr.current = null; current?.abort(); }; }, []);
  const send = () => {
    if (!draft || disabled || xhr.current) return;
    const current = new XMLHttpRequest(); xhr.current = current;
    setError(""); setProgress(0); onBusy(true);
    current.open("POST", `/api/webapp/tickets/${id}/attachments`);
    current.timeout = 60000;
    current.setRequestHeader("X-Telegram-Init-Data", initData());
    current.setRequestHeader("X-Ticket-Request-Key", draft.key);
    current.setRequestHeader("Content-Type", draft.file.type);
    current.upload.onprogress = e => { if (e.lengthComputable) setProgress(Math.round(e.loaded / e.total * 100)); };
    const finish = (ok: boolean) => {
      if (!live.current || xhr.current !== current) return;
      xhr.current = null; setProgress(null); onBusy(false);
      if (ok) { setDraft(null); onSaved(); }
      else setError(t("tickets_file_uncertain"));
    };
    current.onload = () => {
      let confirmed = false;
      try { const body = JSON.parse(current.responseText); confirmed = current.status === 200 && body.ticket?.id === id && Number.isSafeInteger(body.message_id); } catch { /* unknown response: keep replay key */ }
      finish(confirmed);
    };
    current.onerror = current.ontimeout = current.onabort = () => finish(false);
    current.send(draft.file);
  };
  return <section className="app-card p-4" aria-label={t("tickets_file")}>
    <p className="app-hint text-sm">{t("tickets_file_hint")}</p>
    <input aria-label={t("tickets_file")} type="file" accept="image/png,image/jpeg,text/plain,.txt,.log" disabled={disabled || progress !== null || !!draft} onChange={e => {
      const file = e.target.files?.[0]; e.target.value = "";
      if (!file) return;
      if (!validFile(file)) { setError(t("tickets_file_invalid")); return; }
      setDraft({ file, key: crypto.randomUUID() }); setError("");
    }} />
    {draft && <p dir="auto" className="break-words">{draft.file.name}</p>}
    {progress !== null && <><progress max={100} value={progress} aria-label={t("tickets_sending")} /><p role="status">{progress}% — {t("tickets_file_wait")}</p><button type="button" onClick={() => xhr.current?.abort()}>{t("tickets_cancel")}</button></>}
    {draft && progress === null && <><button type="button" className="min-h-11 underline" disabled={disabled} onClick={send}>{t("tickets_file_send")}</button><button type="button" className="min-h-11 mx-3" disabled={disabled} onClick={() => { setDraft(null); setError(""); }}>{t("tickets_file_discard")}</button></>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
