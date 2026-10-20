import { useEffect, useRef, useState, type FormEvent } from "react";
import { call } from "../api";
import { useSession } from "../session";
import { Button, Field, Input } from "../ui";
import { text } from "./copy";

export function PasswordRecovery({ initialUsername = "" }: { initialUsername?: string }) {
  const { locale, refresh } = useSession();
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState(initialUsername);
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<"sent" | "done" | null>(null);
  const [error, setError] = useState<"failed" | "mismatch" | null>(null);
  const gate = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const t = (key: Parameters<typeof text>[1]) => text(locale, key);
  const clear = () => { setToken(""); setPassword(""); setRepeat(""); };
  const submit = async (event: FormEvent, confirm: boolean) => {
    event.preventDefault();
    if (gate.current) return;
    if (confirm && (password !== repeat || password.length < 10 || password.length > 256)) {
      setError("mismatch"); return;
    }
    gate.current = true; setBusy(true); setError(null); setNotice(null);
    try {
      const response = await call<{ ok: boolean }>(`/api/auth/password-reset/${confirm ? "confirm" : "request"}`, {
        method: "POST", body: confirm ? { token: token.trim(), new_password: password } : { username: username.trim() },
      });
      if (!response || response.ok !== true) throw new Error("invalid response");
      if (!mounted.current) return;
      setNotice(confirm ? "done" : "sent");
      if (confirm) { clear(); await refresh(); }
    } catch {
      if (mounted.current) setError("failed");
    } finally {
      gate.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  if (!open) return <Button type="button" onClick={() => setOpen(true)}>{t("title")}</Button>;
  return <section className="mt-6 flex flex-col gap-4" aria-label={t("title")}>
    <h2 className="font-bold">{t("title")}</h2>
    <p>{t("intro")}</p>
    <form onSubmit={(e) => void submit(e, false)} className="flex flex-col gap-3">
      <Field label={t("username")}>{(id) => <Input id={id} dir="ltr" autoComplete="username" maxLength={64} required value={username} disabled={busy} onChange={(e) => setUsername(e.target.value)} />}</Field>
      <Button type="submit" busy={busy} disabled={busy || !username.trim()}>{t("request")}</Button>
    </form>
    <form onSubmit={(e) => void submit(e, true)} className="flex flex-col gap-3">
      <p>{t("warning")}</p>
      <Field label={t("token")}>{(id) => <Input id={id} type="password" autoComplete="off" dir="ltr" minLength={32} maxLength={128} required disabled={busy} value={token} onChange={(e) => setToken(e.target.value)} />}</Field>
      <Field label={t("password")}>{(id) => <Input id={id} type="password" autoComplete="new-password" dir="ltr" minLength={10} maxLength={256} required disabled={busy} value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
      <Field label={t("repeat")}>{(id) => <Input id={id} type="password" autoComplete="new-password" dir="ltr" minLength={10} maxLength={256} required disabled={busy} value={repeat} onChange={(e) => setRepeat(e.target.value)} />}</Field>
      <Button type="submit" busy={busy} disabled={busy || token.trim().length < 32 || !password || password !== repeat}>{t("confirm")}</Button>
    </form>
    <div aria-live="polite">{notice && <p role="status">{t(notice)}</p>}{error && <p role="alert">{t(error)}</p>}</div>
    <Button type="button" disabled={busy} onClick={() => { clear(); setNotice(null); setError(null); setOpen(false); }}>{t("close")}</Button>
  </section>;
}
