/** Special-needs serialization — byte-identical to the Python service. */

export const SPECIAL_NEEDS_OPTIONS = [
  'ADHD',
  'Autism',
  'Down Syndrome',
  'other',
] as const

export type SpecialNeedsSelection = {
  /** Selected pieces; "other" is its own selectable piece (legacy). */
  fixed: Set<string>
  /** Custom text behind the "other" piece. */
  other: string
}

/** Parse one stored semicolon-joined value (None/"" = none). */
export function parseSpecialNeeds(text: string | null | undefined): SpecialNeedsSelection {
  const parts = (text ?? '')
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
  const fixed = new Set<string>()
  let other = ''
  for (const part of parts) {
    if (part.toLowerCase().startsWith('other')) {
      other = part.slice(5).replace(/^[:\s]+/, '').trim()
    } else {
      fixed.add(part)
    }
  }
  return { fixed, other }
}

/**
 * Serialize one selection: sorted fixed pieces, "other: {custom}" last,
 * null when empty. Custom text is emitted only when "other" is selected
 * (legacy checkbox semantics).
 */
export function serializeSpecialNeeds(
  selection: SpecialNeedsSelection,
): string | null {
  const pieces = [...selection.fixed].sort()
  const index = pieces.indexOf('other')
  if (index >= 0) {
    pieces.splice(index, 1)
    const custom = selection.other.trim()
    pieces.push(custom ? `other: ${custom}` : 'other')
  }
  return pieces.length ? pieces.join('; ') : null
}
