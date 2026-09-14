import { useEffect, useRef, useState } from 'react'
import type { ExecutionCreatePayload } from '../../types'

type ExecutionKind = 'reminder.send' | 'documents.reindex'

export interface CreateSubmissionResult {
  complete: boolean
  executionId: number | null
}

interface Props {
  onSubmit: (
    payload: ExecutionCreatePayload,
    idempotencyKey: string,
    existingExecutionId: number | null,
  ) => Promise<CreateSubmissionResult>
  completedExecutionId: number | null
}

function newIdempotencyKey(): string {
  return `jarvis-ui-${window.crypto.randomUUID()}`
}

export default function ExecutionCreateForm({ onSubmit, completedExecutionId }: Props) {
  const [executionType, setExecutionType] = useState<ExecutionKind>('reminder.send')
  const [reminderId, setReminderId] = useState('')
  const [taskId, setTaskId] = useState('')
  const [message, setMessage] = useState('')
  const [documentId, setDocumentId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const keyRef = useRef<string | null>(null)
  const snapshotRef = useRef<string | null>(null)
  const pendingExecutionIdRef = useRef<number | null>(null)
  const submittingRef = useRef(false)

  const resetForm = (): void => {
    setReminderId('')
    setTaskId('')
    setMessage('')
    setDocumentId('')
    keyRef.current = null
    snapshotRef.current = null
    pendingExecutionIdRef.current = null
  }

  useEffect(() => {
    if (
      completedExecutionId !== null &&
      completedExecutionId === pendingExecutionIdRef.current
    ) {
      resetForm()
    }
  }, [completedExecutionId])

  const markDraftChanged = (): void => {
    keyRef.current = null
    snapshotRef.current = null
    pendingExecutionIdRef.current = null
    setValidationError(null)
  }

  const buildRequest = (): ExecutionCreatePayload | null => {
    if (executionType === 'reminder.send') {
      const reminder = Number(reminderId)
      const task = Number(taskId)
      if (!Number.isInteger(reminder) || reminder < 1 || !Number.isInteger(task) || task < 1) {
        setValidationError('Reminder ID 和 Task ID 必须是正整数。')
        return null
      }
      if (!message.trim()) {
        setValidationError('Message 不能为空。')
        return null
      }
      return {
        execution_type: executionType,
        payload: { reminder_id: reminder, task_id: task, message: message.trim() },
      }
    }

    const document = Number(documentId)
    if (!Number.isInteger(document) || document < 1) {
      setValidationError('Document ID 必须是正整数。')
      return null
    }
    return { execution_type: executionType, payload: { document_id: document } }
  }

  const handleSubmit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault()
    if (submittingRef.current) return
    const request = buildRequest()
    if (request === null) return

    const snapshot = JSON.stringify(request)
    if (snapshotRef.current !== snapshot || keyRef.current === null) {
      snapshotRef.current = snapshot
      keyRef.current = newIdempotencyKey()
      pendingExecutionIdRef.current = null
    }

    submittingRef.current = true
    setSubmitting(true)
    setValidationError(null)
    try {
      const result = await onSubmit(
        request,
        keyRef.current,
        pendingExecutionIdRef.current,
      )
      pendingExecutionIdRef.current = result.executionId
      if (result.complete) resetForm()
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  return (
    <form className="execution-create" onSubmit={(event) => void handleSubmit(event)}>
      <div className="execution-section-heading">
        <div>
          <h2>创建执行</h2>
          <p>
            {pendingExecutionIdRef.current === null
              ? '创建后自动请求入队；若响应不确定，将按同一幂等键恢复。'
              : `Execution #${pendingExecutionIdRef.current} 已创建；继续操作只恢复该 Execution。`}
          </p>
        </div>
      </div>

      <label className="execution-field">
        <span>Execution Type</span>
        <select
          value={executionType}
          disabled={submitting}
          onChange={(event) => {
            markDraftChanged()
            setExecutionType(event.target.value as ExecutionKind)
          }}
        >
          <option value="reminder.send">reminder.send</option>
          <option value="documents.reindex">documents.reindex</option>
        </select>
      </label>

      {executionType === 'reminder.send' ? (
        <div className="execution-form-grid">
          <label className="execution-field">
            <span>Reminder ID</span>
            <input
              type="number"
              min="1"
              step="1"
              required
              value={reminderId}
              disabled={submitting}
              onChange={(event) => {
                markDraftChanged()
                setReminderId(event.target.value)
              }}
            />
          </label>
          <label className="execution-field">
            <span>Task ID</span>
            <input
              type="number"
              min="1"
              step="1"
              required
              value={taskId}
              disabled={submitting}
              onChange={(event) => {
                markDraftChanged()
                setTaskId(event.target.value)
              }}
            />
          </label>
          <label className="execution-field execution-field--wide">
            <span>Message</span>
            <input
              type="text"
              required
              value={message}
              disabled={submitting}
              onChange={(event) => {
                markDraftChanged()
                setMessage(event.target.value)
              }}
            />
          </label>
        </div>
      ) : (
        <label className="execution-field">
          <span>Document ID</span>
          <input
            type="number"
            min="1"
            step="1"
            required
            value={documentId}
            disabled={submitting}
            onChange={(event) => {
              markDraftChanged()
              setDocumentId(event.target.value)
            }}
          />
        </label>
      )}

      {validationError && <p className="form-error">{validationError}</p>}
      <button type="submit" className="btn btn--primary" disabled={submitting}>
        {submitting
          ? '提交中…'
          : pendingExecutionIdRef.current === null ? '创建并入队' : '继续入队'}
      </button>
    </form>
  )
}
