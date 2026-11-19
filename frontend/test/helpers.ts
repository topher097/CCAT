import { vi } from 'vitest'

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
