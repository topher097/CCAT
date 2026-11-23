import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import type {
  DispatchChildListView,
  DispatchDraft,
  DispatchListView,
  DispatchRow,
} from '../src/api/dispatch'
import DispatchView from '../src/views/DispatchView.vue'
import { useSessionStore } from '../src/stores/session'
import {
  bodyOf,
  headersOf,
  jsonResponse,
  mockRoutes,
  mountRouted,
} from './helpers'

const JOB = {
  id: 11,
  job_type: 'generate',
  status: 'queued',
  requester: 'admin',
  attempt: 0,
  cancel_requested: false,
}

function withSeason(admin = true): void {
  const session = useSessionStore()
  session.season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
  session.me = {
    username: 'coordinator',
    roles: [admin ? 'admin' : 'coordinator'],
    csrf_token: 'csrf',
  }
}

function sponsor(overrides: Partial<DispatchRow> = {}): DispatchRow {
  return {
    sponsor_id: 7,
    display_name: 'Ada Lovelace',
    first_name: 'Ada',
    last_name: 'Lovelace',
    organization_name: 'Analytical Engines',
    sponsor_type: 'Business',
    email: 'ada@example.org',
    physical_packet: false,
    num_children: 2,
    assigned_count: 2,
    assigned_angel_ids: ['1A', '1B'],
    sheets_ready_count: 2,
    sheets_total: 2,
    missing_sheet_angel_ids: [],
    packet_status: 'ready',
    packet_pdf_artifact_id: 90,
    packet_created_at: '2026-11-20T10:00:00Z',
    instruction_pdf_artifact_id: 91,
    attachments: [],
    email_status: 'draft_ready',
    email_sent_at: null,
    email_error: null,
    draft_version: 1,
    draft_subject: 'Your sponsor packet',
    packets_dispatched: false,
    packets_dispatched_at: null,
    sponsor_row_version: 3,
    duplicate_recipient_sponsor_ids: [],
    blockers: [],
    ...overrides,
  }
}

function listView(rows: DispatchRow[] = [sponsor()]): DispatchListView {
  return {
    season: '2026',
    mail_configured: true,
    can_send: true,
    totals: {
      sponsors: rows.length,
      assigned_children: 2,
      sheets_ready: 2,
      sheets_missing: 1,
      packets_ready: 1,
      packets_missing: 1,
      dispatched: 0,
    },
    rows,
  }
}

function childrenView(): DispatchChildListView {
  return {
    rows: [
      {
        zite_record_id: 'child-1',
        angel_id: '1A',
        family_id: 1,
        sponsor_id: 7,
        generated_info_sheet: true,
        sheet_artifact_id: 88,
      },
    ],
  }
}

function draft(overrides: Partial<DispatchDraft> = {}): DispatchDraft {
  return {
    sponsor_id: 7,
    version: 1,
    subject: 'Your sponsor packet',
    body_html: '<html><body><strong>Thank you</strong></body></html>',
    note: '',
    to_email: 'ada@example.org',
    from_email: 'angeltree@example.org',
    bcc_emails: ['archive@example.org'],
    attachments: [
      {
        artifact_id: 90,
        role: 'packet_pdf',
        filename: 'ada-packet.pdf',
        content_type: 'application/pdf',
        size_bytes: 4096,
        sha256: 'abc',
      },
    ],
    build_id: 'build-1',
    email_status: 'draft_ready',
    email_error: null,
    blockers: [],
    ...overrides,
  }
}

function sendResult(state: 'sent' | 'failed' | 'unknown') {
  return {
    sponsor_id: 7,
    state,
    attempt_id: 'attempt-1',
    message_id: state === 'sent' ? 'message-1' : '',
    draft_version: 2,
    sent_at: state === 'sent' ? '2026-11-22T10:00:00Z' : null,
    error: state === 'failed' ? 'Mailbox refused the recipient' : null,
  }
}

function baseRoutes(
  list: DispatchListView = listView(),
  loadedDraft: DispatchDraft = draft(),
  children: DispatchChildListView | null = childrenView(),
) {
  return [
    (url: string, init: RequestInit) =>
      children &&
      url.endsWith('/seasons/2026/dispatch/children') &&
      (init.method ?? 'GET') === 'GET'
        ? jsonResponse(200, children)
        : undefined,
    (url: string, init: RequestInit) =>
      url.endsWith('/seasons/2026/dispatch') && (init.method ?? 'GET') === 'GET'
        ? jsonResponse(200, list)
        : undefined,
    (url: string, init: RequestInit) =>
      url.endsWith('/seasons/2026/jobs') && (init.method ?? 'GET') === 'GET'
        ? jsonResponse(200, [])
        : undefined,
    (url: string, init: RequestInit) =>
      url.endsWith('/sponsors/7/draft') && (init.method ?? 'GET') === 'GET'
        ? jsonResponse(200, loadedDraft)
        : undefined,
  ]
}

