export const MAX_BYTES = 4 * 1024 * 1024;
export const TYPES = ["image/png", "image/jpeg", "text/plain"];
export function fileMime(file: { type: string; name?: string }): string {
  return file.type || (/\.(txt|log)$/i.test(file.name ?? "") ? "text/plain" : "");
}
export function validFile(file: { size: number; type: string; name?: string }): boolean {
  const mime = fileMime(file);
  return file.size > 0 && file.size <= MAX_BYTES && TYPES.includes(mime) && (mime !== "text/plain" || file.size <= 512 * 1024);
}
/** Reuse the exact key on an ambiguous retry; edited payload gets a new key. */
export function intent(previous: { body: string; key: string } | null, body: string): { body: string; key: string } {
  return previous?.body === body ? previous : { body, key: crypto.randomUUID() };
}
