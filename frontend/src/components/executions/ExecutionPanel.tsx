import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  cancelExecution,
  confirmStep,
  createExecution,
  enqueueExecution,
  getExecution,
  listExecutionEvents,
  listExecutions,
  listExecutionSteps,
  rejectStep,
  retryExecution,
} from '../../api'
import type {
  Execution,
  ExecutionCreatePayload,
  ExecutionDetail,
  ExecutionEvent,
  ExecutionStatus,
  ExecutionStep,
} from '../../types'
import ExecutionConfirmationDialog from './ExecutionConfirmationDialog'
import ExecutionCreateForm, { type CreateSubmissionResult } from './ExecutionCreateForm'
import ExecutionDetailView from './ExecutionDetail'
import ExecutionList from './ExecutionList'
import {
  canCancel,
  canDecideStep,
  canEnqueue,
  canRetry,
  isTerminalExecution,
} from './executionActions'

const PAGE_SIZE = 10
const LOCAL_ACTOR = 'default'

interface SafeUiError {
  message: string
  status?: number
  code?: string
  correlationId?: string
}

function safeError(error: unknown): SafeUiError {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      status: error.status,
      code: error.code,
      correlationId: error.correlationId,
    }
  }
  return { message: error instanceof Error ? error.message : '请求失败，请稍后重试。' }
}

function displayError(error: SafeUiError): string {
  return [error.message, error.code ? `Code: ${error.code}` : null, error.correlationId ? `Correlation: ${error.correlationId}` : null]
    .filter(Boolean)
    .join(' · ')
}

