import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useSettingsStore } from '../src/stores/settings'
import { bodyOf, jsonResponse, mockRoutes } from './helpers'

const CONFIG = {
  path: 'config/ccat_2026.json',
  config: {
    season: '2026',
    vocabularies: { sponsor_affiliation: ['Individual', 'Organization'] },
    tables: {
      sponsor: { columns: { email: { type: 'email' }, name: { type: 'name' } } },
    },
  },
}

function configRoutes(): ReturnType<typeof mockRoutes> {
  return mockRoutes([
    (url, init) =>
      url.endsWith('/api/v1/config') && init.method === 'PUT'
        ? jsonResponse(200, CONFIG)
        : undefined,
    (url, init) =>
      url.endsWith('/api/v1/config') && init.method === 'GET'
        ? jsonResponse(200, CONFIG)
        : undefined,
  ])
}

beforeEach(() => setActivePinia(createPinia()))

describe('settings store', () => {
  it('loads the configuration tree and starts clean', async () => {
    configRoutes()
    const store = useSettingsStore()

    await store.load()

    expect(store.filePath).toBe(CONFIG.path)
    expect(store.draft).toEqual(CONFIG.config)
    expect(store.dirty).toBe(false)
  })

  it('saves the whole edited tree and clears the dirty flag', async () => {
    const fetchMock = configRoutes()
    const store = useSettingsStore()
    await store.load()

    store.setValue(['season'], '2027')
    expect(store.dirty).toBe(true)

    await store.save()

    const call = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/api/v1/config') &&
        String((entry[1] as RequestInit).method) === 'PUT',
    )
    expect(bodyOf(call!)).toMatchObject({ season: '2027' })
    expect(store.dirty).toBe(false)
  })

  it('adds and removes list items', async () => {
    configRoutes()
    const store = useSettingsStore()
    await store.load()

    store.appendItem(['vocabularies', 'sponsor_affiliation'], 'Walmart')
    expect(store.draft).toMatchObject({
      vocabularies: { sponsor_affiliation: ['Individual', 'Organization', 'Walmart'] },
    })

    store.removeNode(['vocabularies', 'sponsor_affiliation', '0'])
    expect(store.draft).toMatchObject({
      vocabularies: { sponsor_affiliation: ['Organization', 'Walmart'] },
    })
  })

  it('adds and renames map entries without reordering the keys', async () => {
    configRoutes()
    const store = useSettingsStore()
    await store.load()

    store.addEntry(['vocabularies'], 'child_gender', ['Male'])
    store.renameKey(['vocabularies', 'child_gender'], 'child_sex')

    expect(Object.keys(store.draft!.vocabularies as object)).toEqual([
      'sponsor_affiliation',
      'child_sex',
    ])
  })

  it('reports contract violations by path', async () => {
    mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'PUT'
          ? jsonResponse(422, {
              detail: {
                code: 'invalid_config',
                message: 'Configuration is invalid',
                errors: [
                  { path: 'api.port', message: 'Input should be an integer' },
                ],
              },
            })
          : undefined,
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'GET'
          ? jsonResponse(200, CONFIG)
          : undefined,
    ])
    const store = useSettingsStore()
    await store.load()

    await store.save()

    expect(store.error).toBe('Configuration is invalid')
    expect(store.errorAt(['api', 'port'])).toBe('Input should be an integer')
    expect(store.errorAt(['api', 'host'])).toBeUndefined()
  })

  it('discards the edits back to the loaded tree', async () => {
    configRoutes()
    const store = useSettingsStore()
    await store.load()

    store.setValue(['season'], '2027')
    store.discard()

    expect(store.draft).toEqual(CONFIG.config)
    expect(store.dirty).toBe(false)
  })
})
