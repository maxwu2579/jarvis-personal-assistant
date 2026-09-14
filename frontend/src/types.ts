export type TaskStatus = 'DRAFT' | 'CONFIRMED' | 'DONE'

export interface Task {
  id: number
  title: string
  description: string | null
  status: TaskStatus
  /** UTC ISO 8601，例如 2026-08-10T01:00:00Z */
  due_at: string | null
  created_at: string
  updated_at: string
}

export interface TaskCreatePayload {
  title: string
  description: string | null
  due_at: string | null
}

/** 表单提交的数据：due_at 已由组件转换为带时区的 ISO 8601 字符串 */
export interface NewTaskInput {
  title: string
  description: string
  dueAt: string | null
}

export const STATUS_LABELS: Record<TaskStatus, string> = {
  DRAFT: '草稿',
  CONFIRMED: '已确认',
  DONE: '已完成',
}

// ---- 对话 / 消息（Phase 1） ----

export type MessageRole = 'USER' | 'ASSISTANT' | 'SYSTEM' | 'TOOL'

export interface Conversation {
  id: number
  title: string | null
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  conversation_id: number
  role: MessageRole
  content: string
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  latency_ms: number | null
  created_at: string
}

export interface Usage {
  prompt_tokens: number | null
  completion_tokens: number | null
  latency_ms: number | null
}

export interface ChatExchange {
  user_message: Message
  assistant_message: Message
  usage: Usage
}

export const ROLE_LABELS: Record<MessageRole, string> = {
  USER: '用户',
  ASSISTANT: 'JARVIS',
  SYSTEM: '系统',
  TOOL: '工具',
}

// ---- 任务建议（Phase 2） ----

export interface TaskProposalArguments {
  title: string
  description: string | null
  due_at: string | null
}

export interface TaskProposal {
  id: number
  conversation_id: number
  user_message_id: number
  assistant_message_id: number
  task_id: number
  action: string
  arguments: TaskProposalArguments
  explanation: string
  status: string
  created_at: string
}

export interface TaskProposalExchange {
  user_message: Message
  assistant_message: Message
  proposal: TaskProposal
  created_task: Task
  usage: Usage
}

export interface TaskProposalWithTask {
  proposal: TaskProposal
  task: Task
}

// ---- 只读行动提案（Phase 8A） ----

export type ActionProposalAction = 'create_task' | 'create_task_with_reminder' | 'create_calendar_event'
export type ActionProposalStatus = 'READY' | 'NEEDS_CLARIFICATION' | 'UNSUPPORTED' | 'INVALID'

export interface ActionTaskPreview {
  title: string | null
  due_at: string | null
}

export interface ReadyActionTask extends ActionTaskPreview {
  title: string
}

export interface ActionReminderPreview {
  remind_at: string | null
}

export interface ActionCalendarEventPreview {
  title: string | null
  start_at: string | null
  end_at: string | null
  location: string | null
}

export interface ReadyActionCalendarEvent extends ActionCalendarEventPreview {
  title: string
  start_at: string
  end_at: string
}

interface ActionProposalBase {
  missing_fields: string[]
  clarification_question: string | null
  explanation: string
}

export type ActionProposal =
  | (ActionProposalBase & {
      status: 'READY'
      action: 'create_task' | 'create_task_with_reminder'
      task: ReadyActionTask
      reminder: ActionReminderPreview | null
      calendar_event?: null
    })
  | (ActionProposalBase & {
      status: 'READY'
      action: 'create_calendar_event'
      task: null
      reminder: null
      calendar_event: ReadyActionCalendarEvent
    })
  | (ActionProposalBase & {
      status: 'NEEDS_CLARIFICATION'
      action: ActionProposalAction
      task: ActionTaskPreview | null
      reminder: ActionReminderPreview | null
      calendar_event?: ActionCalendarEventPreview | null
    })
  | (ActionProposalBase & {
      status: 'UNSUPPORTED' | 'INVALID'
      action: null
      task: null
      reminder: null
      calendar_event?: null
    })

export interface ActionProposalExchange {
  proposal: ActionProposal
  usage: Usage
}

