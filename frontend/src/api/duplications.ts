/** Typed duplication-audit endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type DuplicationKind = 'duplicate_submission' | 'angel_claim_conflict'

/** Auditor-facing names for each group kind. */
export const DUPLICATION_KIND_LABELS: Record<DuplicationKind, string> = {
  duplicate_submission: 'Duplicate submission',
  angel_claim_conflict: 'Assigned Angel ID conflict',
}

// The generated schema carries these fields; the intersections narrow the
// member state to its three values.
export type DuplicationListItem = Schemas['DuplicationListItem'] & {
  kind: DuplicationKind
  survivor_count: number
  released_count: number
}
export type DuplicationDetail = Schemas['DuplicationDetail'] & {
  kind: DuplicationKind
  survivor_count: number
  children: DuplicationChild[]
  /** The group's merged editable fields: newest non-blank value wins. */
  current: Record<string, unknown>
  /** The row key the merged entry is written to. */
  current_row_key: string
}
export type DuplicationChild = {
  zite_record_id: string
  first_name: string | null
  dob: string | null
  family_id: number | null
  angel_id: string | null
}
// The schema already has an index signature, so this intersection narrows the
// children field now and remains compatible once the generated type carries it.
export type DuplicationMember = Schemas['DuplicationMember'] & {
  state: 'surviving' | 'released' | 'discarded'
  row_key: string
  row_status: string | null
  children: DuplicationChild[]
}

export type DuplicationListPage = Schemas['DuplicationListPage']
export type DuplicationDecision = 'not_duplicate' | 'ignore'
export type DuplicationFilters = {
  q?: string
  entity?: string | null
  kind?: string | null
}

/** Season name -> URL-safe path segment. */
function seasonPath(season: string): string {
  return encodeURIComponent(season)
}

export type DuplicationListParams = DuplicationFilters & {
  page?: number
  page_size?: number
}

/** GET one server-paginated duplicate-group list page. */
export function listDuplications(
  season: string,
  params: DuplicationListParams = {},
): Promise<DuplicationListPage> {
  const query = new URLSearchParams()
  if (params.page !== undefined) query.set('page', String(params.page))
  if (params.page_size !== undefined)
    query.set('page_size', String(params.page_size))
  if (params.q) query.set('q', params.q)
  if (params.entity && params.entity !== 'all')
    query.set('entity', params.entity)
  if (params.kind && params.kind !== 'all') query.set('kind', params.kind)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/duplications${suffix}`,
  )
}

/** GET one duplicate group with its raw and screened members. */
export function getDuplication(
  season: string,
  groupId: string,
  filters: DuplicationFilters = {},
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  if (filters.kind && filters.kind !== 'all') query.set('kind', filters.kind)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}${suffix}`,
  )
}

/** Mark a resolved duplication audited; the refreshed group comes back. */
export function auditDuplication(
  season: string,
  groupId: string,
  filters: DuplicationFilters = {},
  body?: { values?: Record<string, unknown>; expected_version?: number | null },
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  if (filters.kind && filters.kind !== 'all') query.set('kind', filters.kind)
  const suffix = query.size ? `?${query.toString()}` : ''
  const postBody: Record<string, unknown> = {}
  if (body?.values !== undefined) postBody.values = body.values
  if (body?.expected_version !== undefined && body.expected_version !== null)
    postBody.expected_version = body.expected_version
  return api.post<DuplicationDetail>(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}/audit${suffix}`,
    postBody,
  )
}

/** Assign children to families after a family-group split. */
export function assignDuplicationChildren(
  season: string,
  groupId: string,
  assignments: Record<string, number>,
  filters: DuplicationFilters = {},
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  if (filters.kind && filters.kind !== 'all') query.set('kind', filters.kind)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.post<DuplicationDetail>(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}/children${suffix}`,
    { assignments },
  )
}

/** Record one confirmed member decision; the refreshed group comes back. */
export function decideDuplication(
  season: string,
  groupId: string,
  recordId: string,
  decision: DuplicationDecision,
  expectedVersion?: number | null,
  filters: DuplicationFilters = {},
): Promise<DuplicationDetail> {
  const body: Record<string, unknown> = { decision, confirm: true }
  if (expectedVersion !== undefined && expectedVersion !== null)
    body.expected_version = expectedVersion
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  if (filters.kind && filters.kind !== 'all') query.set('kind', filters.kind)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.post<DuplicationDetail>(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}/members/${encodeURIComponent(recordId)}${suffix}`,
    body,
  )
}
