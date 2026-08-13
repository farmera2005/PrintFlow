export class ApiError extends Error {
  status: number
  provider?: string

  constructor(status: number, message: string, provider?: string) {
    super(message)
    this.status = status
    this.provider = provider
  }
}

/** Turn a non-JSON error body into one readable line.
 *
 * When a reverse proxy in front of PrintFlow answers instead of PrintFlow —
 * Cloudflare, nginx, a tunnel — the body is a full HTML error page. Dumping it
 * into an alert buries the one useful fact under a screenful of markup, and
 * the page names PrintFlow's own hostname, which sends the operator looking at
 * the wrong service entirely.
 */
export function summariseErrorBody(text: string, status: number): string {
  const trimmed = text.trim()
  const isHtml = /^(<!doctype|<html)/i.test(trimmed)
  if (!isHtml) return trimmed.length > 400 ? `${trimmed.slice(0, 400)}…` : trimmed

  const title = /<title>([^<]*)<\/title>/i.exec(trimmed)?.[1]?.trim()
  const gateway = status === 502 || status === 504
  const what = title ? `“${title}”` : `an HTML error page (HTTP ${status})`
  if (!gateway) return `The server returned ${what} instead of a response.`
  return (
    `${what} — this came from the proxy in front of PrintFlow, not from ` +
    `PrintFlow itself. PrintFlow took too long to answer or is not running, ` +
    `so the request never reached the service you were configuring.`
  )
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: summariseErrorBody(text, response.status) }
    }
  }

  if (!response.ok) {
    const detail =
      typeof payload?.detail === 'string'
        ? payload.detail
        : Array.isArray(payload?.detail)
          ? payload.detail.map((d: any) => d.msg ?? String(d)).join(', ')
          : `Request failed (${response.status})`
    throw new ApiError(response.status, detail, payload?.provider)
  }
  return payload as T
}

/** A multipart POST, for the one thing that is a file rather than JSON.
 *
 *  The Content-Type is deliberately not set: the browser has to add the
 *  multipart boundary itself, and naming the type by hand omits it and leaves
 *  the server unable to find any of the parts. */
async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    body: form,
  })
  const text = await response.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: summariseErrorBody(text, response.status) }
    }
  }
  if (!response.ok) {
    const detail =
      typeof payload?.detail === 'string'
        ? payload.detail
        : `Request failed (${response.status})`
    throw new ApiError(response.status, detail)
  }
  return payload as T
}

/** A POST whose reply is a file to save, not JSON.
 *
 *  A POST rather than a link because it carries a passphrase, and a passphrase
 *  in a query string lands in the browser's history and every proxy log
 *  between here and the shop. The filename comes from the server so the saved
 *  file is dated by the machine that made it. */
async function requestBlob(
  path: string,
  body: unknown,
): Promise<{ body: Blob; filename: string }> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    const text = await response.text()
    let detail = `Request failed (${response.status})`
    try {
      detail = JSON.parse(text)?.detail ?? detail
    } catch {
      detail = summariseErrorBody(text, response.status)
    }
    throw new ApiError(response.status, detail)
  }
  const disposition = response.headers.get('content-disposition') ?? ''
  const named = /filename="?([^"]+)"?/.exec(disposition)?.[1]
  return { body: await response.blob(), filename: named || 'printflow-backup.tar.gz' }
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body ?? {}),
  del: <T>(path: string) => request<T>('DELETE', path),
  postForm: <T>(path: string, form: FormData) => requestForm<T>(path, form),
  postBlob: (path: string, body?: unknown) => requestBlob(path, body),
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}
