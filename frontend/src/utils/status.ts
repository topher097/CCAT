/** Stored audit status vocabulary, matching ccat.audit.store.STATUS_ORDER. */

export const STATUSES = [
  'new',
  'in_progress',
  'needs_clarification',
  'ignored',
  'audited',
] as const

/** Email/address validation status -> badge CSS class. */
export function checkClass(status: unknown): string {
  const text = String(status ?? '').trim().toLowerCase()
  if (!text) return 'check-unchecked'
  if (text === 'valid' || text === 'found') return 'check-ok'
  if (
    ['invalid', 'not_found', 'do_not_mail', 'spamtrap'].includes(text)
  ) {
    return 'check-bad'
  }
  return 'check-warn'
}

/** Email/address validation status -> display label. */
export function checkLabel(status: unknown): string {
  const text = String(status ?? '').trim().toLowerCase()
  return text ? text.replaceAll('_', ' ') : 'unchecked'
}
