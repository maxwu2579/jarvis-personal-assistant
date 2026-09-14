import type {
  CalendarConnection,
  CalendarMetadata,
  CalendarEventRange,
  ActionProposalExchange,
  ActionProposalConfirmation,
  ChatExchange,
  Conversation,
  ConfirmableActionProposal,
  Document,
  DocumentChunk,
  Execution,
  ExecutionCreatePayload,
  ExecutionCreateResult,
  ExecutionDetail,
  ExecutionEvent,
  ExecutionStatus,
  ExecutionStep,
  Message,
  Notification,
  RAGAnswerResponse,
  Reminder,
  SearchMode,
  SearchResponse,
  Task,
  TaskCreatePayload,
  TaskProposalExchange,
  TaskProposalWithTask,
} from './types'

// 后端地址通过环境变量 VITE_API_BASE_URL 配置（frontend/.env.local），
// 未配置时使用本地开发默认值；生产环境必须显式配置。
const API_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const calendarConnectUrl = `${API_URL}/api/calendar/connect`
export const calendarWriteAuthorizeUrl = `${API_URL}/api/calendar/authorize-write`
export const calendarConnection = () => request<CalendarConnection>('/api/calendar/connection')
export const defaultCalendar = () => request<CalendarMetadata>('/api/calendar/default')
export const calendarEvents = (start: string, end: string) =>
  request<CalendarEventRange>(`/api/calendar/events?${new URLSearchParams({ start, end })}`)
export const disconnectCalendar = () => request<CalendarConnection>('/api/calendar/disconnect', {
  method: 'POST', headers: { 'X-Jarvis-Calendar': '1' },
})

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly correlationId?: string

  constructor(message: string, status: number, code?: string, correlationId?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.correlationId = correlationId
  }
}

/** 只解析统一安全错误字段，不把 raw body 暴露给 UI。 */
async function parseError(res: Response): Promise<ApiError> {
  let body: unknown
  try {
    body = await res.json()
  } catch {
    body = undefined
  }

  const error = (
    body as {
      error?: { code?: string; message?: unknown; correlation_id?: string }
    } | null
  )?.error
  const correlationId = error?.correlation_id ?? res.headers.get('X-Request-ID') ?? undefined
  if (error && typeof error.message === 'string') {
    return new ApiError(error.message, res.status, error.code, correlationId)
  }

  return new ApiError(`请求失败（HTTP ${res.status}）`, res.status, undefined, correlationId)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_URL}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError('无法连接后端服务，请确认后端已启动（http://localhost:8000）', 0)
  }
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as T
}

export function listTasks(): Promise<Task[]> {
  return request<Task[]>('/api/tasks')
}

export function createTask(payload: TaskCreatePayload): Promise<Task> {
  return request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify(payload) })
}

export function confirmTask(id: number): Promise<Task> {
  return request<Task>(`/api/tasks/${id}/confirm`, { method: 'POST' })
}

export function completeTask(id: number): Promise<Task> {
  return request<Task>(`/api/tasks/${id}/complete`, { method: 'POST' })
}

export function postponeTask(id: number, dueAt: string): Promise<Task> {
  return request<Task>(`/api/tasks/${id}/postpone`, {
    method: 'PATCH',
    body: JSON.stringify({ due_at: dueAt }),
  })
}

// ---- 对话 / 消息（Phase 1） ----

export function createConversation(title: string | null): Promise<Conversation> {
  return request<Conversation>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title }),
  })
}

export function listConversations(): Promise<Conversation[]> {
  return request<Conversation[]>('/api/conversations')
}

export function getConversation(id: number): Promise<Conversation> {
  return request<Conversation>(`/api/conversations/${id}`)
}

export function listMessages(conversationId: number): Promise<Message[]> {
  return request<Message[]>(`/api/conversations/${conversationId}/messages`)
}

