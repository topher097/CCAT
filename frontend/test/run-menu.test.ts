import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { VueWrapper } from '@vue/test-utils'

import RunMenu from '../src/components/RunMenu.vue'
import { useJobPopup } from '../src/composables/useJobPopup'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const JOB = {
  id: 12,
  job_type: 'match',
  status: 'queued',
  requester: 'coord',
  attempt: 0,
  cancel_requested: false,
}

const MATCH_SETTINGS = {
  command: 'match',
  run: { test: false, skip_match: false },
  config_path: 'config/ccat_2026.json',
  editable: true,
  saved: false,
}

const GENERATE_SETTINGS = {
  command: 'generate',
  run: { test: false, overwrite: false, max_workers: 4 },
  config_path: 'config/ccat_2026.json',
  editable: true,
  saved: false,
}

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

function isSettings(
  url: string,
  method: string,
  command: 'match' | 'generate',
): boolean {
  return url.endsWith(`/runs/${command}/settings`) && method === 'GET'
}

async function mountMenu(
  command: 'match' | 'generate',
  props: Record<string, unknown> = {},
) {
  return mountRouted(RunMenu, {
    path: '/match',
    props: { command, ...props },
    setup: withSeason,
  })
}

async function openMenu(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('[data-testid="run-trigger"]').trigger('click')
  await flushPromises()
}

async function confirmRun(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('[data-testid="run-confirm"]').trigger('click')
  await wrapper.get('[data-testid="confirm"]').trigger('click')
  await flushPromises()
}

function jobsCall(fetchMock: ReturnType<typeof mockRoutes>) {
  return fetchMock.mock.calls.find(
    (entry) =>
      String(entry[0]).endsWith('/seasons/2026/jobs') &&
      String((entry[1] as RequestInit).method) === 'POST',
  )
}

beforeEach(() => {
  setActivePinia(createPinia())
  useJobPopup().closeJob()
  expect(useJobPopup().jobId.value).toBe(null)
})

