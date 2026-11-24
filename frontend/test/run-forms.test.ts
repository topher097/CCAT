import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PipelineRunForm from '../src/components/PipelineRunForm.vue'
import { useJobPopup } from '../src/composables/useJobPopup'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const JOB = {
  id: 11,
  job_type: 'screen',
  status: 'queued',
  requester: 'lmboswell',
  attempt: 0,
  cancel_requested: false,
}

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

async function mountForm(command: 'acquire' | 'screen' | 'match' | 'generate' | 'stats') {
  return mountRouted(PipelineRunForm, {
    path: '/screen',
    props: { command },
    setup: withSeason,
  })
}

async function enqueueAndRead(fetchMock: ReturnType<typeof mockRoutes>) {
  const call = fetchMock.mock.calls.find(
    (entry) =>
      String(entry[0]).endsWith('/seasons/2026/jobs') &&
      String((entry[1] as RequestInit).method) === 'POST',
  )
  expect(call).toBeDefined()
  return bodyOf(call!)
}

beforeEach(() => {
  setActivePinia(createPinia())
  useJobPopup().closeJob()
  expect(useJobPopup().jobId.value).toBe(null)
})

describe('pipeline run forms', () => {
  it('maps screen options to the CLI payload exactly', async () => {
    const fetchMock = mockRoutes([
      (url) => (url.endsWith('/seasons/2026/jobs') ? jsonResponse(202, JOB) : undefined),
    ])
    const { wrapper } = await mountForm('screen')
    await wrapper.get('input[name="test"]').setValue(true)
    await wrapper.get('input[name="skip_email_check"]').setValue(true)
    await wrapper.get('input[name="redo"]').setValue('c9')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(await enqueueAndRead(fetchMock)).toEqual({
      job_type: 'screen',
      options: {
        test: true,
        overwrite: false,
        skip_email_check: true,
        skip_address_check: false,
        redo: 'c9',
      },
    })
  })

  it('maps generate options to the CLI payload exactly', async () => {
    const fetchMock = mockRoutes([
      (url) => (url.endsWith('/seasons/2026/jobs') ? jsonResponse(202, JOB) : undefined),
    ])
    const { wrapper } = await mountForm('generate')
    await wrapper.get('input[name="overwrite"]').setValue(true)
    await wrapper.get('input[name="sponsor"]').setValue('12')
    await wrapper.get('input[name="max_workers"]').setValue('3')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(await enqueueAndRead(fetchMock)).toEqual({
      job_type: 'generate',
      options: {
        test: false,
        overwrite: true,
        sponsor: 12,
        max_workers: 3,
      },
    })
  })

  it('states that --test still mutates persistent state', async () => {
    mockRoutes([])
    const { wrapper } = await mountForm('screen')
    await wrapper.get('input[name="test"]').setValue(true)
    expect(wrapper.get('[data-testid="test-warning"]').text()).toContain(
      'STILL mutates persistent state',
    )
  })

  it('warns that --overwrite re-does completed work', async () => {
    mockRoutes([])
    const { wrapper } = await mountForm('screen')
    await wrapper.get('input[name="overwrite"]').setValue(true)
    expect(wrapper.get('[data-testid="overwrite-warning"]').text()).toContain(
      're-does work that is already done',
    )
  })

  it('requires confirmation before enqueueing a destructive run', async () => {
    const fetchMock = mockRoutes([
      (url) => (url.endsWith('/seasons/2026/jobs') ? jsonResponse(202, JOB) : undefined),
    ])
    const { wrapper } = await mountForm('generate')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(fetchMock.mock.calls).toHaveLength(0)
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()
    expect(fetchMock.mock.calls).toHaveLength(1)
  })

  it('opens the active job popup from a 409 conflict', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/jobs')
          ? jsonResponse(409, {
              detail: {
                code: 'conflict',
                message:
                  "season '2026' already has an active screen job #7 (status running)",
              },
            })
          : undefined,
    ])
    const { wrapper, router } = await mountForm('screen')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="run-error"]').text()).toContain('#7')
    await wrapper.get('[data-testid="conflict-job-link"]').trigger('click')
    expect(useJobPopup().jobId.value).toBe(7)
    expect(router.currentRoute.value.path).toBe('/screen')
  })

  it('renders a 422 field error naming the invalid field', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/jobs')
          ? jsonResponse(422, {
              detail: {
                code: 'invalid_input',
                message: 'invalid options',
                errors: [
                  {
                    loc: ['body', 'options', 'redo'],
                    msg: 'unknown child',
                  },
                ],
              },
            })
          : undefined,
    ])
    const { wrapper } = await mountForm('screen')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const text = wrapper.get('[data-testid="run-error"]').text()
    expect(text).toContain('body.options.redo')
    expect(text).toContain('unknown child')
  })

  it('shows the global AI screening flag on the screen page', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/runs/screen/settings')
          ? jsonResponse(200, {
              command: 'screen',
              run: { overwrite_new: true },
              config_path: '/tmp/ccat.json',
              editable: true,
              saved: false,
            })
          : undefined,
    ])
    const { wrapper } = await mountForm('screen')
    await flushPromises()

    const box = wrapper.get('input[name="overwrite_new"]')
    expect((box.element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.get('[data-testid="overwrite-new-warning"]').text()).toContain(
      'status is still',
    )
  })

  it('writes the global AI screening flag to the configuration file', async () => {
    const fetchMock = mockRoutes([
      (url, init) =>
        url.endsWith('/runs/screen/settings') && init.method === 'PATCH'
          ? jsonResponse(200, {
              command: 'screen',
              run: { overwrite_new: true },
              config_path: '/tmp/ccat.json',
              editable: true,
              saved: true,
            })
          : url.endsWith('/runs/screen/settings')
            ? jsonResponse(200, {
                command: 'screen',
                run: { overwrite_new: false },
                config_path: '/tmp/ccat.json',
                editable: true,
                saved: false,
              })
            : undefined,
    ])
    const { wrapper } = await mountForm('screen')
    await flushPromises()

    await wrapper.get('input[name="overwrite_new"]').setValue(true)
    await flushPromises()

    const patch = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/runs/screen/settings') &&
        String((entry[1] as RequestInit).method) === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(bodyOf(patch!)).toEqual({ overwrite_new: true })
    expect(
      (wrapper.get('input[name="overwrite_new"]').element as HTMLInputElement)
        .checked,
    ).toBe(true)
  })

  it('leaves the global AI flag out of the job options', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.endsWith('/runs/screen/settings')
          ? jsonResponse(200, {
              command: 'screen',
              run: { overwrite_new: true },
              config_path: '/tmp/ccat.json',
              editable: true,
              saved: false,
            })
          : url.endsWith('/seasons/2026/jobs')
            ? jsonResponse(202, JOB)
            : undefined,
    ])
    const { wrapper } = await mountForm('screen')
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(await enqueueAndRead(fetchMock)).toEqual({
      job_type: 'screen',
      options: {
        test: false,
        overwrite: false,
        skip_email_check: false,
        skip_address_check: false,
      },
    })
  })
})
