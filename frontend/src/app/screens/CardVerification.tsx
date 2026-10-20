import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@shared/http";
import { money } from "@shared/format";
import { api } from "../api";
import { cardCopy } from "../cards/copy";
import { normalizeNumber, parseCard, parseVerification, returnPath, submissionGate, validFile, type Verification } from "../cards/model";
import { useApp } from "../state";
import { Button, Panel, Screen } from "../ui";

function photoBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === "string" ? resolve(reader.result.split(",")[1] ?? "") : reject(Error("photo"));
    reader.onerror = () => reject(Error("photo"));
    reader.readAsDataURL(file);
  });
}

export function CardVerification() {
  const { me } = useApp();
  return me ? <CardForm key={me.id} /> : null;
}

function CardForm() {
  const { locale, me } = useApp();
  const back = returnPath(useLocation().state, me!.id);
  const c = cardCopy(locale);
  const navigate = useNavigate();
  const [state, setState] = useState<Verification | null>(null);
  const [number, setNumber] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState("");
  const gate = useRef(submissionGate());
  const active = useRef(true);
  const request = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const refresh = useCallback(async () => {
    const seq = ++request.current;
    try {
      const result = parseVerification(await api.cardVerification());
      if (!active.current || seq !== request.current) return;
      setState(result);
      // A read cannot identify which intent produced a row. Keep an uncertain payload for exact replay.
    } catch {
      if (active.current && seq === request.current) { setState(null); setMessage(cardCopy(locale).load_failed); }
    }
  }, [locale]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    active.current = true;
    const current = gate.current;
    return () => { active.current = false; ++request.current; current.clear(); };
  }, []);

  const submit = async () => {
    if (busy || (!uncertain && !state?.can_submit)) return;
    const normalized = normalizeNumber(number);
    if (!uncertain && (!normalized || !photo || !validFile(photo))) { setMessage(c.invalid); return; }
    // Lock before FileReader/await, so a rapid second click cannot create another payload.
    setBusy(true);
    const locked = gate.current.enter({ intent_key: crypto.randomUUID(), number: normalized ?? "", image: "" });
    if (!locked) return;
    try {
      if (!uncertain) {
        try { locked.image = await photoBase64(photo!); }
        catch { gate.current.clear(); if (active.current) setMessage(c.invalid); return; }
      }
      if (!active.current) return;
      const result = await api.submitCardVerification(locked);
      parseCard((result as { card?: unknown })?.card);
      if (!active.current) return;
      gate.current.clear();
      setUncertain(false); setNumber(""); setPhoto(null);
      if (input.current) input.current.value = "";
      setMessage(c.sent);
      await refresh();
    } catch (error) {
      if (!active.current) return;
      // A definite validation/policy response can be corrected; network/5xx retain exactly one payload.
      if (error instanceof ApiError && [403, 409, 413, 415, 422].includes(error.status)) {
        gate.current.clear(); setUncertain(false); setMessage(c.invalid); await refresh();
      } else { setUncertain(true); setMessage(c.failed); }
    } finally {
      gate.current.leave();
      if (active.current) setBusy(false);
    }
  };
  return <Screen title={c.title}>
    <p>{c.intro}</p>
    <p className="app-hint">{c.privacy}</p>
    <Button disabled={busy} onClick={() => void refresh()}>{c.refresh}</Button>
    {message && <p role="status">{message}</p>}
    {state && <>
      <Panel>
        <p>{c.minimum}: {money(state.min_amount, locale)}</p>
        {state.blocked ? <p>{c.blocked}</p> : state.trusted ? <p>{c.trusted}</p> : !state.enabled ? <p>{c.disabled}</p> : state.approved ? <p>{c.approved}</p> : null}
        {state.items.map((card) => <p key={card.id}><b dir="ltr">{card.masked}</b> — {c[card.status]}</p>)}
      </Panel>
      {(state.can_submit || uncertain) && <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <label>{c.number}<input type="text" inputMode="numeric" autoComplete="off" maxLength={32}
          disabled={busy || uncertain} value={number} onChange={(e) => setNumber(e.target.value)} /></label>
        <label>{c.photo}<input ref={input} type="file" accept="image/jpeg,image/png"
          disabled={busy || uncertain} onChange={(e) => { setPhoto(e.target.files?.[0] ?? null); setMessage(""); }} /></label>
        <Button type="submit" disabled={busy}>{uncertain ? c.retry : c.send}</Button>
      </form>}
    </>}
    <Button disabled={busy} onClick={() => navigate(back)}>{back === "/wallet" ? c.back : c.checkout}</Button>
  </Screen>;
}
