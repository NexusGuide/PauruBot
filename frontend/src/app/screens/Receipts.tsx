import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { money } from "@shared/format";
import { api } from "../api";
import { translator, type CopyKey } from "../receipts/copy";
import { parseInbox, type ReceiptState } from "../receipts/model";
import { useApp } from "../state";
import { Button, Panel, Screen } from "../ui";

export function Receipts() {
  const { locale } = useApp(),
    c = translator(locale);
  const [page, setPage] = useState<{
    items: ReceiptState[];
    next_before: number | null;
  } | null>(null);
  const [error, setError] = useState(false),
    [busy, setBusy] = useState(false);
  const alive = useRef(true),
    working = useRef(false),
    cursor = useRef<number | undefined>(undefined);
  const load = async (before?: number) => {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(false);
    try {
      const value = parseInbox(await api.receipts(before), before);
      if (alive.current) {
        setPage(value);
        cursor.current = before;
      }
    } catch {
      if (alive.current) {
        setError(true);
        setPage(null);
      }
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  };
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, []);
  return (
    <Screen title={c("title")}>
      <p className="mb-4 text-sm">{c("hint")}</p>
      {busy && <p role="status">{c("loading")}</p>}
      {error && <p role="alert">{c("error")}</p>}
      {page && page.items.length === 0 && <Panel>{c("empty")}</Panel>}
      <ul className="flex flex-col gap-3">
        {page?.items.map((value) => (
          <li key={value.id}>
            <Link
              to={`/payment/${value.id}`}
              className="block rounded-3xl bg-[var(--surface)] p-4"
            >
              <p className="font-bold">
                #{value.id} · {money(value.amount, locale)} {value.currency}
              </p>
              <p>{c(value.phase as CopyKey)}</p>
              {value.correction && (
                <p>
                  {c(
                    value.correction.kind === "amount"
                      ? "amountFix"
                      : "cardFix",
                  )}
                </p>
              )}
            </Link>
          </li>
        ))}
      </ul>
      <div className="mt-4 flex flex-col gap-2">
        <Button disabled={busy} onClick={() => void load(cursor.current)}>
          {c("refresh")}
        </Button>
        {page?.next_before && (
          <Button
            disabled={busy}
            onClick={() => void load(page.next_before ?? undefined)}
          >
            {c("more")}
          </Button>
        )}
      </div>
    </Screen>
  );
}
