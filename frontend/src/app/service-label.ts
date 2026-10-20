/** Display label only, not the panel username, credential or subscription URL.
 * Match the API's 48-code-point limit after trimming. Native input maxLength is
 * conservatively UTF-16 based; this validator also guards programmatic submits.
 */
export function normalizedServiceLabel(value: string): string | null {
  const label = value.trim();
  return label.length > 0 && Array.from(label).length <= 48 ? label : null;
}
