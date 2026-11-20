/** Artifact, match-report, and download helpers (§7.1.10). */

import { api } from './client'
import type { Schemas } from './client'

export type Artifact = Schemas['ArtifactView']
export type MatchReportRow = Record<string, unknown>

/** GET the season's artifacts, optionally filtered by kind. */
export function listArtifacts(
  season: string,
  kind?: string,
): Promise<Artifact[]> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : ''
  return api.get<Artifact[]>(
    `/api/v1/seasons/${encodeURIComponent(season)}/artifacts${query}`,
  )
}

/** Authorized download route for one artifact (never a public file path). */
export function artifactDownloadUrl(artifactId: number | string): string {
  return `/api/v1/artifacts/${encodeURIComponent(String(artifactId))}/download`
}

/** GET the latest match report; 404 means no run exists yet. */
export function getMatchReport(season: string): Promise<MatchReportRow[]> {
  return api.get<MatchReportRow[]>(
    `/api/v1/seasons/${encodeURIComponent(season)}/diagnostics/match-report`,
  )
}
