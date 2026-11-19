/** 412 conflict extraction from the server's stale-version body (§4.4). */

import { ApiError } from '../api/client'

export type ConflictInfo = {
  mode: 'edit' | 'ignore'
  /** The fresh winning row (canonical names). */
  row: Record<string, unknown>
  currentVersion: number
  lastUpdatedBy: string | null
  lastUpdatedAt: string | null
  message: string
}

/** Build conflict state from a 412 ApiError; null for any other error. */
export function conflictFromError(
  error: ApiError,
  mode: ConflictInfo['mode'],
): ConflictInfo | null {
  if (error.status !== 412) return null
  const row = error.context.row
  const version = error.context.current_version
  if (typeof row !== 'object' || row === null) return null
  return {
    mode,
    row: row as Record<string, unknown>,
    currentVersion: Number(version),
    lastUpdatedBy:
      typeof error.context.last_updated_by === 'string'
        ? error.context.last_updated_by
        : null,
    lastUpdatedAt:
      typeof error.context.last_updated_at === 'string'
        ? error.context.last_updated_at
        : null,
    message: error.message,
  }
}
