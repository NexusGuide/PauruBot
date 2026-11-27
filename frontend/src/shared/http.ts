export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public fields?: string[],
    public reason?: string,
  ) {
    super(code);
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  form?: FormData;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  let body: BodyInit | undefined;
  if (options.form) {
    body = options.form;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {
    method: options.method ?? (body ? "POST" : "GET"),
    headers,
    body,
    credentials: "same-origin",
    signal: options.signal,
  });
  if (!response.ok) {
    let code = `http_${response.status}`;
    let fields: string[] | undefined;
    let reason: string | undefined;
    try {
      const data = (await response.json()) as { error?: unknown; fields?: unknown; reason?: unknown };
      if (typeof data.error === "string") code = data.error;
      if (Array.isArray(data.fields)) {
        fields = data.fields.filter((f): f is string => typeof f === "string" && f.length > 0);
      }
      if (typeof data.reason === "string" && data.reason) reason = data.reason;
    } catch {
      // non-JSON error body
    }
    throw new ApiError(response.status, code, fields, reason);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
