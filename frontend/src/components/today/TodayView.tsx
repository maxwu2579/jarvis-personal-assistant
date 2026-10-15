import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  calendarConnection,
  calendarEvents,
  completeTask,
  confirmTask,
  getToday,
  markNotificationRead,
  postponeTask,
} from '../../api'
import { formatDue, localDatetimeToIso } from '../../datetime'
import {
  REMINDER_STATUS_LABELS,
  type CalendarConnection,
  type CalendarEvent,
  type TodayExecution,
  type TodayExecutionReason,
  type TodayList,
  type TodayProjection,
  type TodayReminder,
  type TodayTask,
} from '../../types'

/**
 * 今天（Phase 11A）：只读的每日总览。
 *
 * - 只读取当前事实来源的投影；自身不保存任何状态（无 localStorage / sessionStorage / URL）；
 * - 仅在打开页面、点击「刷新」和每次既有操作之后重新读取；不轮询、不自动跨日；
 * - 所有写操作都是既有的领域接口，原样调用；
 * - 日历事件只在用户点击「加载今日日程」后，通过既有的日历读取接口加载，不缓存。
 */

const EXECUTION_REASON_LABELS: Record<TodayExecutionReason, string> = {
  confirmation_required: '等待确认',
  failed: '执行失败',
  timed_out: '执行超时',
}

const EXECUTION_TYPE_LABELS: Record<string, string> = {
  'calendar.create_event': '创建日历事件',
  'documents.reindex': '重建文档索引',
  'reminder.send': '发送提醒',
  'system.ping': '系统检测',
}

const SHOW_AS_LABELS: Record<CalendarEvent['show_as'], string> = {
  free: '空闲',
  tentative: '暂定',
  busy: '忙碌',
  oof: '外出',
  workingElsewhere: '异地工作',
  unknown: '状态未知',
}

const UNAVAILABLE_TEXT = '暂时无法读取此部分。'

type CalendarLoad = 'idle' | 'loading' | 'loaded' | 'error'

/** 浏览器的 IANA 时区；无法确定时返回 null（绝不使用任何回退时区）。 */
function browserTimeZone(): string | null {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
    return typeof zone === 'string' && zone.length > 0 ? zone : null
  } catch {
    return null
  }
}

function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    return [e.message, e.code ? `Code: ${e.code}` : null].filter(Boolean).join(' · ')
  }
  return e instanceof Error ? e.message : String(e)
}

function Truncated<T>({ list }: { list: TodayList<T> }) {
  if (!list.truncated) return null
  return (
    <p className="form-hint">
      仅显示前 {list.items.length} 项，共 {list.total} 项。
    </p>
  )
}

