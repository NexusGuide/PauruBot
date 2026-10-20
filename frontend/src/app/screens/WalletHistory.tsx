import { dateTime, money } from "@shared/format";
import { api } from "../api";
import { Icon } from "../icons";
import { useApp } from "../state";
import { ReadFailure } from "../ui";
import { useResource } from "../useResource";

/** A failed read is not an empty ledger. Retries never perform a financial action. */
export function WalletHistory() {
  const { t, locale } = useApp();
  const { data, error, loading, retry } = useResource(api.history);
  return (
    <section className="app-card" aria-label={t("transactions")} aria-busy={loading}>
      {loading ? <div className="app-empty" role="status">{t("ui_loading")}</div> : error ? (
        <ReadFailure error={error} retry={retry} />
      ) : !data?.items.length ? (
        <div className="app-empty">{t("no_transactions")}</div>
      ) : (
        data.items.map((item) => (
          <div key={item.id} className="app-row flex items-center gap-3 px-4">
            <span className="app-ico" data-tone={item.amount > 0 ? "green" : undefined}>
              <Icon name={item.amount > 0 ? "coin" : "wallet"} size={21} />
            </span>
            <span className="app-grow">
              <b className="block text-[0.93rem] font-bold leading-7">{t(`tx_${item.kind}`)}</b>
              <small className="num mt-0.5 block text-[0.75rem] font-medium text-[var(--hint)]">
                {dateTime(item.created_at, locale)}
              </small>
            </span>
            <b className={`num text-[0.93rem] font-bold ${item.amount > 0 ? "text-pista-700" : ""}`} dir="ltr">
              {item.amount > 0 ? "+" : "−"}{money(Math.abs(item.amount), locale)}
            </b>
          </div>
        ))
      )}
    </section>
  );
}
