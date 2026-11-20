/** Clarification draft/contact helpers (§7.1.7, D5). */

import { api } from './client'
import type { Schemas } from './client'
import type { Job } from './jobs'

export type ClarifyFamily = Schemas['ClarifyFamilyView']
export type ClarifyDraft = Schemas['ClarifyDraftView']

/** One draft paragraph (keyed by child, D5). */
export type DraftParagraph = {
  zite_record_id: string
  angel_id?: string | null
  order: number
  text: string
}

function path(season: string, familyId?: number | string): string {
  const root = `/api/v1/seasons/${encodeURIComponent(season)}/clarifications`
  return familyId === undefined
    ? root
    : `${root}/${encodeURIComponent(String(familyId))}`
}

/** GET every flagged family with its draft and contact state. */
export function listClarifications(season: string): Promise<ClarifyFamily[]> {
  return api.get<ClarifyFamily[]>(path(season))
}

/** GET one family's draft, children, and contact state. */
export function getClarification(
  season: string,
  familyId: number | string,
): Promise<ClarifyFamily> {
  return api.get<ClarifyFamily>(path(season, familyId))
}

/** PATCH touched draft fields, guarded by If-Match = draft version. */
export function patchDraft(
  season: string,
  familyId: number | string,
  patch: Record<string, unknown>,
  version: number,
): Promise<ClarifyDraft> {
  return api.patch<ClarifyDraft>(`${path(season, familyId)}/draft`, patch, {
    'If-Match': String(version),
  })
}

/** Mark contacted / replied (the server stamps the contact time). */
export function setContactFlag(
  season: string,
  familyId: number | string,
  flag: 'contacted' | 'replied',
  value: boolean,
): Promise<ClarifyFamily> {
  return api.post<ClarifyFamily>(`${path(season, familyId)}/${flag}`, {
    [flag]: value,
  })
}

/** Enqueue the CSV export job (artifact download follows). */
export function enqueueClarifyExport(season: string): Promise<Job> {
  return api.post<Job>(
    `/api/v1/seasons/${encodeURIComponent(season)}/clarify-export`,
  )
}