export default function TodayView() {
  const [timeZone] = useState<string | null>(browserTimeZone)
  const [data, setData] = useState<TodayProjection | null>(null)
  const [loading, setLoading] = useState(timeZone !== null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)
  const [postponingId, setPostponingId] = useState<number | null>(null)
  const [postponeValue, setPostponeValue] = useState('')
  const [connection, setConnection] = useState<CalendarConnection | null>(null)
  const [calendarLoad, setCalendarLoad] = useState<CalendarLoad>('idle')
  const [calendarError, setCalendarError] = useState<string | null>(null)
  const [events, setEvents] = useState<CalendarEvent[]>([])
  // 竞态防护：较旧的响应、或页签卸载之后才返回的响应一律丢弃
  const requestSeq = useRef(0)
  const calendarSeq = useRef(0)
  const mounted = useRef(true)

  /** 读取本地投影。每次读取都会丢弃已加载的日历事件（不缓存）。 */
  const loadToday = useCallback(async () => {
    if (timeZone === null) return
    const seq = ++requestSeq.current
    calendarSeq.current++
    setLoading(true)
    setError(null)
    setEvents([])
    setCalendarLoad('idle')
    setCalendarError(null)
    try {
      const projection = await getToday(timeZone)
      if (seq !== requestSeq.current || !mounted.current) return
      setData(projection)
    } catch (e) {
      if (seq !== requestSeq.current || !mounted.current) return
      setData(null) // 不展示过期数据
      setError(describeError(e))
    } finally {
      if (seq === requestSeq.current && mounted.current) setLoading(false)
    }
  }, [timeZone])

  /** 日历连接状态：只读取本地授权状态，不访问 Microsoft Graph。 */
  const loadConnection = useCallback(async () => {
    const seq = ++calendarSeq.current
    setCalendarError(null)
    try {
      const status = await calendarConnection()
      if (seq !== calendarSeq.current || !mounted.current) return
      setConnection(status)
      setCalendarLoad('idle')
    } catch (e) {
      if (seq !== calendarSeq.current || !mounted.current) return
      setConnection(null)
      setCalendarLoad('error')
      setCalendarError(describeError(e))
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    void loadToday()
    void loadConnection()
    return () => {
      mounted.current = false
    }
  }, [loadToday, loadConnection])

  /** 既有领域操作：原样调用既有接口；无论成功或失败，之后都重新读取投影。 */
  const runAction = async (key: string, action: () => Promise<unknown>): Promise<void> => {
    if (pendingAction !== null) return
    setPendingAction(key)
    setActionError(null)
    try {
      await action()
    } catch (e) {
      if (mounted.current) setActionError(describeError(e))
    } finally {
      if (mounted.current) {
        setPendingAction(null)
        setPostponingId(null)
        setPostponeValue('')
        void loadToday()
      }
    }
  }

  /** 仅由用户点击触发：按 Today 响应中的本地日范围读取既有的日历事件接口。 */
  const loadCalendarEvents = async (): Promise<void> => {
    if (data === null || calendarLoad === 'loading') return
    const seq = ++calendarSeq.current
    setCalendarLoad('loading')
    setCalendarError(null)
    try {
      const range = await calendarEvents(data.day_start, data.day_end)
      if (seq !== calendarSeq.current || !mounted.current) return
      setEvents(range.events)
      setCalendarLoad('loaded')
    } catch (e) {
      if (seq !== calendarSeq.current || !mounted.current) return
      setEvents([])
      setCalendarLoad('error')
      setCalendarError(describeError(e))
    }
  }

  const retryCalendar = (): void => {
    if (connection === null) void loadConnection()
    else void loadCalendarEvents()
  }

  if (timeZone === null) {
    return (
      <section className="today" aria-label="今天">
        <h2>今天</h2>
        <p className="banner banner--error" role="alert">
          无法确定浏览器时区，无法显示今天的内容。
        </p>
      </section>
    )
  }

  const busy = pendingAction !== null

  const taskRow = (task: TodayTask, kind: 'draft' | 'open') => (
    <li key={task.id} className="today-item">
      <div className="today-item-main">
        <span className="today-item-title">{task.title}</span>
        <span className="today-item-meta">
          {task.due_at ? `截止 ${formatDue(task.due_at)}` : '未设截止时间'}
        </span>
      </div>
      {kind === 'draft' ? (
        <div className="today-item-actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => void runAction(`confirm-${task.id}`, () => confirmTask(task.id))}
          >
            确认
          </button>
        </div>
      ) : postponingId === task.id ? (
        <form
          className="today-item-actions"
          onSubmit={(e) => {
            e.preventDefault()
            if (!postponeValue) return
            void runAction(`postpone-${task.id}`, () =>
              postponeTask(task.id, localDatetimeToIso(postponeValue)),
            )
          }}
        >
          <label>
            新的截止时间{' '}
            <input
              type="datetime-local"
              value={postponeValue}
              disabled={busy}
              onChange={(e) => setPostponeValue(e.target.value)}
            />
          </label>
          <button type="submit" className="btn btn--primary" disabled={busy || !postponeValue}>
            保存延期
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={busy}
            onClick={() => {
              setPostponingId(null)
              setPostponeValue('')
            }}
          >
            取消
          </button>
        </form>
      ) : (
        <div className="today-item-actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => void runAction(`complete-${task.id}`, () => completeTask(task.id))}
          >
            完成
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={busy}
            onClick={() => {
              setPostponingId(task.id)
              setPostponeValue('')
            }}
          >
            延期
          </button>
        </div>
      )}
    </li>
  )

  const taskSection = (
    title: string,
    label: string,
    list: TodayList<TodayTask> | null,
    emptyText: string,
    kind: 'draft' | 'open',
  ) => (
    <section className="today-section" aria-label={label}>
      <h3>{title}</h3>
      {list === null ? (
        <p className="today-unavailable">{UNAVAILABLE_TEXT}</p>
      ) : list.items.length === 0 ? (
        <p className="chat-empty">{emptyText}</p>
      ) : (
        <>
          <ul className="today-list">{list.items.map((task) => taskRow(task, kind))}</ul>
          <Truncated list={list} />
        </>
      )}
    </section>
  )

  const reminderGroup = (title: string, list: TodayList<TodayReminder>) =>
    list.items.length === 0 ? null : (
      <div className="today-group">
        <h4>{title}</h4>
        <ul className="today-list">
          {list.items.map((reminder) => (
            <li key={reminder.id} className="today-item">
              <div className="today-item-main">
                <span className="today-item-title">{reminder.task_title}</span>
                <span className="today-item-meta">
                  {REMINDER_STATUS_LABELS[reminder.status]} · {formatDue(reminder.remind_at)}
                </span>
              </div>
            </li>
          ))}
        </ul>
        <Truncated list={list} />
      </div>
    )

  const executionRow = (execution: TodayExecution) => (
    <li key={execution.id} className="today-item">
      <div className="today-item-main">
        <span className="today-item-title">
          {EXECUTION_TYPE_LABELS[execution.execution_type] ?? execution.execution_type}
        </span>
        <span className="today-item-meta">
          {EXECUTION_REASON_LABELS[execution.reason]} · {formatDue(execution.occurred_at)}
        </span>
      </div>
      <div className="today-item-actions">
        <a href={`?tab=executions&execution=${execution.id}`}>查看执行</a>
      </div>
    </li>
  )

  const reminders = data?.reminders
  const remindersEmpty =
    reminders?.status === 'ok' &&
    reminders.attention?.items.length === 0 &&
    reminders.scheduled?.items.length === 0 &&
    reminders.delivered?.items.length === 0
  const otherOpen = data?.tasks.other_open ?? null

  return (
    <section className="today" aria-label="今天">
      <header className="today-header">
        <h2>{data ? `今天 · ${data.local_date} · ${data.timezone}` : '今天'}</h2>
        <button type="button" className="btn btn--ghost" disabled={loading || busy} onClick={() => void loadToday()}>
          刷新
        </button>
      </header>

      {actionError && (
        <div className="banner banner--error" role="alert">
          <span>{actionError}</span>
          <button type="button" className="banner-close" aria-label="关闭错误提示" onClick={() => setActionError(null)}>
            ×
          </button>
        </div>
      )}

      {loading && <p className="chat-empty" role="status">正在加载…</p>}

      {!loading && error && (
        <div className="banner banner--error" role="alert">
          <span>无法加载今天的内容：{error}</span>
          <button type="button" className="btn btn--ghost" onClick={() => void loadToday()}>
            重试
          </button>
        </div>
      )}

      {!loading && data && (
        <>
          {taskSection('逾期任务', '逾期任务', data.tasks.overdue, '没有逾期任务。', 'open')}
          {taskSection('今日到期', '今日到期', data.tasks.due_today, '今天没有到期任务。', 'open')}
          {taskSection('待确认草稿', '待确认草稿', data.tasks.drafts, '没有待确认的草稿。', 'draft')}
          {otherOpen && (otherOpen.later > 0 || otherOpen.no_due_date > 0) && (
            <p className="form-hint today-other-open">
              另有 {otherOpen.later} 个之后到期、{otherOpen.no_due_date} 个未设截止时间的任务，可在「任务」页查看。
            </p>
          )}

          <section className="today-section" aria-label="今日提醒">
            <h3>今日提醒</h3>
            {reminders?.status !== 'ok' || !reminders.attention || !reminders.scheduled || !reminders.delivered ? (
              <p className="today-unavailable">{UNAVAILABLE_TEXT}</p>
            ) : remindersEmpty ? (
              <p className="chat-empty">今天没有提醒。</p>
            ) : (
              <>
                {reminderGroup('需要关注', reminders.attention)}
                {reminderGroup('今日待提醒', reminders.scheduled)}
                {reminderGroup('今日已提醒', reminders.delivered)}
              </>
            )}
            <p className="form-hint">提醒由独立运行的提醒程序投递；当前版本无法检测它是否正在运行。</p>
          </section>

          <section className="today-section" aria-label="未读通知">
            <h3>未读通知</h3>
            {data.notifications.unread === null ? (
              <p className="today-unavailable">{UNAVAILABLE_TEXT}</p>
            ) : data.notifications.unread.items.length === 0 ? (
              <p className="chat-empty">没有未读通知。</p>
            ) : (
              <>
                <ul className="today-list">
                  {data.notifications.unread.items.map((notification) => (
                    <li key={notification.id} className="today-item">
                      <div className="today-item-main">
                        <span className="today-item-title">{notification.title}</span>
                        <span className="today-item-meta">
                          {notification.body} · {formatDue(notification.created_at)}
                        </span>
                      </div>
                      <div className="today-item-actions">
                        <button
                          type="button"
                          className="btn btn--ghost"
                          disabled={busy}
                          onClick={() =>
                            void runAction(`read-${notification.id}`, () => markNotificationRead(notification.id))
                          }
                        >
                          标记已读
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
                <Truncated list={data.notifications.unread} />
              </>
            )}
          </section>

          <section className="today-section" aria-label="需要关注的执行">
            <h3>需要关注的执行</h3>
            {data.executions.attention === null ? (
              <p className="today-unavailable">{UNAVAILABLE_TEXT}</p>
            ) : data.executions.attention.items.length === 0 ? (
              <p className="chat-empty">没有需要关注的执行。</p>
            ) : (
              <>
                <ul className="today-list">{data.executions.attention.items.map(executionRow)}</ul>
                <Truncated list={data.executions.attention} />
              </>
            )}
          </section>

          <section className="today-section" aria-label="今日日程">
            <h3>今日日程（Outlook）</h3>
            {calendarLoad === 'error' ? (
              <>
                <p className="today-unavailable" role="alert">
                  {calendarError ?? '无法读取日历。'}
                </p>
                <button type="button" className="btn btn--ghost" onClick={retryCalendar}>
                  重试加载
                </button>
              </>
            ) : connection === null ? (
              <p className="chat-empty">正在读取日历连接状态…</p>
            ) : !connection.configured ? (
              <p className="chat-empty">尚未配置 Outlook 日历。</p>
            ) : !connection.connected ? (
              <p className="chat-empty">Outlook 未连接。可在「日历」页连接。</p>
            ) : calendarLoad === 'loading' ? (
              <>
                <p className="chat-empty" role="status">正在读取今日日程…</p>
                <button type="button" className="btn btn--primary" disabled>
                  加载今日日程
                </button>
              </>
            ) : calendarLoad === 'loaded' ? (
              events.length === 0 ? (
                <p className="chat-empty">今天没有日历事件。</p>
              ) : (
                <ul className="today-list">
                  {events.map((event) => (
                    <li key={event.external_event_id} className="today-item">
                      <div className="today-item-main">
                        <span className="today-item-title">
                          {event.title}
                          {event.cancelled ? ' · 已取消' : ''}
                        </span>
                        <span className="today-item-meta">
                          {event.is_all_day
                            ? '全天'
                            : `${formatDue(event.start_at)} — ${formatDue(event.end_at)}`}{' '}
                          · {SHOW_AS_LABELS[event.show_as]}
                          {event.location ? ` · ${event.location}` : ''}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <button type="button" className="btn btn--primary" onClick={() => void loadCalendarEvents()}>
                加载今日日程
              </button>
            )}
          </section>
        </>
      )}
    </section>
  )
}