function initialExecutionId(): number | null {
  const value = new URLSearchParams(window.location.search).get('execution')
  if (value === null) return null
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

export default function ExecutionPanel() {
  const [executions, setExecutions] = useState<Execution[]>([])
  const [statusFilter, setStatusFilter] = useState<ExecutionStatus | ''>('')
  const [typeFilter, setTypeFilter] = useState('')
  const [page, setPage] = useState(0)
  const [listLoading, setListLoading] = useState(true)
  const [listRefreshing, setListRefreshing] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(initialExecutionId)
  const [detail, setDetail] = useState<ExecutionDetail | null>(null)
  const [steps, setSteps] = useState<ExecutionStep[]>([])
  const [events, setEvents] = useState<ExecutionEvent[]>([])
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailRefreshing, setDetailRefreshing] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)
  const [dialogStepId, setDialogStepId] = useState<number | null>(null)
  const [dismissedStepId, setDismissedStepId] = useState<number | null>(null)
  const [completedExecutionId, setCompletedExecutionId] = useState<number | null>(null)
  const mountedRef = useRef(true)
  const listSeqRef = useRef(0)
  const detailSeqRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const showNotice = useCallback((message: string): void => {
    setNotice(message)
    window.setTimeout(() => {
      if (mountedRef.current) setNotice(null)
    }, 4500)
  }, [])

  const selectExecution = useCallback((id: number): void => {
    setSelectedId(id)
    const url = new URL(window.location.href)
    url.searchParams.set('tab', 'executions')
    url.searchParams.set('execution', String(id))
    window.history.replaceState(null, '', url)
  }, [])

  const loadList = useCallback(async (background = false): Promise<void> => {
    const seq = ++listSeqRef.current
    if (background) setListRefreshing(true)
    else setListLoading(true)
    try {
      const rows = await listExecutions({
        status: statusFilter || undefined,
        execution_type: typeFilter || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        sort: 'created_at',
        order: 'desc',
      })
      if (!mountedRef.current || seq !== listSeqRef.current) return
      setExecutions(rows)
      setListError(null)
    } catch (error) {
      if (!mountedRef.current || seq !== listSeqRef.current) return
      setListError(displayError(safeError(error)))
    } finally {
      if (mountedRef.current && seq === listSeqRef.current) {
        setListLoading(false)
        setListRefreshing(false)
      }
    }
  }, [page, statusFilter, typeFilter])

  const loadDetail = useCallback(async (id: number, background = false): Promise<void> => {
    const seq = ++detailSeqRef.current
    if (background) setDetailRefreshing(true)
    else setDetailLoading(true)
    try {
      const [nextDetail, nextSteps, nextEvents] = await Promise.all([
        getExecution(id),
        listExecutionSteps(id),
        listExecutionEvents(id),
      ])
      if (!mountedRef.current || seq !== detailSeqRef.current) return
      setDetail(nextDetail)
      setSteps(nextSteps)
      setEvents(nextEvents)
      setDetailError(null)
    } catch (error) {
      if (!mountedRef.current || seq !== detailSeqRef.current) return
      setDetailError(displayError(safeError(error)))
    } finally {
      if (mountedRef.current && seq === detailSeqRef.current) {
        setDetailLoading(false)
        setDetailRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    void loadList()
  }, [loadList])

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null)
      setSteps([])
      setEvents([])
      return
    }
    setDetail(null)
    setSteps([])
    setEvents([])
    setDismissedStepId(null)
    void loadDetail(selectedId)
  }, [loadDetail, selectedId])

  useEffect(() => {
    if (selectedId === null || detail === null || isTerminalExecution(detail)) return
    let cancelled = false
    let timer: number | undefined
    const schedule = (): void => {
      timer = window.setTimeout(() => {
        void Promise.all([loadDetail(selectedId, true), loadList(true)]).finally(() => {
          if (!cancelled) schedule()
        })
      }, 4000)
    }
    schedule()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [detail, loadDetail, loadList, selectedId])

  const pendingStep = useMemo(
    () => detail ? steps.find((step) => canDecideStep(detail, step)) ?? null : null,
    [detail, steps],
  )

  useEffect(() => {
    if (pendingStep === null) {
      setDialogStepId(null)
      setDismissedStepId(null)
    } else if (dialogStepId === null && dismissedStepId !== pendingStep.id) {
      setDialogStepId(pendingStep.id)
    }
  }, [dialogStepId, dismissedStepId, pendingStep])

  const refreshAll = useCallback(async (id: number): Promise<void> => {
    await Promise.all([loadDetail(id, true), loadList(true)])
  }, [loadDetail, loadList])

  const submitCreate = async (
    payload: ExecutionCreatePayload,
    idempotencyKey: string,
    existingExecutionId: number | null,
  ): Promise<CreateSubmissionResult> => {
    setActionError(null)
    setCompletedExecutionId(null)
    let executionId = existingExecutionId
    let current: Execution

    if (executionId === null) {
      try {
        const created = await createExecution(payload, idempotencyKey)
        executionId = created.execution.id
        current = created.execution
      } catch (error) {
        setActionError(displayError(safeError(error)))
        return { complete: false, executionId: null }
      }
    } else {
      try {
        current = await getExecution(executionId)
      } catch (error) {
        setActionError(
          `Execution #${executionId} 已创建，但无法读取当前状态：${displayError(safeError(error))}`,
        )
        return { complete: false, executionId }
      }
    }

    selectExecution(executionId)
    if (current.status === 'CREATED') {
      try {
        current = await enqueueExecution(executionId)
      } catch (enqueueError) {
        const safeEnqueueError = safeError(enqueueError)
        try {
          current = await getExecution(executionId)
        } catch (recoveryError) {
          setActionError(
            `${displayError(safeEnqueueError)} · 无法确认当前状态：${displayError(safeError(recoveryError))}`,
          )
          return { complete: false, executionId }
        }
        await refreshAll(executionId)
        if (current.status === 'CREATED') {
          setActionError(`${displayError(safeEnqueueError)} · Execution 已创建但仍未入队，可安全重试。`)
          return { complete: false, executionId }
        }
        showNotice(`Execution #${executionId} 的入队响应不确定，已从后端恢复为 ${current.status}。`)
        return { complete: true, executionId }
      }
    }

    await refreshAll(executionId)
    showNotice(`Execution #${executionId} 已创建并进入 ${current.status}。`)
    return { complete: true, executionId }
  }

  const runExecutionAction = async (action: 'enqueue' | 'cancel' | 'retry'): Promise<void> => {
    const snapshot = detail
    if (snapshot === null) return
    const legal =
      action === 'enqueue' ? canEnqueue(snapshot) :
      action === 'cancel' ? canCancel(snapshot) : canRetry(snapshot)
    if (!legal) {
      setActionError('当前客户端状态已不允许此操作，正在重新读取后端状态。')
      await refreshAll(snapshot.id)
      return
    }

    setPendingAction(action)
    setActionError(null)
    try {
      if (action === 'enqueue') await enqueueExecution(snapshot.id)
      else if (action === 'cancel') await cancelExecution(snapshot.id)
      else await retryExecution(snapshot.id)
      if (action === 'enqueue') {
        setCompletedExecutionId(snapshot.id)
      }
      showNotice(`Execution #${snapshot.id} ${action} 操作已提交。`)
    } catch (error) {
      const currentError = safeError(error)
      setActionError(
        `${displayError(currentError)}${currentError.status === 409 ? ' · 状态可能已更新。' : ''}`,
      )
    } finally {
      await refreshAll(snapshot.id)
      if (mountedRef.current) setPendingAction(null)
    }
  }

  const runStepAction = async (action: 'confirm' | 'reject', step: ExecutionStep): Promise<void> => {
    const snapshot = detail
    if (snapshot === null || !canDecideStep(snapshot, step)) {
      setActionError('当前 step 已不允许确认决策，正在重新读取后端状态。')
      if (snapshot) await refreshAll(snapshot.id)
      return
    }
    const targetVerified =
      snapshot.execution_type === 'documents.reindex' && snapshot.target?.type === 'document'
    if (!targetVerified) {
      setActionError(`Unable to verify the execution target. Confirmation is disabled for safety.${snapshot.correlation_id ? ` Correlation: ${snapshot.correlation_id}` : ''}`)
      return
    }

    setPendingAction(action)
    setActionError(null)
    try {
      if (action === 'confirm') await confirmStep(snapshot.id, step.id, LOCAL_ACTOR)
      else await rejectStep(snapshot.id, step.id, LOCAL_ACTOR)
      showNotice(`Step #${step.id} 已${action === 'confirm' ? '确认' : '拒绝'}。`)
      setDialogStepId(null)
    } catch (error) {
      const currentError = safeError(error)
      setActionError(`${displayError(currentError)}${currentError.status === 409 ? ' · 状态可能已更新。' : ''}`)
    } finally {
      await refreshAll(snapshot.id)
      if (mountedRef.current) setPendingAction(null)
    }
  }

  const dialogStep = dialogStepId === null ? null : steps.find((step) => step.id === dialogStepId) ?? null

  return (
    <section className="executions-panel">
      {notice && <div className="banner banner--success" aria-live="polite">{notice}</div>}
      {actionError && (
        <div className="banner banner--error" role="alert">
          <span>{actionError}</span>
          <button type="button" className="banner-close" aria-label="关闭错误提示" onClick={() => setActionError(null)}>×</button>
        </div>
      )}

      <ExecutionCreateForm onSubmit={submitCreate} completedExecutionId={completedExecutionId} />
      <ExecutionList
        executions={executions}
        selectedId={selectedId}
        initialLoading={listLoading}
        refreshing={listRefreshing}
        error={listError}
        status={statusFilter}
        executionType={typeFilter}
        page={page}
        pageSize={PAGE_SIZE}
        onStatusChange={(status) => { setStatusFilter(status); setPage(0) }}
        onTypeChange={(executionType) => { setTypeFilter(executionType); setPage(0) }}
        onPageChange={setPage}
        onSelect={selectExecution}
        onRefresh={() => void loadList(true)}
      />
      <ExecutionDetailView
        execution={detail}
        steps={steps}
        events={events}
        loading={detailLoading}
        refreshing={detailRefreshing}
        error={detailError}
        pendingAction={pendingAction}
        onRefresh={() => { if (selectedId !== null) void refreshAll(selectedId) }}
        onEnqueue={() => void runExecutionAction('enqueue')}
        onCancel={() => void runExecutionAction('cancel')}
        onRetry={() => void runExecutionAction('retry')}
        onReviewStep={(stepId) => { setDismissedStepId(null); setDialogStepId(stepId) }}
      />

      {detail && dialogStep && (
        <ExecutionConfirmationDialog
          execution={detail}
          step={dialogStep}
          pendingAction={pendingAction}
          onConfirm={() => void runStepAction('confirm', dialogStep)}
          onReject={() => void runStepAction('reject', dialogStep)}
          onRefresh={() => void refreshAll(detail.id)}
          onClose={() => { setDismissedStepId(dialogStep.id); setDialogStepId(null) }}
        />
      )}
    </section>
  )
}