export function sendMessage(conversationId: number, content: string): Promise<ChatExchange> {
  return request<ChatExchange>(`/api/conversations/${conversationId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  })
}

export function createTaskProposal(
  conversationId: number,
  content: string,
  timezone: string,
): Promise<TaskProposalExchange> {
  return request<TaskProposalExchange>(`/api/conversations/${conversationId}/task-proposals`, {
    method: 'POST',
    body: JSON.stringify({ content, timezone }),
  })
}

export function listTaskProposals(conversationId: number): Promise<TaskProposalWithTask[]> {
  return request<TaskProposalWithTask[]>(`/api/conversations/${conversationId}/task-proposals`)
}

export function createActionProposal(
  conversationId: number,
  content: string,
  timezone: string,
): Promise<ActionProposalExchange> {
  return request<ActionProposalExchange>(`/api/conversations/${conversationId}/action-proposals`, {
    method: 'POST',
    body: JSON.stringify({ content, timezone }),
  })
}

export function confirmActionProposal(
  conversationId: number,
  proposal: ConfirmableActionProposal,
  idempotencyKey: string,
): Promise<ActionProposalConfirmation> {
  return request<ActionProposalConfirmation>(
    `/api/conversations/${conversationId}/action-proposals/confirm`,
    {
      method: 'POST',
      body: JSON.stringify({ proposal }),
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
      },
    },
  )
}

// ---- Reminder / Notification（Phase 3） ----

export function listReminders(params?: {
  status?: string
  task_id?: number
  limit?: number
  offset?: number
}): Promise<Reminder[]> {
  const query = new URLSearchParams()
  if (params?.status) query.set('status', params.status)
  if (params?.task_id !== undefined) query.set('task_id', String(params.task_id))
  if (params?.limit !== undefined) query.set('limit', String(params.limit))
  if (params?.offset !== undefined) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<Reminder[]>(`/api/reminders${suffix}`)
}

export function getReminder(id: number): Promise<Reminder> {
  return request<Reminder>(`/api/reminders/${id}`)
}

export function listNotifications(params?: {
  unread_only?: boolean
  task_id?: number
  limit?: number
  offset?: number
}): Promise<Notification[]> {
  const query = new URLSearchParams()
  if (params?.unread_only) query.set('unread_only', 'true')
  if (params?.task_id !== undefined) query.set('task_id', String(params.task_id))
  if (params?.limit !== undefined) query.set('limit', String(params.limit))
  if (params?.offset !== undefined) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<Notification[]>(`/api/notifications${suffix}`)
}

export function getUnreadCount(): Promise<{ count: number }> {
  return request<{ count: number }>('/api/notifications/unread-count')
}

export function markNotificationRead(id: number): Promise<Notification> {
  return request<Notification>(`/api/notifications/${id}/read`, { method: 'POST' })
}

// ---- 文档摄取（Phase 4A） ----

export function listDocuments(): Promise<Document[]> {
  return request<Document[]>('/api/documents')
}

/** multipart 上传：不能手动设置 Content-Type（浏览器负责 boundary）。 */
export async function uploadDocument(file: File): Promise<Document> {
  const form = new FormData()
  form.append('file', file)
  let res: Response
  try {
    res = await fetch(`${API_URL}/api/documents`, { method: 'POST', body: form })
  } catch {
    throw new ApiError('无法连接后端服务，请确认后端已启动（http://localhost:8000）', 0)
  }
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as Document
}

export function getDocument(id: number): Promise<Document> {
  return request<Document>(`/api/documents/${id}`)
}

export function listDocumentChunks(id: number): Promise<DocumentChunk[]> {
  return request<DocumentChunk[]>(`/api/documents/${id}/chunks`)
}

export function deleteDocument(id: number): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/documents/${id}`, { method: 'DELETE' })
}

// ---- 检索（Phase 4B） ----

export interface IndexResult {
  document_id: number
  index_status: string
  chunks_scanned: number
  embeddings_created: number
  embeddings_updated: number
  embeddings_skipped: number
  fts_rows_written: number
  failures: number
}

export function indexDocument(id: number): Promise<IndexResult> {
  return request<IndexResult>(`/api/retrieval/index/${id}`, { method: 'POST' })
}

export function reindexDocument(id: number): Promise<IndexResult> {
  return request<IndexResult>(`/api/retrieval/reindex/${id}`, { method: 'POST' })
}