export type ConfirmableActionProposal =
  | {
      action: 'create_task'
      task: ReadyActionTask
      reminder: null
    }
  | {
      action: 'create_task_with_reminder'
      task: ReadyActionTask
      reminder: { remind_at: string }
    }
  | {
      action: 'create_calendar_event'
      task: null
      reminder: null
      calendar_event: ReadyActionCalendarEvent
    }

export interface ActionProposalConfirmation {
  confirmation_id: number
  action: ActionProposalAction
  task: Task | null
  reminder: Reminder | null
  execution?: Execution | null
  replayed: boolean
}

export type ChatMode = 'chat' | 'proposal' | 'action_proposal'

// ---- Reminder / Notification（Phase 3） ----

export type ReminderStatus = 'PENDING' | 'CLAIMED' | 'DELIVERED' | 'CANCELLED' | 'FAILED'

export interface Reminder {
  id: number
  task_id: number
  status: ReminderStatus
  remind_at: string
  claimed_at: string | null
  lease_expires_at: string | null
  delivered_at: string | null
  attempt_count: number
  last_error_code: string | null
  created_at: string
  updated_at: string
}

export interface Notification {
  id: number
  reminder_id: number
  task_id: number
  type: string
  title: string
  body: string
  created_at: string
  read_at: string | null
}

export const REMINDER_STATUS_LABELS: Record<ReminderStatus, string> = {
  PENDING: '待提醒',
  CLAIMED: '投递中',
  DELIVERED: '已提醒',
  CANCELLED: '已取消',
  FAILED: '失败',
}

// ---- 文档摄取（Phase 4A） ----

export type DocumentStatus = 'UPLOADED' | 'PROCESSING' | 'READY' | 'FAILED'

/** 检索索引状态（Phase 4B）：独立于摄取状态，由索引服务控制 */
export type DocumentIndexStatus = 'NOT_INDEXED' | 'INDEXING' | 'INDEXED' | 'INDEX_FAILED'

export interface Document {
  id: number
  original_filename: string
  content_type: string
  size_bytes: number
  sha256: string
  status: DocumentStatus
  error_code: string | null
  error_message: string | null
  /** Phase 4B：检索索引状态 */
  retrieval_status: DocumentIndexStatus
  /** 已索引块数（每块一条向量） */
  indexed_chunk_count: number
  created_at: string
  updated_at: string
}

export interface DocumentChunk {
  id: number
  document_id: number
  chunk_index: number
  /** 预览内容（超长截断约 300 字符，非完整原文） */
  content: string
  content_length: number
  char_start: number
  char_end: number
  token_estimate: number
}

export const DOCUMENT_STATUS_LABELS: Record<DocumentStatus, string> = {
  UPLOADED: '已上传',
  PROCESSING: '处理中',
  READY: '已就绪',
  FAILED: '失败',
}

export const RETRIEVAL_STATUS_LABELS: Record<DocumentIndexStatus, string> = {
  NOT_INDEXED: '未索引',
  INDEXING: '索引中',
  INDEXED: '已索引',
  INDEX_FAILED: '索引失败',
}

// ---- 检索（Phase 4B） ----

export type SearchMode = 'keyword' | 'vector' | 'hybrid'

export const SEARCH_MODE_LABELS: Record<SearchMode, string> = {
  keyword: '关键词（FTS5/BM25）',
  vector: '向量（LocalHash）',
  hybrid: '混合（RRF 融合）',
}

export interface SearchItem {
  document_id: number
  document_title: string
  chunk_id: number
  chunk_index: number
  /** 内容预览（截断约 300 字符，非完整原文） */
  content: string
  content_length: number
  char_start: number
  char_end: number
  keyword_score: number
  vector_score: number
  final_score: number
}

export interface SearchResponse {
  query: string
  mode: SearchMode
  items: SearchItem[]
  total_candidates: number
  embedding_model: string
}

// ---- RAG 问答（Phase 4C） ----

export type RAGStatus = 'ANSWERED' | 'INSUFFICIENT_EVIDENCE'

export interface RAGCitation {
  citation_id: string
  document_id: number
  document_title: string
  chunk_id: number
  chunk_index: number
  char_start: number
  char_end: number
  /** 数据库中该 chunk 的真实连续子串（后端生成，非模型输出） */
  quote: string
  retrieval_score: number
  retrieval_mode: string
}

