import { useEffect } from 'react'
import type { ExecutionDetail, ExecutionStep } from '../../types'

interface Props {
  execution: ExecutionDetail
  step: ExecutionStep
  pendingAction: string | null
  onConfirm: () => void
  onReject: () => void
  onRefresh: () => void
  onClose: () => void
}

export default function ExecutionConfirmationDialog(props: Props) {
  const documentTarget =
    props.execution.execution_type === 'documents.reindex' &&
    props.execution.target?.type === 'document'
      ? props.execution.target
      : null
  const targetVerified = documentTarget !== null

  useEffect(() => {
    const handleKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && props.pendingAction === null) props.onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [props])

  return (
    <div className="execution-modal-backdrop">
      <section className="execution-modal" role="dialog" aria-modal="true" aria-labelledby="execution-confirm-title">
        <div className="execution-section-heading">
          <div>
            <h2 id="execution-confirm-title">Execution Confirmation</h2>
            <p>Step ID {props.step.id} · {props.step.step_key}</p>
          </div>
          <button type="button" className="banner-close" aria-label="关闭确认窗口" disabled={props.pendingAction !== null} onClick={props.onClose}>×</button>
        </div>

        <dl className="execution-facts">
          <div><dt>Execution Type</dt><dd>{props.execution.execution_type}</dd></div>
          <div><dt>Target Document</dt><dd>{documentTarget?.document_id ?? '无法验证'}</dd></div>
          <div><dt>Confirmation Status</dt><dd>{props.step.confirmation_status ?? 'N/A'}</dd></div>
        </dl>

        {targetVerified ? (
          <p className="execution-risk">该操作会重新构建目标文档索引，必须由用户显式授权。</p>
        ) : (
          <div className="banner banner--error" role="alert">
            <span>
              Unable to verify the execution target. Confirmation is disabled for safety.
              {props.execution.correlation_id ? ` Correlation ID: ${props.execution.correlation_id}` : ''}
            </span>
          </div>
        )}

        <div className="execution-modal-actions">
          {targetVerified && (
            <>
              <button type="button" className="btn btn--primary" disabled={props.pendingAction !== null} onClick={props.onConfirm}>
                {props.pendingAction === 'confirm' ? '确认中…' : 'Confirm'}
              </button>
              <button type="button" className="btn btn--danger" disabled={props.pendingAction !== null} onClick={props.onReject}>
                {props.pendingAction === 'reject' ? '拒绝中…' : 'Reject'}
              </button>
            </>
          )}
          <button type="button" className="btn btn--ghost" disabled={props.pendingAction !== null} onClick={props.onRefresh}>Refresh</button>
          <button type="button" className="btn btn--ghost" disabled={props.pendingAction !== null} onClick={props.onClose}>Close</button>
        </div>
      </section>
    </div>
  )
}
