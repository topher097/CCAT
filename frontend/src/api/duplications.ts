/** Typed duplication-audit endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

// The generated schema carries these fields; the intersections narrow the
// member state to its three values.
export type DuplicationListItem = Schemas['DuplicationListItem'] & {
  survivor_count: number
  released_count: number
}
export type DuplicationDetail = Schemas['DuplicationDetail'] & {
  survivor_count: number
  children: DuplicationChild[]
}
export type DuplicationMember = Schemas['DuplicationMember'] & {
  state: 'surviving' | 'released' | 'discarded'
  row_key: string
  row_status: string | null
}
export type DuplicationChild = {
  zite_record_id: string
  first_name: string | null
  dob: string | null
  family_id: number | null
  angel_id: string | null
}

export type DuplicationListPage = Schemas['DuplicationListPage']
export type DuplicationDecision = 'not_duplicate' | 'ignore'

/** Season name -> URL-safe path segment. */
function seasonPath(season: string): string {
  return encodeURIComponent(season)
}

export type DuplicationListParams = {
  page?: number
  page_size?: number
  q?: string
  entity?: string | null
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
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/duplications${suffix}`,
  )
}

/** GET one duplicate group with its raw and screened members. */
export function getDuplication(
  season: string,
  groupId: string,
  filters: { q?: string; entity?: string | null } = {},
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}${suffix}`,
  )
}

/** Mark a resolved duplication audited; the refreshed group comes back. */
export function auditDuplication(
  season: string,
  groupId: string,
  filters: { q?: string; entity?: string | null } = {},
  body?: { values?: Record<string, unknown>; expected_version?: number | null },
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
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
  filters: { q?: string; entity?: string | null } = {},
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
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
  filters: { q?: string; entity?: string | null } = {},
): Promise<DuplicationDetail> {
  const body: Record<string, unknown> = { decision, confirm: true }
  if (expectedVersion !== undefined && expectedVersion !== null)
    body.expected_version = expectedVersion
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.post<DuplicationDetail>(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}/members/${encodeURIComponent(recordId)}${suffix}`,
    body,
  )
}
