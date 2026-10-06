import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError, call } from "../api";
import { useSession } from "../session";
import { Button, Field, Input } from "../ui";

function Frame({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  const { t, locale, setLocale } = useSession();
  return (
    <div className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <p className="mb-8 text-2xl font-extrabold tracking-tight text-firouzeh-700">{t("brand")}</p>
        <h1 className="text-[1.6rem] font-extrabold leading-tight">{title}</h1>
        <p className="mt-2 mb-7 leading-relaxed text-[var(--hint)]">{intro}</p>
        {children}
        <button
          type="button"
          onClick={() => setLocale(locale === "fa" ? "en" : "fa")}
          className="mt-8 text-[0.85rem] font-semibold text-[var(--hint)] hover:text-[var(--text)]"
        >
          {locale === "fa" ? "English" : "فارسی"}
        </button>
      </div>
    </div>
  );
}

export function Login() {
  const { t, login, errorText } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(err instanceof ApiError && err.code === "invalid_credentials" ? t("login_wrong") : errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame title={t("login_title")} intro={t("login_intro")}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label={t("username")}>
          {(id) => (
            <Input
              id={id}
              autoComplete="username"
              dir="ltr"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          )}
        </Field>
        <Field label={t("password")}>
          {(id) => (
            <Input
              id={id}
              type="password"
              autoComplete="current-password"
              dir="ltr"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          )}
        </Field>
        {error && (
          <p role="alert" className="rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] font-semibold text-anar-700">
            {error}
          </p>
        )}
        <Button type="submit" tone="primary" busy={busy} className="mt-2">
          {t("login_submit")}
        </Button>
      </form>
    </Frame>
  );
}

export function Setup() {
  const { t, refresh, login, errorText } = useSession();
  const [form, setForm] = useState({
    token: new URLSearchParams(window.location.search).get("token") ?? "",
    shop_title: "", username: "", password: "", telegram_id: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const update = (key: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((prev) => ({ ...prev, [key]: event.target.value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await call("/api/setup/complete", {
        method: "POST",
        body: {
          token: form.token.trim(),
          shop_title: form.shop_title.trim(),
          username: form.username.trim(),
          password: form.password,
          telegram_id: form.telegram_id.trim() ? Number(form.telegram_id.trim()) : null,
        },
      });
      await refresh();
      await login(form.username.trim(), form.password);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame title={t("setup_title")} intro={t("setup_intro")}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label={t("setup_token")} hint={t("setup_token_hint")}>
          {(id) => <Input id={id} dir="ltr" value={form.token} onChange={update("token")} required />}
        </Field>
        <Field label={t("setup_shop_title")}>
          {(id) => <Input id={id} value={form.shop_title} onChange={update("shop_title")} required />}
        </Field>
        <Field label={t("username")}>
          {(id) => (
            <Input id={id} dir="ltr" autoComplete="username" value={form.username} onChange={update("username")} required />
          )}
        </Field>
        <Field label={t("password")} hint={t("password_hint")}>
          {(id) => (
            <Input
              id={id}
              type="password"
              dir="ltr"
              autoComplete="new-password"
              minLength={10}
              value={form.password}
              onChange={update("password")}
              required
            />
          )}
        </Field>
        <Field label={t("setup_telegram_id")} hint={t("setup_telegram_id_hint")}>
          {(id) => (
            <Input id={id} dir="ltr" inputMode="numeric" value={form.telegram_id} onChange={update("telegram_id")} />
          )}
        </Field>
        {error && (
          <p role="alert" className="rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] font-semibold text-anar-700">
            {error}
          </p>
        )}
        <Button type="submit" tone="primary" busy={busy} className="mt-2">
          {t("setup_submit")}
        </Button>
      </form>
    </Frame>
  );
}
