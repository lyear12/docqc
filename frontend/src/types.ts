/**
 * The wire contract, mirroring `docqc/models.py` and `docqc/web/schemas.py`.
 *
 * These are hand-written rather than generated. For a contract this small that
 * is the cheaper trade: a generator adds a build step and a tool to keep
 * working, while a handful of interfaces is something a reader can check against
 * the Python in one sitting. If the API ever grows past a screenful, generating
 * becomes worth it — the field names here are the same strings the server sends,
 * so `GET /api/jobs/{id}` shows any drift immediately.
 */

/** How much a finding matters. Mirrors the server's `Severity`. */
export type Severity = 'error' | 'warning' | 'info'

/** The outcome of one checker. Mirrors the server's `Status`. */
export type CheckStatus = 'pass' | 'fail' | 'warn' | 'skipped' | 'error'

/** Where in the document a finding lives. Every field is optional. */
export interface FindingLocation {
  page?: number | null
  section?: number | null
  paragraph?: number | null
  table?: number | null
  row?: number | null
  column?: number | null
  part?: string | null
  text?: string | null
}

/** One concrete problem, with the evidence needed to act on it. */
export interface Finding {
  checker: string
  code: string
  severity: Severity
  message: string
  location: FindingLocation
  evidence: Record<string, unknown>
  hint?: string | null
}

/** What one checker concluded. */
export interface CheckResult {
  checker: string
  status: CheckStatus
  findings: Finding[]
  duration_ms: number
  error?: string | null
}

/** Rollup across all checkers. */
export interface Summary {
  status: CheckStatus
  errors: number
  warnings: number
  infos: number
  checkers_run: number
  checkers_errored: number
}

/** What docqc learned about the input. */
export interface DocumentInfo {
  path: string
  name: string
  format: string
  size_bytes: number
  sha256: string
  page_count?: number | null
  paragraph_count?: number | null
  table_count?: number | null
}

/** The whole run — what the CLI prints and the UI renders. */
export interface Report {
  schema_version: string
  tool_version: string
  generated_at: string
  document: DocumentInfo
  results: CheckResult[]
  summary: Summary
}

/** A registered checker, from `GET /api/checkers`. */
export interface CheckerInfo {
  name: string
  description: string
  order: number
}

/** A rendering backend, from `GET /api/backends`. */
export interface BackendInfo {
  name: string
  available: boolean
  detail: string
}

/** Returned by `POST /api/jobs`. */
export interface JobCreated {
  job_id: string
  filename: string
  size_bytes: number
  checkers: string[]
  events_url: string
}

/** A job's state when polled. */
export interface JobStatus {
  job_id: string
  state: 'pending' | 'running' | 'done' | 'failed'
  filename: string
  completed: number
  total: number
  error?: string | null
  report?: Report | null
}

/**
 * One server-sent event.
 *
 * The kind discriminates the payload: `report` is present only on `finished`,
 * and `checker`/`index` only on the checker-scoped kinds. Written as a union so
 * the compiler enforces that rather than leaving it to a runtime check.
 */
export type ProgressEvent =
  | { kind: 'started'; message: string; total?: number }
  | {
      kind: 'checker_started'
      message: string
      checker: string
      index: number
      total: number
    }
  | {
      kind: 'checker_finished'
      message: string
      checker: string
      index: number
      total: number
    }
  | { kind: 'finished'; message: string; total?: number; report: Report }
  | { kind: 'failed'; message: string; total?: number }

/** A checker's live state in the UI, fed by the event stream. */
export interface CheckerProgress {
  name: string
  state: 'pending' | 'running' | 'done'
  detail?: string
}

/** Severity order for sorting, worst first. */
export const SEVERITY_ORDER: Record<Severity, number> = {
  error: 0,
  warning: 1,
  info: 2,
}

/** Human labels for severities. */
export const SEVERITY_LABEL: Record<Severity, string> = {
  error: '错误',
  warning: '警告',
  info: '提示',
}

/** Human labels for a checker's status. */
export const STATUS_LABEL: Record<CheckStatus, string> = {
  pass: '通过',
  fail: '不通过',
  warn: '有警告',
  skipped: '已跳过',
  error: '检查器出错',
}

/** Human labels for a run's overall status. */
export const OVERALL_LABEL: Record<CheckStatus, string> = {
  pass: '没有发现问题',
  warn: '有问题建议修一下',
  fail: '有必须修的问题',
  skipped: '已跳过',
  error: '有检查器运行失败',
}

/** A finding's coordinate, rendered the way the CLI does. */
export function describeLocation(location: FindingLocation): string {
  const bits: string[] = []
  if (location.page != null) bits.push(`p.${location.page}`)
  if (location.section != null) bits.push(`§${location.section}`)
  if (location.paragraph != null) bits.push(`¶${location.paragraph}`)
  if (location.table != null) {
    let where = `tbl${location.table}`
    if (location.row != null) {
      where += `[${location.row}`
      where += location.column != null ? `,${location.column}]` : ']'
    }
    bits.push(where)
  }
  return bits.length > 0 ? bits.join(' ') : '(document)'
}

/** Byte count, rendered for a human. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** Flatten findings across checkers, worst first. */
export function allFindings(report: Report): Finding[] {
  return report.results
    .flatMap((result) => result.findings)
    .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
}
