import { vi } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import type { Component } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

import SettingsLayout from '../src/views/settings/SettingsLayout.vue'
import SettingsIndexView from '../src/views/settings/SettingsIndexView.vue'
import SettingsSectionView from '../src/views/settings/SettingsSectionView.vue'

/** Minimal fetch Response surface used by the client. */
export function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

export type RouteResult = Response | undefined

/**
 * Install a fetch mock that hands each request to the first handler
 * returning a response; unmatched requests 404.
 */
export function mockRoutes(
  handlers: Array<
    (url: string, init: RequestInit) => RouteResult | Promise<RouteResult>
  >,
) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      for (const handler of handlers) {
        const response = await handler(url, init ?? {})
        if (response) return response
      }
      return jsonResponse(404, {
        detail: { code: 'not_found', message: `unmocked ${url}` },
      })
    },
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Decode one recorded request body. */
export function bodyOf(call: unknown[]): Record<string, unknown> {
  const init = call[1] as RequestInit
  return init.body ? JSON.parse(String(init.body)) : {}
}

/** Decode one recorded request's headers. */
export function headersOf(call: unknown[]): Record<string, string> {
  return ((call[1] as RequestInit).headers ?? {}) as Record<string, string>
}

type MountOptions = {
  path?: string
  props?: Record<string, unknown>
  /** Runs after Pinia activation, before the component mounts. */
  setup?: () => void
}

const placeholder = { template: '<div />' }

/** Mount one view with a real memory router (route params work). */
export async function mountRouted(
  component: Component,
  options: MountOptions = {},
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: placeholder },
      { path: '/jobs', component: placeholder },
      { path: '/jobs/:id', component: placeholder },
      { path: '/admin', component: placeholder },
      { path: '/clarify', component: placeholder },
      { path: '/clarify/:familyId', component: placeholder },
      { path: '/match', component: placeholder },
      { path: '/generate', component: placeholder },
      { path: '/stats', component: placeholder },
      { path: '/screen', component: placeholder },
      { path: '/acquire', component: placeholder },
      { path: '/audit', component: placeholder },
      { path: '/audit/duplications', component: placeholder },
      { path: '/audit/duplications/:id', component: placeholder },
      { path: '/audit/sponsors/:id', component: placeholder },
      { path: '/audit/families/:id', component: placeholder },
      { path: '/audit/children/:id', component: placeholder },
      { path: '/audit/families/:familyId/children/:childId', component: placeholder },
      { path: '/audit/:rest(.*)*', component: placeholder },
      {
        path: '/settings',
        component: SettingsLayout,
        children: [
          { path: '', name: 'settings', component: SettingsIndexView },
          {
            path: ':section',
            name: 'settings-section',
            component: SettingsSectionView,
          },
        ],
      },
    ],
  })
  await router.push(options.path ?? '/')
  await router.isReady()
  options.setup?.()
  const wrapper = mount(component, {
    props: options.props,
    global: {
      plugins: [pinia, router],
      stubs: { RouterLink: RouterLinkStub },
    },
  })
  return { wrapper, router }
}