async function mountDispatch(
  admin = true,
  list = listView(),
  handlers: Parameters<typeof mockRoutes>[0] = baseRoutes(list),
) {
  const fetchMock = mockRoutes(handlers)
  const mounted = await mountRouted(DispatchView, {
    path: '/dispatch',
    setup: () => withSeason(admin),
  })
  await flushPromises()
  return { ...mounted, fetchMock }
}

async function openDraft(wrapper: Awaited<ReturnType<typeof mountDispatch>>['wrapper']) {
  await wrapper.get('[data-testid="draft-send"]').trigger('click')
  await flushPromises()
}

beforeEach(() => setActivePinia(createPinia()))

describe('dispatch work list and generation', () => {
  it('renders one row per sponsor and explains blockers in text', async () => {
    const rows = [
      sponsor(),
      sponsor({
        sponsor_id: 8,
        display_name: 'Grace Hopper',
        first_name: 'Grace',
        last_name: 'Hopper',
        email: null,
        assigned_count: 1,
        assigned_angel_ids: ['2A'],
        sheets_ready_count: 0,
        sheets_total: 1,
        missing_sheet_angel_ids: ['2A'],
        packet_status: 'missing',
        email_status: 'not_drafted',
        blockers: ['no_email', 'missing_sheets'],
      }),
    ]
    const { wrapper } = await mountDispatch(true, listView(rows))

    expect(wrapper.findAll('[data-testid="dispatch-row"]')).toHaveLength(2)
    const blockers = wrapper
      .get('[data-sponsor-id="8"]')
      .get('[data-testid="dispatch-blockers"]')
    expect(blockers.text()).toContain('No email on file')
    expect(blockers.text()).toContain('Child sheets missing')
    expect(wrapper.get('[data-sponsor-id="8"]').text()).toContain('Missing: 2A')
  })

  it('enqueues generate jobs with sheet, packet, and overwrite options', async () => {
    const fetchMock = mockRoutes([
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/seasons/2026/jobs') && init.method === 'POST'
          ? jsonResponse(202, JOB)
          : undefined,
    ])
    const { wrapper } = await mountRouted(DispatchView, {
      path: '/dispatch',
      setup: () => withSeason(),
    })
    await flushPromises()

    await wrapper.get('[data-testid="generate-sheets"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()
    await wrapper.get('input[name="overwrite"]').setValue(true)
    await wrapper.get('[data-testid="generate-both"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const posts = fetchMock.mock.calls.filter(
      (call) =>
        String(call[0]).endsWith('/seasons/2026/jobs') &&
        (call[1] as RequestInit).method === 'POST',
    )
    expect(bodyOf(posts[0]!)).toEqual({
      job_type: 'generate',
      options: { only: 'sheets', overwrite: false },
    })
    expect(bodyOf(posts[1]!)).toEqual({
      job_type: 'generate',
      options: { only: 'both', overwrite: true },
    })
  })

  it('enqueues a one-child sheet job from the collapsed controls', async () => {
    const fetchMock = mockRoutes([
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/seasons/2026/jobs') && init.method === 'POST'
          ? jsonResponse(202, JOB)
          : undefined,
    ])
    const { wrapper } = await mountRouted(DispatchView, {
      path: '/dispatch',
      setup: () => withSeason(),
    })
    await flushPromises()

    await wrapper.get('input[name="child_id"]').setValue('child-42')
    await wrapper.get('[data-testid="generate-child"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]).endsWith('/seasons/2026/jobs') &&
        (call[1] as RequestInit).method === 'POST',
    )
    expect(bodyOf(post!)).toEqual({
      job_type: 'generate',
      options: { child: 'child-42', only: 'sheets', overwrite: false },
    })
  })
})

describe('dispatch downloads', () => {
  it('offers sponsor and page ZIPs, child sheets, and packet PDF downloads', async () => {
    const { wrapper } = await mountDispatch()

    expect(wrapper.get('[data-testid="download-sheets-zip"]').attributes('href')).toBe(
      '/api/v1/seasons/2026/dispatch/sheets.zip?sponsor=7',
    )
    expect(
      wrapper.get('[data-testid="download-all-sheets-zip"]').attributes('href'),
    ).toBe('/api/v1/seasons/2026/dispatch/sheets.zip')
    expect(wrapper.get('[data-testid="download-sheet"]').attributes('href')).toBe(
      '/api/v1/artifacts/88/download',
    )
    expect(
      wrapper.get('[data-testid="download-packet-pdf"]').attributes('href'),
    ).toBe('/api/v1/artifacts/90/download')
  })

  it('hides sponsor ZIP and packet links when unavailable', async () => {
    const row = sponsor({
      sheets_ready_count: 0,
      sheets_total: 1,
      missing_sheet_angel_ids: ['1A'],
      packet_pdf_artifact_id: null,
    })
    const { wrapper } = await mountDispatch(true, listView([row]))

    expect(wrapper.find('[data-testid="download-sheets-zip"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="download-all-sheets-zip"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="download-packet-pdf"]').exists()).toBe(false)
  })

  it('keeps dispatch available when child sheets request returns 404', async () => {
    const { wrapper } = await mountDispatch(true, listView(), baseRoutes(listView(), draft(), null))

    expect(wrapper.find('[data-testid="dispatch-table"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="download-sheet"]').exists()).toBe(false)
  })
})

describe('sponsor email popup', () => {
  it('opens with the draft subject, recipient, attachments, and sandboxed body', async () => {
    const { wrapper } = await mountDispatch()
    await openDraft(wrapper)

    expect(
      (wrapper.get('[data-testid="draft-subject"]').element as HTMLInputElement)
        .value,
    ).toBe('Your sponsor packet')
    expect(wrapper.get('[data-testid="draft-recipient"]').text()).toBe(
      'ada@example.org',
    )
    expect(wrapper.get('[data-testid="draft-attachments"]').text()).toContain(
      'ada-packet.pdf',
    )
    expect(wrapper.get('[data-testid="draft-attachments"] a').attributes('href')).toBe(
      '/api/v1/artifacts/90/download',
    )
    const frame = wrapper.get('[data-testid="draft-body"]')
    expect(frame.attributes('sandbox')).toBe('')
    expect(frame.attributes('srcdoc')).toContain('<strong>Thank you</strong>')
  })

  it('saves subject edits with the draft version in If-Match', async () => {
    const { wrapper, fetchMock } = await mountDispatch(true, listView(), [
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/sponsors/7/draft') && init.method === 'PATCH'
          ? jsonResponse(200, draft({ subject: 'Edited subject', version: 2 }))
          : undefined,
    ])
    await openDraft(wrapper)
    await wrapper.get('[data-testid="draft-subject"]').setValue('Edited subject')
    await wrapper.get('[data-testid="save-draft"]').trigger('click')
    await flushPromises()

    const patch = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]).endsWith('/sponsors/7/draft') &&
        (call[1] as RequestInit).method === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(bodyOf(patch!)).toEqual({ subject: 'Edited subject', note: '' })
    expect(headersOf(patch!)['If-Match']).toBe('1')
  })

  it('sends the typed subject and note, then closes with a dispatched notice', async () => {
    const { wrapper, fetchMock } = await mountDispatch(true, listView(), [
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/sponsors/7/send') && init.method === 'POST'
          ? jsonResponse(200, sendResult('sent'))
          : undefined,
    ])
    await openDraft(wrapper)
    await wrapper.get('[data-testid="draft-subject"]').setValue('Packet ready now')
    await wrapper.get('[data-testid="draft-note"]').setValue('Call me with questions')
    await wrapper.get('[data-testid="send-email"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]).endsWith('/sponsors/7/send') &&
        (call[1] as RequestInit).method === 'POST',
    )
    expect(bodyOf(post!)).toEqual({
      version: 1,
      subject: 'Packet ready now',
      note: 'Call me with questions',
    })
    expect(wrapper.find('[data-testid="sponsor-email-modal"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="send-notice"]').text()).toBe(
      'Sent packet email to Ada Lovelace (ada@example.org). Sponsor #7 is now marked dispatched.',
    )
  })

  it('keeps a refused send open with its error and a Retry send button', async () => {
    mockRoutes([
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/sponsors/7/send') && init.method === 'POST'
          ? jsonResponse(200, sendResult('failed'))
          : undefined,
    ])
    const { wrapper } = await mountRouted(DispatchView, {
      path: '/dispatch',
      setup: () => withSeason(),
    })
    await flushPromises()
    await openDraft(wrapper)
    await wrapper.get('[data-testid="send-email"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="sponsor-email-modal"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="send-error"]').text()).toContain(
      'Mailbox refused the recipient',
    )
    expect(wrapper.get('[data-testid="send-email"]').text()).toBe('Retry send')
  })

  it('does not offer a retry after an unknown send outcome', async () => {
    mockRoutes([
      ...baseRoutes(),
      (url, init) =>
        url.endsWith('/sponsors/7/send') && init.method === 'POST'
          ? jsonResponse(200, sendResult('unknown'))
          : undefined,
    ])
    const { wrapper } = await mountRouted(DispatchView, {
      path: '/dispatch',
      setup: () => withSeason(),
    })
    await flushPromises()
    await openDraft(wrapper)
    await wrapper.get('[data-testid="send-email"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="send-error"]').text()).toBe(
      'We do not know whether this email was accepted. Check the sent mail before sending again.',
    )
    expect(wrapper.find('[data-testid="send-email"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Retry send')
  })

  it('omits the send action for a user who cannot send', async () => {
    const list = { ...listView(), can_send: false }
    const { wrapper } = await mountDispatch(false, list)

    expect(wrapper.find('[data-testid="draft-send"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="dispatch-row"]').text()).toContain(
      'Sending requires an administrator',
    )
  })
})
