/** Field diff + tri-state patch helpers (§7.2, §9.3). */

/** Compare form values the way the store does (blank == null). */
export function normalizeValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).trim()
}

/** Whether a draft value differs from its baseline. */
export function differs(a: unknown, b: unknown): boolean {
  return normalizeValue(a) !== normalizeValue(b)
}

/** Fields whose current draft value differs from the baseline. */
export function changedFields(
  draft: Record<string, unknown>,
  baseline: Record<string, unknown>,
  fields: readonly string[],
): string[] {
  return fields.filter((field) => differs(draft[field], baseline[field]))
}

/**
 * Build the tri-state PATCH body: only touched keys are present, so
 * omitted fields stay unchanged server-side.
 */
export function buildPatch(
  draft: Record<string, unknown>,
  touched: ReadonlySet<string>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  for (const field of touched) {
    const value = draft[field]
    payload[field] =
      value === null || value === undefined || normalizeValue(value) === ''
        ? null
        : value
  }
  return payload
}

/** Normalize an age value the server expects as int | null. */
export function toAge(value: unknown): number | null {
  const text = normalizeValue(value)
  if (!text) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null
}
