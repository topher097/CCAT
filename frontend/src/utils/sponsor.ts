/** Shared sponsor display and assignment helpers. */

export type SponsorSummary = {
  sponsor_id: number
  display_name?: string | null
  first_name?: string | null
  last_name?: string | null
  organization_name?: string | null
  num_children?: number | null
  assigned_count?: number | null
  remaining?: number | null
  packets_dispatched?: boolean
}

export function personName(sponsor: SponsorSummary): string {
  return (
    [sponsor.first_name, sponsor.last_name].filter(Boolean).join(' ') ||
    sponsor.display_name ||
    `Sponsor ${sponsor.sponsor_id}`
  )
}

/** Secondary line, only when it adds something beyond the primary name. */
export function organization(sponsor: SponsorSummary): string {
  const org = sponsor.organization_name ?? ''
  return org && org !== personName(sponsor) ? org : ''
}

/** Slots left; null when the sponsor has no quota (unlimited). */
export function remainingOf(sponsor: SponsorSummary): number | null {
  if (sponsor.num_children === null || sponsor.num_children === undefined) return null
  return (
    sponsor.remaining ??
    Math.max(sponsor.num_children - (sponsor.assigned_count ?? 0), 0)
  )
}

/** Why the dispatch button is off; empty when it is on. */
export function dispatchBlock(sponsor: SponsorSummary): string {
  if (sponsor.packets_dispatched === true) return 'Packets already dispatched'
  const remaining = remainingOf(sponsor)
  if (remaining === null) return 'A children quota is required before dispatching packets'
  if (remaining > 0) return `Fill all child slots first (${remaining} remaining)`
  return ''
}
