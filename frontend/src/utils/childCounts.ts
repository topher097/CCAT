/** Exact per-family child counts from the paginated children list. */

import { listEntities } from '../api/audit'

/**
 * Count children per family_id.
 *
 * The list API has no server-side child_count column, so this reads
 * child pages and groups client-side (display only, never overview
 * statistics). shortcut: caps at 25 pages (5,000 children); revisit
 * when a server child_count field exists.
 */
export async function loadChildCounts(
  season: string,
): Promise<Map<string, number>> {
  const counts = new Map<string, number>()
  const pageSize = 200
  for (let page = 1; page <= 25; page += 1) {
    const result = await listEntities(season, 'child', {
      page,
      page_size: pageSize,
    })
    for (const child of result.items as Record<string, unknown>[]) {
      if (child.family_id === null || child.family_id === undefined) continue
      const key = String(child.family_id)
      counts.set(key, (counts.get(key) ?? 0) + 1)
    }
    if (page * pageSize >= result.total) break
  }
  return counts
}
