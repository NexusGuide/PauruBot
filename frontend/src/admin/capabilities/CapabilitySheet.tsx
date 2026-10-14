import { useCallback, useEffect, useRef, useState } from "react";
import { call, type PanelRow } from "../api";
import { useSession } from "../session";
import { Sheet } from "../ui";
import { CHECK_KEYS, cleanupUnconfirmed, needsCleanup, validateReport, type CapabilityReport } from "./report";
import "./capabilities.css";

/** Keep the operational username prefix in LTR even inside Persian instructions. */
function ProbeText({ text }: { text: string }) {
  return <>{text.split("pbprobe_").map((part, index) => <span key={index}>{index > 0 && <bdi dir="ltr">pbprobe_</bdi>}{part}</span>)}</>;
}

/** No probe on open, no automatic write/retry. Closing a tab does not cancel server work. */
export function CapabilitySheet({ panel, onClose }: { panel: PanelRow; onClose: () => void }) {
  const { t, can, locale, errorText } = useSession();
  const [result, setResult] = useState<{ report: CapabilityReport; requestedWrite: boolean; at: Date } | null>(null);
  const [pending, setPending] = useState<"read" | "write" | null>(null);
  const [error, setError] = useState<{ text: string; uncertain: boolean } | null>(null);
  const [ack, setAck] = useState(false);
  const busy = useRef(false), alive = useRef(true), content = useRef<HTMLDivElement>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null), errorBox = useRef<HTMLDivElement>(null), pendingBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = result ? resultHeading.current : error ? errorBox.current : pending ? pendingBox.current : null;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "start", behavior: "instant" });
  }, [result, error, pending]);
  const close = useCallback(() => { if (!busy.current) onClose(); }, [onClose]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const dialog = content.current?.closest('[role="dialog"]');
      if (!dialog) return;
      const controls = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href], select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')].filter(el => el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (!first || !last) { event.preventDefault(); return; }
      const active = document.activeElement;
      if (!dialog.contains(active) || active === dialog || (!event.shiftKey && active === last) || (event.shiftKey && active === first)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => document.removeEventListener("keydown", trap);
  }, []);
  const run = async (write: boolean) => {
    if (busy.current || !can("panels.write") || (write && !ack)) return;
    busy.current = true; setPending(write ? "write" : "read"); setError(null); setResult(null); setAck(false);
    try {
      const report = validateReport(await call<unknown>(`/api/admin/panels/${panel.id}/capabilities`, { method: "POST", body: { write } }));
      if (!write && report.write_tested) throw new Error("capability_invalid");
      if (alive.current) setResult({ report, requestedWrite: write, at: new Date() });
    } catch (err) {
      if (alive.current) setError({ text: err instanceof Error && err.message === "capability_invalid" ? t("err_capability_invalid") : errorText(err), uncertain: write });
    } finally {
      busy.current = false;
      if (alive.current) setPending(null);
    }
  };
  const permitted = can("panels.write");
  return <div className="panel-capabilities"><Sheet open onClose={close} title={t("cap_title", { name: panel.name })}>
    <div ref={content} className="cap-content">
      <p className="cap-hint">{t("cap_scope")}</p>
      <p className="cap-hint">{t("cap_declared_hint")}</p>
      <p className="cap-identity" dir="auto">{panel.name} · {t(`panel_kind_${panel.kind}`)} · #{panel.id}</p>
      {!permitted ? <p role="alert">{t("cap_forbidden")}</p> : <>
        <button type="button" className="cap-button cap-primary" disabled={!!pending} onClick={() => void run(false)}>{t("cap_run_read")}</button>
        <details className="cap-write"><summary>{t("cap_write_title")}</summary>
          <p><ProbeText text={t("cap_write_warning")} /></p>
          <label className="cap-ack"><input type="checkbox" checked={ack} disabled={!!pending} onChange={e => setAck(e.target.checked)} /><span>{t("cap_write_ack")}</span></label>
          <button type="button" className="cap-button cap-danger" disabled={!!pending || !ack} onClick={() => void run(true)}>{t("cap_run_write")}</button>
        </details>
      </>}
      {pending && <div ref={pendingBox} tabIndex={-1} role="status" aria-live="polite"><p>{t(pending === "write" ? "cap_running_write" : "cap_running_read")}</p><p className="cap-hint">{t("cap_running_hint")}</p></div>}
      {error && <div ref={errorBox} tabIndex={-1} role="alert" className="cap-error"><p>{error.text}</p>{error.uncertain && <p><ProbeText text={t("cap_uncertain")} /></p>}<p>{t("cap_no_auto_retry")}</p></div>}
      {!pending && !error && !result && <p role="status" className="cap-hint">{t("cap_not_run")}</p>}
      {result && <section aria-label={t("cap_result")}>
        <h3 ref={resultHeading} tabIndex={-1} className="cap-result-title">{t("cap_result")}: <span className={`cap-status cap-${result.report.status}`}>{t(`cap_status_${result.report.status}`)}</span></h3>
        <p className="cap-hint">{t("cap_observed", { at: result.at.toLocaleString(locale) })}</p>
        <p>{t(result.report.write_tested ? "cap_write_tested" : result.requestedWrite ? "cap_write_not_run" : "cap_read_only")}</p>
        {cleanupUnconfirmed(result.report) && <p role="alert" className="cap-error"><ProbeText text={t(needsCleanup(result.report) ? "cap_cleanup" : "cap_cleanup_unconfirmed")} /></p>}
        <ul className="cap-checks">{result.report.checks.map(check => <li key={check.key}>
          <div className="cap-check-heading"><h4>{CHECK_KEYS.some(key => key === check.key) ? t(`cap_check_${check.key}`) : t("cap_unknown", { key: check.key })}</h4><span className={`cap-status cap-${check.status}`}>{t(`cap_status_${check.status}`)}</span></div>
          {check.detail && <p dir="auto" className="cap-detail">{check.detail}</p>}
        </li>)}</ul>
        <p className="cap-hint">{t("cap_snapshot_hint")}</p>
      </section>}
      <button type="button" className="cap-button" disabled={!!pending} onClick={close}>{t("close")}</button>
    </div>
  </Sheet></div>;
}
