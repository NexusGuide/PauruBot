import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { call, qs, type Paged } from "./api";

export interface Loaded<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (value: T | ((prev: T | null) => T | null)) => void;
}

/** Fetch on mount and whenever the path changes; keeps the last data while reloading. */
export function useLoad<T>(path: string | null): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const seq = useRef(0);

  const reload = useCallback(async () => {
    if (!path) return;
    const mine = ++seq.current;
    setLoading(true);
    try {
      const value = await call<T>(path);
      if (mine === seq.current) {
        setData(value);
        setError(null);
      }
    } catch (err) {
      if (mine === seq.current) setError(err);
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}

export const PAGE_SIZE = 25;

export interface ListState<T> extends Loaded<Paged<T>> {
  params: URLSearchParams;
  page: number;
  sort: string;
  get: (key: string) => string;
  set: (changes: Record<string, string | null>) => void;
  setPage: (page: number) => void;
  toggleSort: (column: string) => void;
}

/**
 * A server-paged list whose filters, sort and page live in the URL, so a filtered view can be
 * bookmarked, shared with another admin, and survives a reload.
 */
export function useList<T>(endpoint: string, filterKeys: string[]): ListState<T> {
  const [params, setParams] = useSearchParams();
  const page = Math.max(0, Number(params.get("page") ?? "0") || 0);
  const sort = params.get("sort") ?? "";
  const filterSignature = filterKeys.map((key) => `${key}=${params.get(key) ?? ""}`).join("&");

  const path = useMemo(() => {
    const values: Record<string, string> = {};
    for (const key of filterKeys) {
      const value = params.get(key);
      if (value) values[key] = value;
    }
    return `${endpoint}${qs({ ...values, sort, limit: PAGE_SIZE, offset: page * PAGE_SIZE })}`;
    // `filterSignature` stands in for `params` so unrelated URL changes do not refetch.
  }, [endpoint, filterSignature, sort, page]);

  const loaded = useLoad<Paged<T>>(path);

  const set = useCallback(
    (changes: Record<string, string | null>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(changes)) {
            if (value === null || value === "") next.delete(key);
            else next.set(key, value);
          }
          if (!("page" in changes)) next.delete("page");
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const setPage = useCallback((value: number) => set({ page: value > 0 ? String(value) : null }), [set]);

  const toggleSort = useCallback(
    (column: string) => {
      const next = sort === column ? `-${column}` : sort === `-${column}` ? null : column;
      set({ sort: next });
    },
    [sort, set],
  );

  const get = useCallback((key: string) => params.get(key) ?? "", [params]);

  return { ...loaded, params, page, sort, get, set, setPage, toggleSort };
}

/** Run an async action with a busy flag; errors are handed to the caller's handler. */
export function useAction(): [boolean, <R>(fn: () => Promise<R>) => Promise<R | undefined>] {
  const [busy, setBusy] = useState(false);
  const run = useCallback(async <R,>(fn: () => Promise<R>) => {
    setBusy(true);
    try {
      return await fn();
    } finally {
      setBusy(false);
    }
  }, []);
  return [busy, run];
}

/** A Set of selected row ids that resets whenever the visible rows change. */
export function useSelection(rowKey: string): {
  selected: Set<number>;
  toggle: (id: number) => void;
  setAll: (ids: number[], on: boolean) => void;
  clear: () => void;
} {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  useEffect(() => setSelected(new Set()), [rowKey]);
  const toggle = useCallback((id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const setAll = useCallback((ids: number[], on: boolean) => {
    setSelected(on ? new Set(ids) : new Set());
  }, []);
  const clear = useCallback(() => setSelected(new Set()), []);
  return { selected, toggle, setAll, clear };
}