export interface RAGRetrievalMeta {
  mode: string
  candidate_count: number
  returned_count: number
}

export interface RAGUsage {
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  latency_ms: number | null
}

export interface RAGAnswerResponse {
  answer: string
  status: RAGStatus
  citations: RAGCitation[]
  retrieval: RAGRetrievalMeta
  usage: RAGUsage
  conversation_id: number | null
  user_message_id: number | null
  assistant_message_id: number | null
}

export const RAG_STATUS_LABELS: Record<RAGStatus, string> = {
  ANSWERED: '已回答',
  INSUFFICIENT_EVIDENCE: '证据不足',
}

/** 人类可读的文件大小（KB/MB） */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ---- Execution（Phase 6E） ----

export type ExecutionStatus =
  | 'CREATED'
  | 'QUEUED'
  | 'CLAIMED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'
  | 'TIMED_OUT'
  | 'CANCEL_REQUESTED'

export type StepStatus = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'

export type ConfirmationStatus = 'PENDING' | 'GRANTED' | 'REJECTED' | 'EXPIRED'

export interface Execution {
  id: number
  owner_id: string
  execution_type: string
  status: ExecutionStatus
  idempotency_key: string
  normalized_payload_hash: string
  run_at: string | null
  retry_after: string | null
  correlation_id: string | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
  cancel_requested_at: string | null
  attempt_count: number
  max_attempts: number
  timeout_seconds: number | null
  last_error_code: string | null
  last_error_message: string | null
  retry_of_id: number | null
  created_at: string
  updated_at: string
}

export interface ExecutionDocumentTarget {
  type: 'document'
  document_id: number
}

export interface ExecutionCalendarTarget {
  type: 'calendar_event'
  title: string
  start_at: string
  end_at: string
  location: string | null
}

export interface ExecutionCalendarResult {
  external_event_id: string
  title: string
  start_at: string
  end_at: string
  location: string | null
}

export type ExecutionTarget = ExecutionDocumentTarget | ExecutionCalendarTarget

/** 单项详情比列表项多一个由后端显式 allowlist 的安全业务目标。 */
export interface ExecutionDetail extends Execution {
  target: ExecutionTarget | null
  result: ExecutionCalendarResult | null
}

export interface ExecutionEvent {
  id: number
  execution_id: number
  sequence_number: number
  event_type: string
  actor_type: string
  actor_id: string
  attempt_id: number | null
  step_id: number | null
  correlation_id: string | null
  payload: Record<string, unknown> | null
  created_at: string
}

export interface ExecutionStep {
  id: number
  execution_id: number
  step_key: string
  step_index: number
  tool_name: string
  status: StepStatus
  requires_confirmation: boolean
  confirmation_status: ConfirmationStatus | null
  confirmation_actor_type: string | null
  confirmation_actor_id: string | null
  confirmed_at: string | null
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface ExecutionCreatePayload {
  execution_type: string
  payload?: Record<string, unknown>
  run_at?: string | null
}

export interface ExecutionCreateResult {
  execution: Execution
  replayed: boolean
}

export const EXECUTION_STATUS_LABELS: Record<ExecutionStatus, string> = {
  CREATED: '已创建',
  QUEUED: '排队中',
  CLAIMED: '已领取',
  RUNNING: '执行中',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  TIMED_OUT: '已超时',
  CANCEL_REQUESTED: '取消请求中',
}
// Phase 9A: safe read-only calendar fields; credentials never cross the API.
export interface CalendarConnection {
  connected: boolean
  configured: boolean
  provider: 'microsoft_global'
  account_hint: string | null
  read_authorized: boolean
  write_authorized: boolean
}

export interface CalendarMetadata {
  external_calendar_id: string
  name: string
}

export interface CalendarEvent {
  external_event_id: string
  title: string
  start_at: string
  end_at: string
  is_all_day: boolean
  cancelled: boolean
  show_as: 'free' | 'tentative' | 'busy' | 'oof' | 'workingElsewhere' | 'unknown'
  location: string | null
}

export interface CalendarEventRange {
  events: CalendarEvent[]
  complete: true
  busy_projection_available: false
}
