import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  calendarConnection,
  confirmActionProposal,
  confirmTask,
  createActionProposal,
  createConversation,
  createTaskProposal,
  listConversations,
  listMessages,
  listTaskProposals,
  getExecution,
  sendMessage,
} from '../../api'
import {
  ROLE_LABELS,
  type ActionProposal,
  type ActionProposalConfirmation,
  type ChatMode,
  type ConfirmableActionProposal,
  type Conversation,
  type CalendarConnection,
  type ExecutionDetail,
  type Message,
  type Task,
  type TaskProposalWithTask,
} from '../../types'
import ActionProposalConfirmPanel from './ActionProposalConfirmPanel'
import ProposalCard from './ProposalCard'

interface ActionProposalPreview {
  id: string
  idempotencyKey: string
  request: string
  proposal: ActionProposal
  result?: ActionProposalConfirmation
  executionDetail?: ExecutionDetail
}

function initialCalendarExecutionId(): number | null {
  const raw = new URLSearchParams(window.location.search).get('calendar_execution')
  if (!raw || !/^[1-9]\d{0,14}$/.test(raw)) return null
  const id = Number(raw)
  return Number.isSafeInteger(id) ? id : null
}

/** 浏览器本地 IANA 时区（如 Asia/Shanghai）；失败时回退 Asia/Shanghai。 */
function localTimezone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    return tz && tz.length > 0 ? tz : 'Asia/Shanghai'
  } catch {
    return 'Asia/Shanghai'
  }
}

