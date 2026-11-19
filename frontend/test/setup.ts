import { afterEach, vi } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'

import { setDirty } from '../src/composables/useDirtyGuard'

enableAutoUnmount(afterEach)

afterEach(() => {
  setDirty(false)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
