/** Typed manual-matching endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type ManualMatchPoolPage = Schemas['ManualMatchPoolPage']
export type ManualMatchFamilyCard = Schemas['ManualMatchFamilyCard']
export type ManualMatchChildCard = Schemas['ManualMatchChildCard']
export type ManualMatchSponsor = Schemas['ManualMatchSponsor']
export type ManualMatchSponsorPage = Schemas['ManualMatchSponsorPage']
export type ManualMatchAutomaticRun = Schemas['ManualMatchAutomaticRun']
export type ManualMatchHold = Schemas['ManualMatchHold']
export type ManualAssignmentRequest = Schemas['ManualAssignmentRequest']
export type ManualReleaseRequest = Schemas['ManualReleaseRequest']
export type ManualAssignmentResult = Schemas['ManualAssignmentResult']
export type ManualAssignmentListPage = Schemas['ManualAssignmentListPage']
export type ManualHoldRequest = Schemas['ManualHoldRequest']
export type ManualHoldReleaseRequest = Schemas['ManualHoldReleaseRequest']
export type ManualHoldResult = Schemas['ManualHoldResult']
export type ManualHoldListPage = Schemas['ManualHoldListPage']

/** The four family assignment states. */
export type AssignmentState =
  | 'unassigned'
  | 'partial'
  | 'split'
  | 'assigned'

/** Season name -> URL-safe path segment. */
function seasonPath(season: string): string {
  return encodeURIComponent(season)
}

/** The manual-matching pool's filters. */
export type PoolParams = {
  page?: number
  page_size?: number
  q?: string
  assignment?: AssignmentState | 'any'
  child_gender?: string
  age_min?: number
  age_max?: number
  special_needs?: 'any' | 'yes' | 'no'
  location?: 'any' | 'hancock' | 'calumet'
  family_size_min?: number
  family_size_max?: number
  child_status?: string
  hold?: 'any' | 'held' | 'not_held'
  sponsor_id?: number
}

/** The sponsor picker's filters. */
export type SponsorParams = {
  page?: number
  page_size?: number
  q?: string
  affiliation?: string
  has_capacity?: boolean
}

/** The assignment list's filters. */
export type AssignmentListParams = {
  page?: number
  page_size?: number
  sponsor_id?: number
  source?: 'auto' | 'manual'
  family_id?: number
}

/** Build a query string from the defined, non-empty parameters. */
function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const suffix = search.toString()
  return suffix ? `?${suffix}` : ''
}

/** GET one page of the manual-matching pool. */
export function listPool(
  season: string,
  params: PoolParams = {},
): Promise<ManualMatchPoolPage> {
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/pool${query(params)}`,
  )
}

/** GET one page of the sponsor picker. */
export function listSponsors(
  season: string,
  params: SponsorParams = {},
): Promise<ManualMatchSponsorPage> {
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/sponsors${query(params)}`,
  )
}

/** GET the current assignments, derived from the children. */
export function listAssignments(
  season: string,
  params: AssignmentListParams = {},
): Promise<ManualAssignmentListPage> {
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/assignments${query(params)}`,
  )
}

/** Place the selection with a sponsor. */
export function assign(
  season: string,
  body: ManualAssignmentRequest,
): Promise<ManualAssignmentResult> {
  return api.post(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/assignments`,
    body,
  )
}

/** Release assignments. */
export function release(
  season: string,
  body: ManualReleaseRequest,
): Promise<ManualAssignmentResult> {
  return api.post(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/release`,
    body,
  )
}

/** GET the active holds, with their notes. */
export function listHolds(
  season: string,
  params: { page?: number; page_size?: number } = {},
): Promise<ManualHoldListPage> {
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/holds${query(params)}`,
  )
}

/** Hold a family, with the reason. */
export function holdFamily(
  season: string,
  body: ManualHoldRequest,
): Promise<ManualHoldResult> {
  return api.post(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/holds`,
    body,
  )
}

/** Release a family's hold. */
export function releaseHold(
  season: string,
  body: ManualHoldReleaseRequest,
): Promise<ManualHoldResult> {
  return api.post(
    `/api/v1/seasons/${seasonPath(season)}/manual-match/holds/release`,
    body,
  )
}
