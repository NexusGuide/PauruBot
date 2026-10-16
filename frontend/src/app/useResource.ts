import { useCallback, useEffect, useState } from "react";

/** Read-only screen state: failures are not empty success; obsolete results are ignored. */
export function useResource<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(null);
    void load().then(value => { if (active) setData(value); })
      .catch(reason => { if (active) setError(reason); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [load, attempt]);
  const retry = useCallback(() => setAttempt(value => value + 1), []);
  return { data, error, loading, retry };
}
