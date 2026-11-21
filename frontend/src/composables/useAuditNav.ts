/** The list query the audit views carry from a list to a detail and back. */

import { computed, type ComputedRef } from 'vue'
import { useRoute } from 'vue-router'

/**
 * Read the audit list context from the URL.
 *
 * The list views keep their filters in the query string, so a detail
 * link carries them and the back button restores the place in the
 * table the user came from.
 *
 * @param listPath The list route the back button returns to; a
 *   function keeps it reactive when the view serves several lists.
 * @returns The carried query string and the list URL.
 */
export function useAuditNav(listPath: string | (() => string)): {
  query: ComputedRef<string>
  listUrl: ComputedRef<string>
} {
  const route = useRoute()
  const path = computed(() =>
    typeof listPath === 'function' ? listPath() : listPath,
  )
  const query = computed(() => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(route.query)) {
      if (typeof value === 'string' && value) params.set(key, value)
    }
    return params.toString()
  })
  const listUrl = computed(() =>
    query.value ? `${path.value}?${query.value}` : path.value,
  )
  return { query, listUrl }
}
