/**
 * The whole UI, in one component tree.
 *
 * Kept in one file on purpose for now. The state is a small machine — pick
 * checkers, drop a file, watch it run, read the report — and splitting it across
 * files before that machine has settled would spread tightly coupled state over
 * three modules. The pieces below are separate components, just co-located.
 *
 * State model:
 *
 *   phase       what the screen is doing; exactly one of idle/uploading/running/done/error
 *   checkers    which checkers are selected (empty = all)
 *   progress    per-checker state during a run, driven by the SSE stream
 *   report      the finished result
 *
 * `phase` is a single value rather than several booleans because "loading and
 * error and has a report" is not a state that should be representable.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  createJob,
  deleteJob,
  listBackends,
  listCheckers,
  streamJob,
} from './api'
import type {
  BackendInfo,
  CheckerInfo,
  CheckerProgress,
  Finding,
  ProgressEvent,
  Report,
  Severity,
} from './types'
import {
  OVERALL_LABEL,
  SEVERITY_LABEL,
  allFindings,
  describeLocation,
  formatBytes,
} from './types'
import './App.css'

type Phase = 'idle' | 'uploading' | 'running' | 'done' | 'error'

const ACCEPTED = '.docx'

export default function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [file, setFile] = useState<File | null>(null)
  const [uploadFraction, setUploadFraction] = useState(0)
  const [checkers, setCheckers] = useState<CheckerInfo[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [backends, setBackends] = useState<BackendInfo[]>([])
  const [progress, setProgress] = useState<CheckerProgress[]>([])
  const [log, setLog] = useState<string[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)

  const closeStream = useRef<(() => void) | null>(null)
  const currentJob = useRef<string | null>(null)

  // Load the checker list and backend availability once.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [found, available] = await Promise.all([listCheckers(), listBackends()])
        if (cancelled) return
        setCheckers(found)
        setBackends(available)
        // Default to a render-free run when no backend exists, so the first
        // experience is not a checker that silently skips itself.
        const noRender = available.every((backend) => !backend.available)
        setSelected(
          new Set(noRender ? found.filter((c) => c.name !== 'layout').map((c) => c.name) : []),
        )
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.message
              : '无法连接到 docqc 服务，请确认 `docqc serve` 正在运行',
          )
          setPhase('error')
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Close any open stream when the component goes away.
  useEffect(() => () => closeStream.current?.(), [])

  const reset = useCallback(() => {
    closeStream.current?.()
    closeStream.current = null
    if (currentJob.current) {
      // Best-effort: the server also drops uploads on eviction.
      void deleteJob(currentJob.current).catch(() => undefined)
      currentJob.current = null
    }
    setPhase('idle')
    setProgress([])
    setLog([])
    setReport(null)
    setError(null)
    setUploadFraction(0)
  }, [])

  const run = useCallback(
    async (chosen: File) => {
      reset()
      setFile(chosen)
      setPhase('uploading')

      try {
        const created = await createJob(chosen, [...selected], setUploadFraction)
        currentJob.current = created.job_id
        setProgress(created.checkers.map((name) => ({ name, state: 'pending' as const })))
        setPhase('running')

        closeStream.current = streamJob(
          created.job_id,
          (event: ProgressEvent) => {
            setLog((previous) => [...previous, event.message])

            if (event.kind === 'checker_started') {
              setProgress((previous) =>
                previous.map((item) =>
                  item.name === event.checker ? { ...item, state: 'running' } : item,
                ),
              )
            } else if (event.kind === 'checker_finished') {
              setProgress((previous) =>
                previous.map((item) =>
                  item.name === event.checker
                    ? { ...item, state: 'done', detail: event.message }
                    : item,
                ),
              )
            } else if (event.kind === 'finished') {
              setReport(event.report)
              setPhase('done')
            } else if (event.kind === 'failed') {
              setError(event.message)
              setPhase('error')
            }
          },
          (message) => {
            setError(message)
            setPhase('error')
          },
        )
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
        setPhase('error')
      }
    },
    [reset, selected],
  )

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      setDragging(false)
      const dropped = event.dataTransfer.files[0]
      if (dropped) void run(dropped)
    },
    [run],
  )

  const busy = phase === 'uploading' || phase === 'running'
  const noBackend = backends.length > 0 && backends.every((b) => !b.available)

  return (
    <div className="app">
      <header className="header">
        <h1>docqc</h1>
        <p className="tagline">在交付前检查 Word 文档的排版、公式和结构</p>
      </header>

      {noBackend && (
        <div className="notice notice--warn">
          本机没有可用的渲染后端（Word 或 LibreOffice），<code>layout</code>{' '}
          检查会跳过自己。其余检查器不受影响。
        </div>
      )}

      <CheckerPicker
        checkers={checkers}
        selected={selected}
        disabled={busy}
        onChange={setSelected}
      />

      <DropZone
        phase={phase}
        file={file}
        fraction={uploadFraction}
        dragging={dragging}
        disabled={busy}
        onPick={(picked) => void run(picked)}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      />

      {progress.length > 0 && <ProgressList items={progress} />}

      {phase === 'error' && error && (
        <div className="notice notice--error">
          <strong>出错了</strong>
          <p>{error}</p>
          <button type="button" className="button" onClick={reset}>
            重来
          </button>
        </div>
      )}

      {phase === 'done' && report && (
        <>
          <ReportHeader report={report} />
          <FindingList report={report} />
          <div className="actions">
            <button type="button" className="button" onClick={reset}>
              检查另一份
            </button>
          </div>
        </>
      )}

      {log.length > 0 && <ActivityLog lines={log} />}
    </div>
  )
}

function CheckerPicker({
  checkers,
  selected,
  disabled,
  onChange,
}: {
  checkers: CheckerInfo[]
  selected: Set<string>
  disabled: boolean
  onChange: (next: Set<string>) => void
}) {
  if (checkers.length === 0) return null

  const toggle = (name: string) => {
    const next = new Set(selected)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    onChange(next)
  }

  return (
    <section className="panel">
      <div className="panel__head">
        <h2>检查器</h2>
        <button
          type="button"
          className="link"
          disabled={disabled}
          onClick={() => onChange(new Set())}
        >
          {selected.size === 0 ? '全部（当前）' : '改为全部'}
        </button>
      </div>
      <div className="checkers">
        {checkers.map((checker) => {
          const active = selected.size === 0 || selected.has(checker.name)
          return (
            <label key={checker.name} className={`checker ${active ? 'checker--on' : ''}`}>
              <input
                type="checkbox"
                checked={active}
                disabled={disabled}
                onChange={() => toggle(checker.name)}
              />
              <span className="checker__name">{checker.name}</span>
              <span className="checker__desc">{checker.description}</span>
            </label>
          )
        })}
      </div>
    </section>
  )
}

function DropZone({
  phase,
  file,
  fraction,
  dragging,
  disabled,
  onPick,
  onDragOver,
  onDragLeave,
  onDrop,
}: {
  phase: Phase
  file: File | null
  fraction: number
  dragging: boolean
  disabled: boolean
  onPick: (file: File) => void
  onDragOver: (event: React.DragEvent) => void
  onDragLeave: () => void
  onDrop: (event: React.DragEvent) => void
}) {
  const input = useRef<HTMLInputElement>(null)

  const pick = (event: React.ChangeEvent<HTMLInputElement>) => {
    const chosen = event.target.files?.[0]
    if (chosen) onPick(chosen)
    // Reset so re-picking the same file fires `change` again.
    event.target.value = ''
  }

  return (
    <section
      className={`drop ${dragging ? 'drop--over' : ''} ${disabled ? 'drop--busy' : ''}`}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(event) => {
        if (!disabled && (event.key === 'Enter' || event.key === ' ')) {
          input.current?.click()
        }
      }}
      role="button"
      tabIndex={0}
    >
      <input
        ref={input}
        type="file"
        accept={ACCEPTED}
        onChange={pick}
        hidden
        disabled={disabled}
      />
      {phase === 'uploading' ? (
        <>
          <p className="drop__title">上传中… {Math.round(fraction * 100)}%</p>
          <div className="bar">
            <div className="bar__fill" style={{ width: `${fraction * 100}%` }} />
          </div>
        </>
      ) : file && disabled ? (
        <>
          <p className="drop__title">{file.name}</p>
          <p className="drop__hint">检查中，请稍候…</p>
        </>
      ) : (
        <>
          <p className="drop__title">把 .docx 拖到这里</p>
          <p className="drop__hint">或点击选择文件</p>
        </>
      )}
    </section>
  )
}

function ProgressList({ items }: { items: CheckerProgress[] }) {
  return (
    <section className="panel">
      <h2>进度</h2>
      <ul className="progress">
        {items.map((item) => (
          <li key={item.name} className={`progress__row progress__row--${item.state}`}>
            <span className="progress__dot" aria-hidden="true" />
            <span className="progress__name">{item.name}</span>
            <span className="progress__state">
              {item.state === 'pending' && '等待'}
              {item.state === 'running' && '运行中'}
              {item.state === 'done' && (item.detail ?? '完成')}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ReportHeader({ report }: { report: Report }) {
  const { document: doc, summary } = report
  const tone =
    summary.status === 'pass'
      ? 'ok'
      : summary.status === 'fail' || summary.status === 'error'
        ? 'bad'
        : 'warn'

  return (
    <section className={`verdict verdict--${tone}`}>
      <div className="verdict__headline">
        <strong>{OVERALL_LABEL[summary.status]}</strong>
        <span className="verdict__counts">
          {summary.errors} 错误 · {summary.warnings} 警告
          {summary.infos > 0 && ` · ${summary.infos} 提示`}
        </span>
      </div>
      <div className="verdict__doc">
        <span title={doc.path}>{doc.name}</span>
        <span className="muted">
          {formatBytes(doc.size_bytes)} · {doc.paragraph_count ?? 0} 段 ·{' '}
          {doc.table_count ?? 0} 表 · sha256 {doc.sha256.slice(0, 12)}…
        </span>
      </div>
    </section>
  )
}

function FindingList({ report }: { report: Report }) {
  const [filter, setFilter] = useState<Severity | 'all'>('all')
  const [showInfo, setShowInfo] = useState(false)

  const findings = useMemo(() => allFindings(report), [report])
  const visible = useMemo(
    () =>
      findings.filter((finding) =>
        filter === 'all'
          ? showInfo || finding.severity !== 'info'
          : finding.severity === filter,
      ),
    [findings, filter, showInfo],
  )

  const tally = useMemo(() => {
    const counts: Record<Severity, number> = { error: 0, warning: 0, info: 0 }
    for (const finding of findings) counts[finding.severity] += 1
    return counts
  }, [findings])

  if (report.summary.errors === 0 && report.summary.warnings === 0) {
    return (
      <section className="panel">
        <h2>问题</h2>
        <p className="empty">没有发现需要修的问题。</p>
      </section>
    )
  }

  return (
    <section className="panel">
      <div className="panel__head">
        <h2>问题（{visible.length}）</h2>
        <div className="tabs">
          {(['all', 'error', 'warning', 'info'] as const).map((key) => {
            const count = key === 'all' ? findings.length : tally[key]
            if (key === 'info' && count === 0) return null
            return (
              <button
                key={key}
                type="button"
                className={`tab ${filter === key ? 'tab--on' : ''}`}
                onClick={() => setFilter(key)}
              >
                {key === 'all' ? '全部' : SEVERITY_LABEL[key]}
                <span className="tab__count">{count}</span>
              </button>
            )
          })}
        </div>
      </div>

      {filter === 'all' && tally.info > 0 && (
        <label className="toggle">
          <input
            type="checkbox"
            checked={showInfo}
            onChange={(event) => setShowInfo(event.target.checked)}
          />
          显示 {tally.info} 条提示
        </label>
      )}

      <ul className="findings">
        {visible.map((finding, index) => (
          <FindingRow
            key={`${finding.checker}-${finding.code}-${finding.location.paragraph ?? index}`}
            finding={finding}
          />
        ))}
      </ul>
    </section>
  )
}

function FindingRow({ finding }: { finding: Finding }) {
  const [open, setOpen] = useState(false)
  const where = describeLocation(finding.location)
  const evidenceEntries = Object.entries(finding.evidence)

  return (
    <li className={`finding finding--${finding.severity}`}>
      <button
        type="button"
        className="finding__head"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span className={`badge badge--${finding.severity}`}>
          {SEVERITY_LABEL[finding.severity]}
        </span>
        <span className="finding__where">{where}</span>
        <span className="finding__message">{finding.message}</span>
        <span className="finding__chevron">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="finding__body">
          {finding.location.text && <p className="finding__quote">{finding.location.text}</p>}
          {finding.hint && <p className="finding__hint">→ {finding.hint}</p>}
          <p className="finding__code">
            {finding.checker} · {finding.code}
          </p>
          {evidenceEntries.length > 0 && (
            <dl className="evidence">
              {evidenceEntries.map(([key, value]) => (
                <div key={key} className="evidence__row">
                  <dt>{key}</dt>
                  <dd>{renderEvidence(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}
    </li>
  )
}

/** Render an evidence value without assuming it is a primitive. */
function renderEvidence(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function ActivityLog({ lines }: { lines: string[] }) {
  const [open, setOpen] = useState(false)
  const recent = open ? lines : lines.slice(-3)

  return (
    <section className="panel panel--dim">
      <div className="panel__head">
        <h2>运行日志</h2>
        <button type="button" className="link" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : `展开全部 ${lines.length} 条`}
        </button>
      </div>
      <ul className="log">
        {recent.map((line, index) => (
          <li key={`${index}-${line.slice(0, 12)}`}>{line}</li>
        ))}
      </ul>
    </section>
  )
}
