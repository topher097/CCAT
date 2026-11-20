/** Season overview state: manual refresh + 30s poll paused when hidden. */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { getOverview } from '../api/audit'
import type { Overview } from '../api/audit'

/** Poll interval (§7.1.2: server-computed, cheap refresh). */
export const POLL_MS = 30_000

export const useOverviewStore = defineStore('overview', () => {
  const data = ref<Overview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastLoaded = ref<Date | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null

  const season = computed(() => data.value?.season ?? null)

  /** Fetch the overview for the session's season. */
  async function refresh(seasonName: string | null): Promise<void> {
    if (!seasonName) return
    loading.value = true
    error.value = null
    try {
      data.value = await getOverview(seasonName)
      lastLoaded.value = new Date()
    } catch (failure) {
      error.value =
        failure instanceof Error ? failure.message : 'overview unavailable'
    } finally {
      loading.value = false
    }
  }

  /** Start the 30s poll; hidden documents skip work. */
  function startPolling(seasonName: string | null): void {
    stopPolling()
    timer = setInterval(() => {
      if (!document.hidden) void refresh(seasonName)
    }, POLL_MS)
  }

  function stopPolling(): void {
    if (timer !== null) clearInterval(timer)
    timer = null
  }

  /** Drop cached overview data on logout/season switch (§7.2). */
  function clear(): void {
    stopPolling()
    data.value = null
    error.value = null
    lastLoaded.value = null
  }

  return {
    data,
    loading,
    error,
    lastLoaded,
    season,
    refresh,
    startPolling,
    stopPolling,
    clear,
  }
})
