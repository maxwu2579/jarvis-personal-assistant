import { formatDue } from '../../datetime'
import type { ExecutionDetail as ExecutionDetailType, ExecutionEvent, ExecutionStep } from '../../types'
import { canCancel, canDecideStep, canEnqueue, canRetry } from './executionActions'
import ExecutionStatusBadge from './ExecutionStatusBadge'

interface Props {
  execution: ExecutionDetailType | null
  steps: ExecutionStep[]
  events: ExecutionEvent[]
  loading: boolean
  refreshing: boolean
  error: string | null
  pendingAction: string | null
  onRefresh: () => void
  onEnqueue: () => void
  onCancel: () => void
  onRetry: () => void
  onReviewStep: (stepId: number) => void
}

function valueOrDash(value: string | null): string {
  return value ? formatDue(value) : '—'
}

export default function ExecutionDetail(props: Props) {
  const execution = props.execution
  if (props.loading && execution === null) {
    return <section className="execution-detail"><p className="execution-state">详情加载中…</p></section>
  }
  if (execution === null) {
    return (
      <section className="execution-detail">
        {props.error ? (
          <div className="banner banner--error" role="alert">{props.error}</div>
        ) : (
          <p className="execution-state">选择一个 Execution 查看详情。</p>
        )}
      </section>
    )
  }

  return (
    <section className="execution-detail">
      <div className="execution-section-heading">
        <div>
          <h2>Execution #{execution.id}</h2>
          <p>{props.refreshing ? '正在同步后端状态…' : execution.execution_type}</p>
        </div>
        <button type="button" className="btn btn--ghost" onClick={props.onRefresh} disabled={props.refreshing}>
          刷新详情
        </button>
      </div>

      {props.error && <div className="banner banner--error" role="alert">{props.error}</div>}

      <div className="execution-detail-header">
        <ExecutionStatusBadge status={execution.status} />
        <div className="execution-actions">
          {canEnqueue(execution) && (
            <button type="button" className="btn btn--primary" disabled={props.pendingAction !== null} onClick={props.onEnqueue}>
              {props.pendingAction === 'enqueue' ? '入队中…' : 'Queue / Enqueue'}
            </button>
          )}
          {canCancel(execution) && (
            <button type="button" className="btn btn--ghost" disabled={props.pendingAction !== null} onClick={props.onCancel}>
              {props.pendingAction === 'cancel' ? '取消中…' : 'Cancel'}
            </button>
          )}
          {canRetry(execution) && (
            <button type="button" className="btn btn--primary" disabled={props.pendingAction !== null} onClick={props.onRetry}>
              {props.pendingAction === 'retry' ? '重试中…' : 'Retry'}
            </button>
          )}
        </div>
      </div>

      <dl className="execution-facts">
        <div><dt>Execution Type</dt><dd>{execution.execution_type}</dd></div>
        <div><dt>Created</dt><dd>{formatDue(execution.created_at)}</dd></div>
        <div><dt>Updated</dt><dd>{formatDue(execution.updated_at)}</dd></div>
        <div><dt>Run At</dt><dd>{valueOrDash(execution.run_at)}</dd></div>
        <div><dt>Correlation ID</dt><dd>{execution.correlation_id ?? '—'}</dd></div>
        <div>
          <dt>Safe Target</dt>
          <dd>{execution.target?.type === 'document' ? `Document #${execution.target.document_id}` : '—'}</dd>
        </div>
        <div><dt>Attempts</dt><dd>{execution.attempt_count} / {execution.max_attempts}</dd></div>
        <div><dt>Last Error</dt><dd>{execution.last_error_code ? `${execution.last_error_code}: ${execution.last_error_message ?? ''}` : '—'}</dd></div>
      </dl>

      <div className="execution-subsection">
        <h3>Execution Steps</h3>
        {props.steps.length === 0 ? (
          <p className="execution-state execution-state--compact">尚未产生步骤。</p>
        ) : (
          <ol className="execution-step-list">
            {props.steps.map((step) => (
              <li key={step.id} className="execution-step">
                <div className="execution-step-heading">
                  <strong>#{step.step_index} · {step.step_key}</strong>
                  <span>{step.status}</span>
                </div>
                <div className="execution-step-meta">
                  <span>Step ID {step.id}</span>
                  <span>Tool {step.tool_name}</span>
                  <span>Confirmation {step.confirmation_status ?? 'N/A'}</span>
                  <span>Created {formatDue(step.created_at)}</span>
                  <span>Updated {formatDue(step.updated_at)}</span>
                  {step.started_at && <span>Started {formatDue(step.started_at)}</span>}
                  {step.finished_at && <span>Finished {formatDue(step.finished_at)}</span>}
                </div>
                {step.error_code && <p className="execution-safe-error">{step.error_code}: {step.error_message ?? '执行步骤失败'}</p>}
                {canDecideStep(execution, step) && (
                  <button type="button" className="btn btn--primary" onClick={() => props.onReviewStep(step.id)}>
                    Review Confirmation
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}
      </div>

      <div className="execution-subsection">
        <h3>Execution Event Timeline</h3>
        {props.events.length === 0 ? (
          <p className="execution-state execution-state--compact">尚无审计事件。</p>
        ) : (
          <ol className="execution-event-list">
            {props.events.map((event) => (
              <li key={event.id} className="execution-event">
                <span className="execution-event-sequence">{event.sequence_number}</span>
                <div>
                  <strong>{event.event_type}</strong>
                  <p>{formatDue(event.created_at)} · Actor type {event.actor_type} · Step {event.step_id ?? '—'} · Correlation {event.correlation_id ?? '—'}</p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  )
}
