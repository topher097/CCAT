/** Typed duplication-audit endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type DuplicationListPage = Schemas['DuplicationListPage']
export type DuplicationListItem = Schemas['DuplicationListItem']
export type DuplicationDetail = Schemas['DuplicationDetail']
export type DuplicationMember = Schemas['DuplicationMember']
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
): Promise<DuplicationDetail> {
  const query = new URLSearchParams()
  if (filters.q) query.set('q', filters.q)
  if (filters.entity && filters.entity !== 'all')
    query.set('entity', filters.entity)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.post<DuplicationDetail>(
    `/api/v1/seasons/${seasonPath(season)}/duplications/${encodeURIComponent(groupId)}/audit${suffix}`,
    {},
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
