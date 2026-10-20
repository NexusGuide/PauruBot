import { useEffect, useRef, useState } from "react";

/** Authenticated fetch only; never put Telegram init data in a URL or local storage. */
export function TicketAttachment({ path, headers, label, failure, loading }: {
  path: string; headers?: Record<string, string>; label: string; failure: string; loading: string;
}) {
  const [media, setMedia] = useState<{ url: string; image: boolean; name: string } | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(false);
  const controller = useRef<AbortController | null>(null), objectUrl = useRef<string | null>(null);
  useEffect(() => () => { controller.current?.abort(); if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); }, [path]);
  const load = async () => {
    if (controller.current || media) return;
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError(false);
    try {
      const response = await fetch(path, { headers, credentials: "same-origin", signal: abort.signal, cache: "no-store" });
      if (!response.ok) throw new Error("attachment_unavailable");
      const blob = await response.blob();
      if (blob.size > 4 * 1024 * 1024 || !["image/png", "image/jpeg", "text/plain", "text/plain; charset=utf-8"].includes(blob.type)) throw new Error("attachment_invalid");
      if (abort.signal.aborted) return;
      const url = URL.createObjectURL(blob); objectUrl.current = url;
      setMedia({ url, image: blob.type.startsWith("image/"), name: blob.type === "image/png" ? "attachment.png" : blob.type === "image/jpeg" ? "attachment.jpg" : "attachment.txt" });
    } catch { if (!abort.signal.aborted) setError(true); }
    finally { if (!abort.signal.aborted) { controller.current = null; setBusy(false); } }
  };
  return <div className="my-2">
    {!media && <button type="button" disabled={busy} onClick={() => void load()} className="min-h-11 underline">{busy ? loading : label}</button>}
    {error && <p role="alert">{failure}</p>}
    {media && <><a href={media.url} download={media.name} className="block min-h-11 underline">{label}</a>{media.image && <img src={media.url} alt={label} style={{ maxWidth: "100%", maxHeight: 280, objectFit: "contain" }} />}</>}
  </div>;
}