export default function ChatView() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [proposals, setProposals] = useState<TaskProposalWithTask[]>([])
  const [actionProposals, setActionProposals] = useState<ActionProposalPreview[]>([])
  const [mode, setMode] = useState<ChatMode>('chat')
  const [input, setInput] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [loadingList, setLoadingList] = useState(true)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [sending, setSending] = useState(false)
  const [confirmingId, setConfirmingId] = useState<number | null>(null)
  const [confirmingActionId, setConfirmingActionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [calendarStatus, setCalendarStatus] = useState<CalendarConnection | null>(null)
  const [trackedExecutionId, setTrackedExecutionId] = useState<number | null>(initialCalendarExecutionId)
  const [trackedExecution, setTrackedExecution] = useState<ExecutionDetail | null>(null)
  const [trackingError, setTrackingError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  const actionConfirmGuardRef = useRef<string | null>(null)
  // 竞态防护：对话切换请求序号 + 组件卸载标志
  const selectRequestRef = useRef(0)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    if (trackedExecutionId === null) return
    let cancelled = false
    let timer: number | undefined
    const load = async () => {
      try {
        const detail = await getExecution(trackedExecutionId)
        if (cancelled) return
        setTrackedExecution(detail)
        setTrackingError(null)
        if (!['SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'].includes(detail.status)) {
          timer = window.setTimeout(() => { void load() }, 2000)
        }
      } catch (e) {
        if (!cancelled) setTrackingError(e instanceof ApiError ? `${e.message} · ${e.code ?? '请求失败'}` : '无法读取执行状态')
      }
    }
    void load()
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer) }
  }, [trackedExecutionId])

  useEffect(() => {
    const refreshCalendar = () => { void calendarConnection().then(setCalendarStatus).catch(() => setCalendarStatus(null)) }
    refreshCalendar()
    window.addEventListener('focus', refreshCalendar)
    return () => window.removeEventListener('focus', refreshCalendar)
  }, [])

  const showError = (e: unknown): void => {
    if (!mountedRef.current) return
    if (e instanceof ApiError) {
      const details = [
        e.message,
        e.code ? `Code: ${e.code}` : null,
        e.correlationId ? `Correlation ID: ${e.correlationId}` : null,
      ].filter(Boolean)
      setError(details.join(' · '))
      return
    }
    setError(e instanceof Error ? e.message : String(e))
  }

  const loadConversations = useCallback(async () => {
    try {
      setConversations(await listConversations())
    } catch (e) {
      showError(e)
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => {
    void loadConversations()
  }, [loadConversations])

  const selectConversation = async (id: number): Promise<void> => {
    const requestId = ++selectRequestRef.current
    setActiveId(id)
    setMessages([])
    setProposals([])
    setActionProposals([])
    setLoadingMessages(true)
    setError(null)
    try {
      const [msgs, props] = await Promise.all([listMessages(id), listTaskProposals(id)])
      if (requestId !== selectRequestRef.current || !mountedRef.current) return
      setMessages(msgs)
      setProposals(props)
    } catch (e) {
      if (requestId !== selectRequestRef.current || !mountedRef.current) return
      showError(e)
    } finally {
      if (requestId === selectRequestRef.current && mountedRef.current) {
        setLoadingMessages(false)
      }
    }
  }

  const handleCreate = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault()
    try {
      const conversation = await createConversation(newTitle.trim() || null)
      setNewTitle('')
      await loadConversations()
      await selectConversation(conversation.id)
      const label = conversation.title ?? `对话 #${conversation.id}`
      setNotice(`已创建对话「${label}」`)
      window.setTimeout(() => setNotice(null), 3000)
    } catch (err) {
      showError(err)
    }
  }

  const handleSend = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault()
    if (activeId === null || input.trim() === '' || sending) return
    setSending(true)
    setError(null)
    try {
      if (mode === 'action_proposal') {
        const request = input.trim()
        const exchange = await createActionProposal(activeId, request, localTimezone())
        const id = window.crypto.randomUUID()
        setActionProposals([
          {
            id,
            idempotencyKey: `action.${id}`,
            request,
            proposal: exchange.proposal,
          },
        ])
        setNotice('已生成行动提案；只有明确确认后才会创建内容')
      } else if (mode === 'proposal') {
        const exchange = await createTaskProposal(activeId, input.trim(), localTimezone())
        setMessages((prev) => [...prev, exchange.user_message, exchange.assistant_message])
        setProposals((prev) => [
          ...prev.filter((p) => p.proposal.id !== exchange.proposal.id),
          { proposal: exchange.proposal, task: exchange.created_task },
        ])
        setNotice('已生成任务建议（草稿，需确认后才生效）')
      } else {
        const exchange = await sendMessage(activeId, input.trim())
        setMessages((prev) => [...prev, exchange.user_message, exchange.assistant_message])
      }
      setInput('')
      void loadConversations()
    } catch (err) {
      showError(err)
    } finally {
      setSending(false)
    }
    window.setTimeout(() => setNotice(null), 4000)
  }

  const handleConfirmProposal = async (task: Task): Promise<void> => {
    if (activeId === null || confirmingId !== null) return
    setConfirmingId(task.id)
    setError(null)
    try {
      await confirmTask(task.id)
      setProposals(await listTaskProposals(activeId))
      setNotice(`已确认任务「${task.title}」`)
    } catch (err) {
      showError(err)
    } finally {
      setConfirmingId(null)
    }
    window.setTimeout(() => setNotice(null), 4000)
  }

  const switchMode = (nextMode: ChatMode): void => {
    if (nextMode !== mode) {
      setActionProposals([])
      setInput('')
      setError(null)
    }
    setMode(nextMode)
  }

  const handleConfirmAction = async (item: ActionProposalPreview): Promise<void> => {
    if (
      activeId === null ||
      item.proposal.status !== 'READY' ||
      item.result !== undefined ||
      !actionProposals.some(candidate => candidate.id === item.id) ||
      sending ||
      actionConfirmGuardRef.current !== null
    ) {
      return
    }

    let confirmable: ConfirmableActionProposal
    if (item.proposal.action === 'create_calendar_event') {
      if (!item.proposal.calendar_event || calendarStatus?.write_authorized !== true) return
      confirmable = {
        action: 'create_calendar_event', task: null, reminder: null,
        calendar_event: item.proposal.calendar_event,
      }
    } else if (item.proposal.action === 'create_task_with_reminder') {
      const remindAt = item.proposal.reminder?.remind_at
      if (!remindAt) return
      confirmable = {
        action: 'create_task_with_reminder',
        task: item.proposal.task,
        reminder: { remind_at: remindAt },
      }
    } else {
      confirmable = {
        action: 'create_task',
        task: item.proposal.task,
        reminder: null,
      }
    }

    actionConfirmGuardRef.current = item.id
    setConfirmingActionId(item.id)
    setError(null)
    try {
      const result = await confirmActionProposal(activeId, confirmable, item.idempotencyKey)
      if (!mountedRef.current) return
      setActionProposals((current) =>
        current.map((candidate) =>
          candidate.id === item.id ? { ...candidate, result } : candidate,
        ),
      )
      if (result.execution) {
        setNotice(`${result.replayed ? '已恢复' : '已排队'}日历执行 #${result.execution.id}`)
        const url = new URL(window.location.href)
        url.searchParams.set('tab', 'chat')
        url.searchParams.set('calendar_execution', String(result.execution.id))
        window.history.replaceState(null, '', url)
        setTrackedExecutionId(result.execution.id)
      } else if (result.task) {
        const reminderText = result.reminder ? `，Reminder #${result.reminder.id}` : ''
        setNotice(`${result.replayed ? '已恢复' : '已创建'} Task #${result.task.id}${reminderText}`)
      }
    } catch (err) {
      showError(err)
    } finally {
      actionConfirmGuardRef.current = null
      if (mountedRef.current) setConfirmingActionId(null)
    }
    window.setTimeout(() => setNotice(null), 5000)
  }

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [actionProposals, messages])

  return (
    <section className="chat" aria-label="LLM 对话">
      <aside className="chat-sidebar">
        <form className="chat-new" onSubmit={handleCreate}>
          <input
            type="text"
            placeholder="新对话标题（可选）"
            aria-label="新对话标题（可选）"
            value={newTitle}
            maxLength={200}
            onChange={(e) => setNewTitle(e.target.value)}
          />
          <button type="submit" className="btn btn--primary">
            新建对话
          </button>
        </form>

        <div className="chat-conv-list" role="list" aria-label="对话列表">
          {loadingList && <p className="chat-empty">加载中…</p>}
          {!loadingList && conversations.length === 0 && (
            <p className="chat-empty">还没有对话，先新建一个。</p>
          )}
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              role="listitem"
              className={`chat-conv-item${conversation.id === activeId ? ' chat-conv-item--active' : ''}`}
              onClick={() => void selectConversation(conversation.id)}
            >
              <span className="chat-conv-title">
                {conversation.title ?? `对话 #${conversation.id}`}
              </span>
              <span className="chat-conv-time">
                {new Date(conversation.updated_at).toLocaleString('zh-CN', {
                  month: '2-digit',
                  day: '2-digit',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <div className="chat-main">
        {error && (
          <div className="banner banner--error" role="alert">
            <span>{error}</span>
            <button
              type="button"
              className="banner-close"
              aria-label="关闭错误提示"
              onClick={() => setError(null)}
            >
              ×
            </button>
          </div>
        )}
        {notice && (
          <div className="banner banner--success" aria-live="polite">
            {notice}
          </div>
        )}

        <div className="chat-messages" aria-live="polite">
          {activeId === null && <p className="chat-empty">选择一个对话，或新建一个开始聊天。</p>}
          {loadingMessages && <p className="chat-empty">加载消息…</p>}
          {!loadingMessages &&
            activeId !== null &&
            messages.length === 0 &&
            proposals.length === 0 &&
            actionProposals.length === 0 && (
              <p className="chat-empty">这个对话还没有内容，发一句试试。</p>
            )}
          {messages.map((message) => (
            <div key={message.id} className={`msg msg--${message.role.toLowerCase()}`}>
              <div className="msg-meta">
                <span className="msg-role">{ROLE_LABELS[message.role]}</span>
                {message.role === 'ASSISTANT' && message.model && (
                  <span className="msg-model">
                    {message.model}
                    {message.latency_ms !== null ? ` · ${message.latency_ms} ms` : ''}
                  </span>
                )}
              </div>
              <div className="msg-content">{message.content}</div>
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {actionProposals.length > 0 && (
          <section className="chat-action-proposals" aria-label="行动提案预览（只读）">
            <h3 className="chat-proposals-title">行动提案预览（只读）</h3>
            {actionProposals.map((item) => (
              <ActionProposalConfirmPanel
                key={item.id}
                request={item.request}
                proposal={item.proposal}
                result={item.result}
                executionDetail={item.result?.execution?.id === trackedExecutionId ? trackedExecution ?? undefined : item.executionDetail}
                confirming={confirmingActionId !== null}
                writeAuthorized={calendarStatus?.write_authorized === true}
                readAuthorized={calendarStatus?.read_authorized === true}
                onRefreshWriteAuth={() => { void calendarConnection().then(setCalendarStatus).catch(showError) }}
                onConfirm={() => void handleConfirmAction(item)}
              />
            ))}
          </section>
        )}

        {trackedExecutionId !== null && (
          <section className="action-confirm-result" aria-label="Outlook 日历执行状态">
            <strong>Outlook 日历执行 #{trackedExecutionId}</strong>
            <p role="status">{trackedExecution ? `状态：${trackedExecution.status}` : '正在读取状态…'}</p>
            {trackedExecution?.result && <p>已创建 Outlook 日历事件：{trackedExecution.result.title} · {new Date(trackedExecution.result.start_at).toLocaleString('zh-CN')} — {new Date(trackedExecution.result.end_at).toLocaleString('zh-CN')}{trackedExecution.result.location ? ` · ${trackedExecution.result.location}` : ''}</p>}
            {trackedExecution?.last_error_message && <p role="alert">{trackedExecution.last_error_message}{trackedExecution.correlation_id ? ` · 请求 ${trackedExecution.correlation_id}` : ''}</p>}
            {trackingError && <p role="alert">{trackingError}</p>}
            <a href={`?tab=executions&execution=${trackedExecutionId}`}>查看执行详情</a>
            <button type="button" onClick={() => { void getExecution(trackedExecutionId).then(setTrackedExecution).catch(() => setTrackingError('无法刷新执行状态')) }}>刷新状态</button>
          </section>
        )}

        {proposals.length > 0 && (
          <section className="chat-proposals" aria-label="任务建议">
            <h3 className="chat-proposals-title">任务建议（仅草稿，确认后生效）</h3>
            {proposals.map(({ proposal, task }) => (
              <ProposalCard
                key={proposal.id}
                proposal={proposal}
                task={task}
                confirming={confirmingId === task.id}
                onConfirm={(t) => void handleConfirmProposal(t)}
              />
            ))}
          </section>
        )}

        <form className="chat-input-row" onSubmit={handleSend}>
          <textarea
            aria-label="消息内容"
            placeholder={
              mode === 'action_proposal'
                ? '描述想做的事，例如：明天下午 3 点提醒我交报告…'
                : mode === 'proposal'
                ? '描述想安排的任务，例如：请帮我安排在2026年8月20日下午5点前完成JARVIS报告…'
                : '输入消息…'
            }
            rows={2}
            value={input}
            maxLength={8000}
            disabled={activeId === null || sending}
            onChange={(e) => {
              if (mode === 'action_proposal' && actionProposals.length > 0) {
                setActionProposals([])
              }
              setInput(e.target.value)
            }}
          />
          <button
            type="submit"
            className="btn btn--primary"
            disabled={activeId === null || sending || input.trim() === ''}
          >
            {sending
              ? '处理中…'
              : mode === 'action_proposal'
                ? '生成只读提案'
                : mode === 'proposal' ? '生成任务建议' : '发送'}
          </button>
        </form>

        <div className="chat-mode-row" role="radiogroup" aria-label="输入模式">
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'chat'}
            className={`mode-btn${mode === 'chat' ? ' mode-btn--active' : ''}`}
            onClick={() => switchMode('chat')}
          >
            普通聊天
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'proposal'}
            className={`mode-btn${mode === 'proposal' ? ' mode-btn--active' : ''}`}
            onClick={() => switchMode('proposal')}
          >
            生成任务建议
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'action_proposal'}
            className={`mode-btn${mode === 'action_proposal' ? ' mode-btn--active' : ''}`}
            onClick={() => switchMode('action_proposal')}
          >
            行动提案（只读）
          </button>
        </div>
      </div>
    </section>
  )
}
