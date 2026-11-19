/**
 * Thin typed same-origin fetch wrapper (refactor §7).
 *
 * Types come only from the generated OpenAPI schema (`schema.d.ts`).
 * Mutating requests attach the session CSRF token; a 401 invokes the
 * configured redirect hook. Errors are always `ApiError` carrying the
 * server's `{code, message, ...context}` detail.
 */

import type { components } from './schema'

export type Schemas = components['schemas']
export type JsonObject = Record<string, unknown>

export type SessionHooks = {
  /** Current session CSRF token, or null before login. */
  csrfToken: () => string | null
  /** Called on any 401 response (redirect to /login). */
  onUnauthorized: () => void
}

let hooks: SessionHooks = {
  csrfToken: () => null,
  onUnauthorized: () => {},
}

/** Install the session-aware hooks (called once from main.ts). */
export function configureClient(next: Partial<SessionHooks>): void {
  hooks = { ...hooks, ...next }
}

/** One API failure with the stable server code plus extra context. */
export class ApiError extends Error {
  /** HTTP status (0 for network failures). */
  readonly status: number
  /** Stable machine-readable code (`stale_version`, `forbidden`, ...). */
  readonly code: string
  /** Extra server detail fields (e.g. the 412 fresh row). */
  readonly context: JsonObject

  constructor(
    status: number,
    code: string,
    message: string,
    context: JsonObject = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.context = context
  }
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export type RequestOptions = {
  method?: string
  body?: unknown
  headers?: Record<string, string>
}

function errorFrom(status: number, body: unknown): ApiError {
  const detail =
    typeof body === 'object' && body !== null
      ? (body as { detail?: unknown }).detail
      : undefined
  if (typeof detail === 'object' && detail !== null) {
    const { code, message, ...context } = detail as {
      code?: unknown
      message?: unknown
    } & JsonObject
    return new ApiError(
      status,
      typeof code === 'string' ? code : 'http_error',
      typeof message === 'string' ? message : `HTTP ${status}`,
      context,
    )
  }
  return new ApiError(status, 'http_error', `HTTP ${status}`)
}

/** Perform one request and decode JSON/204 responses. */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers: Record<string, string> = { ...options.headers }
  if (!SAFE_METHODS.has(method)) {
    const token = hooks.csrfToken()
    if (token) headers['X-CCAT-CSRF'] = token
  }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new ApiError(0, 'network_error', 'network unavailable')
  }

  if (response.status === 401) hooks.onUnauthorized()
  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = null
    }
    throw errorFrom(response.status, body)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  get<T>(path: string): Promise<T> {
    return request<T>(path)
  },
  post<T>(path: string, body?: unknown): Promise<T> {
    return request<T>(path, { method: 'POST', body })
  },
  patch<T>(path: string, body: unknown, headers?: Record<string, string>) {
    return request<T>(path, { method: 'PATCH', body, headers })
  },
}