export function searchDocuments(params: {
  query: string
  mode: SearchMode
  topK: number
  documentIds?: number[]
}): Promise<SearchResponse> {
  return request<SearchResponse>('/api/retrieval/search', {
    method: 'POST',
    body: JSON.stringify({
      query: params.query,
      mode: params.mode,
      top_k: params.topK,
      document_ids: params.documentIds,
    }),
  })
}

// ---- RAG 问答（Phase 4C） ----

export function askRag(params: {
  question: string
  mode: SearchMode
  topK: number
  documentIds?: number[]
}): Promise<RAGAnswerResponse> {
  return request<RAGAnswerResponse>('/api/rag/ask', {
    method: 'POST',
    body: JSON.stringify({
      question: params.question,
      retrieval_mode: params.mode,
      top_k: params.topK,
      document_ids: params.documentIds,
    }),
  })
}

// ---- Execution（Phase 6E） ----

export interface ListExecutionsParams {
  status?: ExecutionStatus
  execution_type?: string
  created_from?: string
  created_to?: string
  limit?: number
  offset?: number
  sort?: 'id' | 'created_at' | 'run_at'
  order?: 'asc' | 'desc'
}

/** 创建 Execution：Idempotency-Key Header 必填（8-128，[A-Za-z0-9._-]）。 */
export function createExecution(
  payload: ExecutionCreatePayload,
  idempotencyKey: string,
): Promise<ExecutionCreateResult> {
  return request<ExecutionCreateResult>('/api/executions', {
    method: 'POST',
    body: JSON.stringify(payload),
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
  })
}

export function listExecutions(params?: ListExecutionsParams): Promise<Execution[]> {
  const query = new URLSearchParams()
  if (params?.status) query.set('status', params.status)
  if (params?.execution_type) query.set('execution_type', params.execution_type)
  if (params?.created_from) query.set('created_from', params.created_from)
  if (params?.created_to) query.set('created_to', params.created_to)
  if (params?.limit !== undefined) query.set('limit', String(params.limit))
  if (params?.offset !== undefined) query.set('offset', String(params.offset))
  if (params?.sort) query.set('sort', params.sort)
  if (params?.order) query.set('order', params.order)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<Execution[]>(`/api/executions${suffix}`)
}

export function getExecution(id: number): Promise<ExecutionDetail> {
  return request<ExecutionDetail>(`/api/executions/${id}`)
}

export function enqueueExecution(id: number): Promise<Execution> {
  return request<Execution>(`/api/executions/${id}/enqueue`, { method: 'POST' })
}

export function listExecutionSteps(id: number): Promise<ExecutionStep[]> {
  return request<ExecutionStep[]>(`/api/executions/${id}/steps`)
}

export function listExecutionEvents(
  id: number,
  params?: { limit?: number; offset?: number },
): Promise<ExecutionEvent[]> {
  const query = new URLSearchParams()
  if (params?.limit !== undefined) query.set('limit', String(params.limit))
  if (params?.offset !== undefined) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<ExecutionEvent[]>(`/api/executions/${id}/events${suffix}`)
}

export function cancelExecution(id: number): Promise<Execution> {
  return request<Execution>(`/api/executions/${id}/cancel`, { method: 'POST' })
}

export function retryExecution(id: number): Promise<Execution> {
  return request<Execution>(`/api/executions/${id}/retry`, { method: 'POST' })
}

/** 确认步骤：actor 必填（显式确认人，无默认值）。 */
export function confirmStep(
  executionId: number,
  stepId: number,
  actor: string,
): Promise<ExecutionStep> {
  return request<ExecutionStep>(
    `/api/executions/${executionId}/steps/${stepId}/confirm`,
    { method: 'POST', body: JSON.stringify({ actor }) },
  )
}

/** 拒绝步骤：稳定失败（EXEC_CONFIRMATION_REJECTED，不重试）。 */
export function rejectStep(
  executionId: number,
  stepId: number,
  actor: string,
): Promise<ExecutionStep> {
  return request<ExecutionStep>(
    `/api/executions/${executionId}/steps/${stepId}/reject`,
    { method: 'POST', body: JSON.stringify({ actor }) },
  )
}
