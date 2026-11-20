/**
 * Settings state: one loaded configuration tree, edited in place and
 * saved back to the JSON file the server owns.
 *
 * The whole tree is one draft shared by every section, so a change
 * made in one section is still there after moving to another one, and
 * one Save writes the complete file.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '../api/client'
import type { JsonObject } from '../api/client'
import { getConfig, saveConfig } from '../api/config'
import { getAt, removeAt, renameAt, setAt } from '../settings/tree'
import type { Path } from '../settings/tree'

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value))
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== typeof b) return false
  if (a === null || b === null) return a === b
  if (typeof a !== 'object') return false

  const aIsArray = Array.isArray(a)
  const bIsArray = Array.isArray(b)
  if (aIsArray !== bIsArray) return false

  if (aIsArray) {
    const left = a as unknown[]
    const right = b as unknown[]
    if (left.length !== right.length) return false
    return left.every((value, index) => deepEqual(value, right[index]))
  }

  const left = a as Record<string, unknown>
  const right = b as Record<string, unknown>
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(right)
  if (leftKeys.length !== rightKeys.length) return false
  return leftKeys.every((key) => deepEqual(left[key], right[key]))
}

/** Decode the server's list of {path, message} contract violations. */
function extractErrors(list: unknown): Record<string, string> {
  if (!Array.isArray(list)) return {}
  const map: Record<string, string> = {}
  for (const item of list) {
    if (
      item &&
      typeof item === 'object' &&
      'path' in item &&
      'message' in item &&
      typeof item.path === 'string' &&
      typeof item.message === 'string'
    ) {
      map[item.path] = item.message
    }
  }
  return map
}

export const useSettingsStore = defineStore('settings', () => {
  /** Path of the configuration file the server owns. */
  const filePath = ref<string | null>(null)
  /** The tree as last loaded or saved. */
  const saved = ref<JsonObject | null>(null)
  /** The tree being edited. */
  const draft = ref<JsonObject | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)
  /** Contract violations from the last save, keyed by dotted path. */
  const fieldErrors = ref<Record<string, string>>({})
  /** When the tree was last saved. */
  const savedAt = ref<Date | null>(null)

  const dirty = computed(() => !deepEqual(saved.value, draft.value))

  /** Load the configuration tree once per session. */
  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    fieldErrors.value = {}
    try {
      const response = await getConfig()
      filePath.value = response.path
      saved.value = clone(response.config)
      draft.value = clone(response.config)
    } catch (failure) {
      error.value =
        failure instanceof ApiError ? failure.message : 'failed to load config'
    } finally {
      loading.value = false
    }
  }

  /** Validate and write the whole tree back to the file. */
  async function save(): Promise<void> {
    if (!draft.value) return
    saving.value = true
    error.value = null
    fieldErrors.value = {}
    try {
      const response = await saveConfig(draft.value)
      filePath.value = response.path
      saved.value = clone(response.config)
      draft.value = clone(response.config)
      savedAt.value = new Date()
    } catch (failure) {
      if (failure instanceof ApiError) {
        error.value = failure.message
        fieldErrors.value = extractErrors(failure.context.errors)
      } else {
        error.value = 'failed to save config'
      }
    } finally {
      saving.value = false
    }
  }

  /** Throw away unsaved edits. */
  function discard(): void {
    draft.value = saved.value ? clone(saved.value) : null
    error.value = null
    fieldErrors.value = {}
  }

  /** Write one node of the draft. */
  function setValue(path: Path, value: unknown): void {
    if (!draft.value) return
    setAt(draft.value, path, value)
  }

  /** Remove one node of the draft. */
  function removeNode(path: Path): void {
    if (!draft.value) return
    removeAt(draft.value, path)
  }

  /** Rename one object key of the draft. */
  function renameKey(path: Path, nextKey: string): void {
    if (!draft.value) return
    renameAt(draft.value, path, nextKey)
  }

  /** Add one entry to a map of the draft. */
  function addEntry(path: Path, key: string, value: unknown): void {
    if (!draft.value) return
    setAt(draft.value, [...path, key], value)
  }

  /** Append one item to a list of the draft. */
  function appendItem(path: Path, value: unknown): void {
    if (!draft.value) return
    const list = getAt(draft.value, path)
    if (Array.isArray(list)) list.push(value)
  }

  /**
   * The contract violation reported for one node, if any.
   *
   * The server names a violation by its dotted path; a violation on a
   * whole map entry also covers the fields inside it.
   */
  function errorAt(path: Path): string | undefined {
    const dotted = path.join('.')
    const exact = fieldErrors.value[dotted]
    if (exact) return exact
    const prefix = `${dotted}.`
    const covered = Object.keys(fieldErrors.value).find((key) =>
      key.startsWith(prefix),
    )
    return covered ? fieldErrors.value[covered] : undefined
  }

  return {
    filePath,
    saved,
    draft,
    loading,
    saving,
    error,
    fieldErrors,
    savedAt,
    dirty,
    load,
    save,
    discard,
    setValue,
    removeNode,
    renameKey,
    addEntry,
    appendItem,
    errorAt,
  }
})
