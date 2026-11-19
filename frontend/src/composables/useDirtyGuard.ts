/** Shared dirty-form flag feeding the router's navigation guard (§7.2). */

import { onUnmounted, ref, watch } from 'vue'
import type { Ref } from 'vue'

export const formDirty = ref(false)

export function setDirty(value: boolean): void {
  formDirty.value = value
}

/**
 * Mirror one editor's dirty state into the global navigation guard.
 * The guard lives in the router so component-less tests can drive it.
 */
export function useDirtyGuard(isDirty: Ref<boolean>): void {
  watch(isDirty, setDirty, { immediate: true })
  onUnmounted(() => setDirty(false))
}