describe('run menu', () => {
  it('opens from the button and is seeded from the stored settings', async () => {
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, {
              ...MATCH_SETTINGS,
              run: { test: true, skip_match: false },
            })
          : undefined,
    ])
    const { wrapper } = await mountMenu('match')

    expect(wrapper.get('button[aria-haspopup="menu"]').attributes('aria-expanded')).toBe(
      'false',
    )
    await openMenu(wrapper)

    expect(wrapper.get('button[aria-haspopup="menu"]').attributes('aria-expanded')).toBe(
      'true',
    )
    expect(wrapper.get('[data-testid="run-menu"]').attributes('role')).toBe('menu')
    expect(
      (wrapper.get('input[name="test"]').element as HTMLInputElement).checked,
    ).toBe(true)
    expect(
      (wrapper.get('input[name="skip_match"]').element as HTMLInputElement).checked,
    ).toBe(false)
  })

  it('patches the settings before enqueueing the edited values', async () => {
    const fetchMock = mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : url.endsWith('/runs/match/settings') && init.method === 'PATCH'
            ? jsonResponse(200, { ...MATCH_SETTINGS, saved: true })
            : url.endsWith('/seasons/2026/jobs')
              ? jsonResponse(202, JOB)
              : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)
    await wrapper.get('input[name="test"]').setValue(true)
    await confirmRun(wrapper)

    const patchIndex = fetchMock.mock.calls.findIndex(
      (entry) =>
        String(entry[0]).endsWith('/runs/match/settings') &&
        (entry[1] as RequestInit).method === 'PATCH',
    )
    const enqueueIndex = fetchMock.mock.calls.findIndex((entry) =>
      String(entry[0]).endsWith('/seasons/2026/jobs'),
    )
    expect(patchIndex).toBeGreaterThanOrEqual(0)
    expect(enqueueIndex).toBeGreaterThan(patchIndex)

    expect(bodyOf(fetchMock.mock.calls[patchIndex]!)).toEqual({
      test: true,
      skip_match: false,
    })
    expect(bodyOf(fetchMock.mock.calls[enqueueIndex]!)).toEqual({
      job_type: 'match',
      options: { test: true, skip_match: false },
      idempotency_key: expect.any(String),
    })
  })

  it('skips the patch when remembering is off', async () => {
    const fetchMock = mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : url.endsWith('/seasons/2026/jobs')
            ? jsonResponse(202, JOB)
            : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)
    await wrapper.get('input[name="remember"]').setValue(false)
    await wrapper.get('input[name="test"]').setValue(true)
    await confirmRun(wrapper)

    expect(
      fetchMock.mock.calls.some(
        (entry) =>
          String(entry[0]).endsWith('/runs/match/settings') &&
          (entry[1] as RequestInit).method === 'PATCH',
      ),
    ).toBe(false)
    expect(bodyOf(jobsCall(fetchMock)!)).toMatchObject({
      job_type: 'match',
      options: { test: true, skip_match: false },
    })
  })

  it('aborts the run when the settings write fails', async () => {
    const fetchMock = mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : url.endsWith('/runs/match/settings') && init.method === 'PATCH'
            ? jsonResponse(422, {
                detail: {
                  code: 'invalid_config',
                  message: 'invalid configuration',
                },
              })
            : url.endsWith('/seasons/2026/jobs')
              ? jsonResponse(202, JOB)
              : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)
    await confirmRun(wrapper)

    expect(jobsCall(fetchMock)).toBeUndefined()
    expect(wrapper.get('[data-testid="run-error"]').text()).toContain(
      'invalid configuration',
    )
  })

  it('disables test mode while skip matching is on', async () => {
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)

    expect(wrapper.get('input[name="test"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('input[name="test"]').setValue(true)
    await wrapper.get('input[name="skip_match"]').setValue(true)

    expect(wrapper.get('input[name="test"]').attributes('disabled')).toBeDefined()
    expect(
      (wrapper.get('input[name="test"]').element as HTMLInputElement).checked,
    ).toBe(false)
  })

  it('offers the match command exactly two settings', async () => {
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)

    const names = wrapper
      .get('[data-testid="run-menu"]')
      .findAll('input')
      .map((input) => input.attributes('name'))
      .sort()
    expect(names).toEqual(['remember', 'skip_match', 'test'])
    expect(wrapper.find('input[name="overwrite"]').exists()).toBe(false)
  })

  it('offers overwrite and workers for generate, and never saves the sponsor', async () => {
    const fetchMock = mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'generate')
          ? jsonResponse(200, GENERATE_SETTINGS)
          : url.endsWith('/runs/generate/settings') && init.method === 'PATCH'
            ? jsonResponse(200, { ...GENERATE_SETTINGS, saved: true })
            : url.endsWith('/seasons/2026/jobs')
              ? jsonResponse(202, { ...JOB, job_type: 'generate' })
              : undefined,
    ])
    const { wrapper } = await mountMenu('generate', { sponsor: 42 })
    await openMenu(wrapper)

    expect(wrapper.find('input[name="overwrite"]').exists()).toBe(true)
    expect(wrapper.find('input[name="max_workers"]').exists()).toBe(true)
    expect(
      (wrapper.get('input[name="sponsor"]').element as HTMLInputElement).value,
    ).toBe('42')

    await wrapper.get('input[name="overwrite"]').setValue(true)
    await wrapper.get('input[name="max_workers"]').setValue('2')
    await confirmRun(wrapper)

    const patchCall = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/runs/generate/settings') &&
        (entry[1] as RequestInit).method === 'PATCH',
    )
    expect(bodyOf(patchCall!)).toEqual({
      test: false,
      overwrite: true,
      max_workers: 2,
    })
    expect(bodyOf(patchCall!)).not.toHaveProperty('sponsor')

    expect(bodyOf(jobsCall(fetchMock)!)).toMatchObject({
      job_type: 'generate',
      options: { test: false, overwrite: true, max_workers: 2, sponsor: 42 },
    })
  })

  it('opens the blocking job popup from a 409 conflict', async () => {
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : url.endsWith('/runs/match/settings') && init.method === 'PATCH'
            ? jsonResponse(200, { ...MATCH_SETTINGS, saved: true })
            : url.endsWith('/seasons/2026/jobs')
              ? jsonResponse(409, {
                  detail: {
                    code: 'conflict',
                    message:
                      "season '2026' already has an active match job #7 (status running)",
                  },
                })
              : undefined,
    ])
    const { wrapper, router } = await mountMenu('match')
    await openMenu(wrapper)
    await confirmRun(wrapper)

    expect(wrapper.get('[data-testid="run-error"]').text()).toContain('#7')
    await wrapper.get('[data-testid="conflict-job-link"]').trigger('click')
    expect(useJobPopup().jobId.value).toBe(7)
    expect(router.currentRoute.value.path).toBe('/match')
  })

  it('closes on Esc and returns focus to the trigger', async () => {
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, MATCH_SETTINGS)
          : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)

    const trigger = wrapper.get('[data-testid="run-trigger"]')
    const focus = vi.spyOn(trigger.element as HTMLElement, 'focus')
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    )
    await flushPromises()

    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-testid="run-menu"]').exists()).toBe(false)
    expect(focus).toHaveBeenCalled()
  })

  it('renders read-only for a role that cannot change the settings', async () => {
    const settings = {
      ...MATCH_SETTINGS,
      run: { test: true, skip_match: false },
      editable: false,
    }
    mockRoutes([
      (url, init) =>
        isSettings(url, init.method ?? 'GET', 'match')
          ? jsonResponse(200, settings)
          : undefined,
    ])
    const { wrapper } = await mountMenu('match')
    await openMenu(wrapper)

    expect(wrapper.get('[data-testid="read-only"]').text()).toContain(
      'coordinator or admin',
    )
    expect(wrapper.find('[data-testid="run-confirm"]').exists()).toBe(false)
    for (const input of wrapper.get('[data-testid="run-menu"]').findAll('input')) {
      expect(input.attributes('disabled')).toBeDefined()
    }
  })
})
