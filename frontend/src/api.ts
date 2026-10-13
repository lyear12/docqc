/**
 * HTTP and SSE access to the docqc server.
 *
 * Two things shape this module:
 *
 * 1. **The base URL is configurable.** In development the Vite dev server runs
 *    on one port and the API on another, so requests go cross-origin. In
 *    production the built assets are served by the API itself, so the same code
 *    must work with a relative prefix. `VITE_API_BASE` covers the first case and
 *    defaults to empty for the second.
 *
 * 2. **SSE is used through `EventSource`, which cannot send custom headers.**
 *    That is fine here — the endpoint takes the job id in the path and needs no
 *    auth — but it is why the upload is a separate `fetch` rather than being
 *    folded into the stream.
 */

import type {
  BackendInfo,
  CheckerInfo,
  JobCreated,
  JobStatus,
  ProgressEvent,
} from './types'

/** Base URL for API calls. Empty means same-origin. */
const BASE = import.meta.env.VITE_API_BASE ?? ''

/** A failed request, carrying the server's message when it sent one. */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * Read the server's error message out of a failed response.
 *
 * FastAPI reports validation failures as `{detail: ...}` and our own handlers
 * use the same shape, but `detail` can be a string or a list of field errors —
 * both are common enough to handle rather than stringify blindly.
 */
async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const detail = body?.detail ?? body?.error
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => item?.msg ?? JSON.stringify(item))
        .join('; ')
    }
    return response.statusText || `HTTP ${response.status}`
  } catch {
    return response.statusText || `HTTP ${response.status}`
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init)
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status)
  }
  return (await response.json()) as T
}

/** Server liveness and version. */
export function health(): Promise<{ status: string; tool: string; version: string }> {
  return request('/api/health')
}

/** Every registered checker. */
export function listCheckers(): Promise<CheckerInfo[]> {
  return request('/api/checkers')
}

/** Rendering backends available on the server's machine. */
export function listBackends(): Promise<BackendInfo[]> {
  return request('/api/backends')
}

/**
 * Upload a document and start a run.
 *
 * @param file Document to check.
 * @param checkers Names to run; empty means every checker.
 * @param onProgress Called as the upload body is sent, with a 0-1 fraction.
 *   Upload is instant on localhost, but a large document on a slow link is not,
 *   and a spinner with no progress looks frozen.
 */
export function createJob(
  file: File,
  checkers: string[],
  onProgress?: (fraction: number) => void,
): Promise<JobCreated> {
  const form = new FormData()
  form.append('file', file)
  if (checkers.length > 0) {
    form.append('checkers', checkers.join(','))
  }

  // XMLHttpRequest rather than fetch: `fetch` still cannot report upload
  // progress in a form the browsers we target agree on.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}/api/jobs`)

    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total)
      }
    })

    xhr.addEventListener('load', () => {
      let body: unknown
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        reject(new ApiError('服务器返回了无法解析的响应', xhr.status))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as JobCreated)
        return
      }
      const detail = (body as { detail?: string })?.detail
      reject(new ApiError(detail ?? `HTTP ${xhr.status}`, xhr.status))
    })

    xhr.addEventListener('error', () => {
      reject(new ApiError('无法连接到 docqc 服务，请确认 `docqc serve` 正在运行', 0))
    })

    xhr.addEventListener('abort', () => {
      reject(new ApiError('上传已取消', 0))
    })

    xhr.send(form)
  })
}

/** Poll a job's state. */
export function getJob(jobId: string): Promise<JobStatus> {
  return request(`/api/jobs/${jobId}`)
}

/** Delete a job and its upload. */
export function deleteJob(jobId: string): Promise<{ status: string; job_id: string }> {
  return request(`/api/jobs/${jobId}`, { method: 'DELETE' })
}

/**
 * Subscribe to a job's progress.
 *
 * Returns a function that closes the stream. The caller must invoke it —
 * leaving an `EventSource` open keeps the browser's connection pool busy and,
 * with HTTP/1.1's six-connection limit, would eventually stall other requests.
 *
 * A malformed frame is dropped rather than throwing: one bad line should not
 * tear down a run that is otherwise reporting fine.
 *
 * @param jobId Job to follow.
 * @param onEvent Called for each parsed event.
 * @param onError Called when the stream fails before a terminal event.
 */
export function streamJob(
  jobId: string,
  onEvent: (event: ProgressEvent) => void,
  onError: (message: string) => void,
): () => void {
  const source = new EventSource(`${BASE}/api/jobs/${jobId}/events`)
  let settled = false
  let closed = false

  const close = () => {
    if (!closed) {
      closed = true
      source.close()
    }
  }

  source.onmessage = (message) => {
    let payload: ProgressEvent
    try {
      payload = JSON.parse(message.data) as ProgressEvent
    } catch {
      return
    }
    if (payload.kind === 'finished' || payload.kind === 'failed') {
      settled = true
      close()
    }
    onEvent(payload)
  }

  source.onerror = () => {
    // EventSource auto-reconnects, which is wrong once the run is over: a
    // terminal event already closed the stream, so a later error is noise.
    if (settled) return
    close()
    onError('与服务器的连接中断')
  }

  return close
}
