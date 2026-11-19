/** Typed audit/overview endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type Entity = 'sponsor' | 'family' | 'child'
export type EntityDetail = Schemas['EntityDetail']
export type EntityListPage = Schemas['EntityListPage']
export type EntityPatch = Schemas['EntityPatch']
export type Overview = Schemas['Overview']
export type SeasonView = Schemas['SeasonView']

/** URL segment per entity. */
export const SEGMENT: Record<Entity, string> = {
  sponsor: 'sponsors',
  family: 'families',
  child: 'children',
}

/** Season name -> URL-safe path segment. */
function seasonPath(season: string): string {
  return encodeURIComponent(season)
}

export type ListParams = {
  page?: number
  page_size?: number
  q?: string
  status?: string | null
}

/** GET one server-paginated entity list page. */
export function listEntities(
  season: string,
  entity: Entity,
  params: ListParams = {},
): Promise<EntityListPage> {
  const query = new URLSearchParams()
  if (params.page !== undefined) query.set('page', String(params.page))
  if (params.page_size !== undefined)
    query.set('page_size', String(params.page_size))
  if (params.q) query.set('q', params.q)
  if (params.status && params.status !== 'all')
    query.set('status', params.status)
  const suffix = query.size ? `?${query.toString()}` : ''
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/${SEGMENT[entity]}${suffix}`,
  )
}

/** GET one entity's full detail (baseline + submissions + version). */
export function getEntity(
  season: string,
  entity: Entity,
  key: string,
): Promise<EntityDetail> {
  return api.get(
    `/api/v1/seasons/${seasonPath(season)}/${SEGMENT[entity]}/${encodeURIComponent(key)}`,
  )
}

/** PATCH touched fields only, guarded by If-Match = row_version. */
export function patchEntity(
  season: string,
  entity: Entity,
  key: string,
  patch: EntityPatch,
  rowVersion: number,
): Promise<EntityDetail> {
  return api.patch<EntityDetail>(
    `/api/v1/seasons/${seasonPath(season)}/${SEGMENT[entity]}/${encodeURIComponent(key)}`,
    patch,
    { 'If-Match': String(rowVersion) },
  )
}

/** Confirmed destructive ignore/unignore (expected_version required). */
export function setIgnored(
  season: string,
  entity: Entity,
  key: string,
  action: 'ignore' | 'unignore',
  expectedVersion: number,
): Promise<EntityDetail> {
  return api.post<EntityDetail>(
    `/api/v1/seasons/${seasonPath(season)}/${SEGMENT[entity]}/${encodeURIComponent(key)}/${action}`,
    { confirm: true, expected_version: expectedVersion },
  )
}

/** GET the server-computed season overview. */
export function getOverview(season: string): Promise<Overview> {
  return api.get<Overview>(
    `/api/v1/seasons/${seasonPath(season)}/overview`,
  )
}

/** GET the configured active season. */
export function getCurrentSeason(): Promise<SeasonView> {
  return api.get<SeasonView>('/api/v1/seasons/current')
}
