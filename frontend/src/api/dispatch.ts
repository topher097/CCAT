/** Sponsor packet generation, drafting, and sending helpers. */

import { api } from './client'
import type { Schemas } from './client'

export type DispatchAttachment = Schemas['DispatchAttachment']
export type DispatchChildListView = Schemas['DispatchChildListView']
export type DispatchDraft = Schemas['DispatchDraftView']
export type DispatchDraftBatchRequest = Schemas['DispatchDraftBatchRequest']
export type DispatchDraftRequest = Schemas['DispatchDraftRequest']
export type DispatchListView = Schemas['DispatchListView']
export type DispatchRow = Schemas['DispatchRow']
export type DispatchSendRequest = Schemas['DispatchSendRequest']
export type DispatchSendResult = Schemas['DispatchSendResult']
export type DispatchTotals = Schemas['DispatchTotals']

function path(season: string): string {
  return `/api/v1/seasons/${encodeURIComponent(season)}/dispatch`
}

function draftPath(season: string, sponsorId: number): string {
  return `${path(season)}/sponsors/${encodeURIComponent(String(sponsorId))}/draft`
}

export function listDispatch(season: string): Promise<DispatchListView> {
  return api.get<DispatchListView>(path(season))
}

export function listDispatchChildren(
  season: string,
): Promise<DispatchChildListView> {
  return api.get<DispatchChildListView>(`${path(season)}/children`)
}

export function draftDispatchEmails(
  season: string,
  request: DispatchDraftBatchRequest,
): Promise<DispatchListView> {
  return api.post<DispatchListView>(`${path(season)}/drafts`, request)
}

export function getDispatchDraft(
  season: string,
  sponsorId: number,
): Promise<DispatchDraft> {
  return api.get<DispatchDraft>(draftPath(season, sponsorId))
}

export function patchDispatchDraft(
  season: string,
  sponsorId: number,
  patch: DispatchDraftRequest,
  version: number,
): Promise<DispatchDraft> {
  return api.patch<DispatchDraft>(draftPath(season, sponsorId), patch, {
    'If-Match': String(version),
  })
}

export function sendDispatchEmail(
  season: string,
  sponsorId: number,
  request: DispatchSendRequest,
): Promise<DispatchSendResult> {
  return api.post<DispatchSendResult>(
    `${path(season)}/sponsors/${encodeURIComponent(String(sponsorId))}/send`,
    request,
  )
}
