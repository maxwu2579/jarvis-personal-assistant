import { useCallback, useEffect, useRef, useState } from 'react'
import {
  completeTask,
  confirmTask,
  createTask,
  getUnreadCount,
  listReminders,
  listTasks,
  postponeTask,
} from './api'
import ChatView from './components/chat/ChatView'
import DocumentsView from './components/documents/DocumentsView'
import ExecutionPanel from './components/executions/ExecutionPanel'
import CalendarPanel from './components/calendar/CalendarPanel'
import MemoryPanel from './components/memories/MemoryPanel'
import NotificationCenter from './components/NotificationCenter'
import TaskForm from './components/TaskForm'
import TaskList from './components/TaskList'
import TodayView from './components/today/TodayView'
import type { NewTaskInput, Reminder, Task } from './types'

type Tab = 'today' | 'tasks' | 'chat' | 'documents' | 'executions' | 'calendar' | 'memories'

/** 默认落地页为「今天」（Phase 11A）；`tab` 参数缺失或无法识别时同样进入「今天」。 */
function initialTab(): Tab {
  const requested = new URLSearchParams(window.location.search).get('tab')
  if (requested === 'memories') return 'memories'
  if (requested === 'chat') return 'chat'
  if (requested === 'calendar') return 'calendar'
  if (requested === 'executions') return 'executions'
  if (requested === 'tasks') return 'tasks'
  return 'today'
}

function TaskSection() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [reminders, setReminders] = useState<Reminder[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingIds, setPendingIds] = useState<Set<number>>(new Set())
  // 竞态防护：切换页签卸载后不再 setState；慢的旧请求不得覆盖新请求
  const mountedRef = useRef(true)
  const refreshSeqRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current
    try {
      const [taskList, reminderList] = await Promise.all([listTasks(), listReminders()])
      if (seq !== refreshSeqRef.current || !mountedRef.current) return
      setTasks(taskList)
      setReminders(reminderList)
      setError(null)
    } catch (e) {
      if (seq !== refreshSeqRef.current || !mountedRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === refreshSeqRef.current && mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const showNotice = useCallback((message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(null), 4000)
  }, [])

  const handleCreate = async (input: NewTaskInput): Promise<void> => {
    await createTask({
      title: input.title,
      description: input.description,
      due_at: input.dueAt,
    })
    await refresh()
    showNotice(`已创建草稿任务「${input.title}」`)
  }

  const runAction = async (
    id: number,
    action: () => Promise<Task>,
    successMessage: string,
  ): Promise<void> => {
    setPendingIds((prev) => new Set(prev).add(id))
    try {
      await action()
      await refresh()
      showNotice(successMessage)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPendingIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  const handleConfirm = (task: Task): void => {
    const message =
      task.due_at !== null ? `已确认「${task.title}」并安排到期提醒` : `已确认「${task.title}」`
    void runAction(task.id, () => confirmTask(task.id), message)
  }

  const handleComplete = (task: Task): void => {
    void runAction(task.id, () => completeTask(task.id), `已完成「${task.title}」，未投递提醒已取消`)
  }

  const handlePostpone = (task: Task, dueAt: string): void => {
    void runAction(task.id, () => postponeTask(task.id, dueAt), `已将「${task.title}」延期，提醒时间已同步`)
  }

  const reminderByTask = (taskId: number): Reminder | undefined =>
    reminders.find((r) => r.task_id === taskId)

  return (
    <>
      {notice && (
        <div className="banner banner--success" aria-live="polite">
          {notice}
        </div>
      )}
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

      <TaskForm onCreate={handleCreate} />

      <TaskList
        tasks={tasks}
        loading={loading}
        pendingIds={pendingIds}
        reminderByTask={reminderByTask}
        onConfirm={handleConfirm}
        onComplete={handleComplete}
        onPostpone={handlePostpone}
      />
    </>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [unreadCount, setUnreadCount] = useState(0)
  const [showNotifications, setShowNotifications] = useState(false)

  const refreshUnread = useCallback(async () => {
    try {
      const unread = await getUnreadCount()
      setUnreadCount(unread.count)
    } catch {
      // 未读计数失败不影响主界面；面板内会显示具体错误
    }
  }, [])

  const selectTab = (nextTab: Tab): void => {
    setTab(nextTab)
    const url = new URL(window.location.href)
    if (nextTab === 'tasks') {
      // 「今天」成为默认页后，任务页通过 ?tab=tasks 寻址
      url.searchParams.set('tab', 'tasks')
      url.searchParams.delete('execution')
      url.searchParams.delete('calendar_execution')
    } else if (nextTab === 'executions' || nextTab === 'calendar' || nextTab === 'memories') {
      url.searchParams.set('tab', nextTab)
      if (nextTab !== 'executions') url.searchParams.delete('execution')
      url.searchParams.delete('calendar_execution')
    } else if (nextTab === 'chat') {
      url.searchParams.set('tab', 'chat')
      url.searchParams.delete('execution')
    } else {
      url.searchParams.delete('tab')
      url.searchParams.delete('execution')
      url.searchParams.delete('calendar_execution')
    }
    window.history.replaceState(null, '', url)
  }

  useEffect(() => {
    // setTimeout 链而非 setInterval：上一次请求完成后才开始下一次计时，
    // 慢请求永远不会与下一次轮询重叠。
    let cancelled = false
    let timer: number | undefined
    const schedule = (delay: number): void => {
      timer = window.setTimeout(() => {
        void refreshUnread().finally(() => {
          if (!cancelled) schedule(15000)
        })
      }, delay)
    }
    void refreshUnread().finally(() => {
      if (!cancelled) schedule(15000)
    })
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [refreshUnread])

  return (
    <div className="app">
      <header className="app-header app-header--row">
        <div>
          <h1>JARVIS</h1>
          <p className="app-subtitle">任务管理 · LLM 对话 · 提醒通知 · 文档摄取</p>
        </div>
        <button
          type="button"
          className="notification-bell"
          aria-label={`通知中心，${unreadCount} 条未读`}
          onClick={() => setShowNotifications((prev) => !prev)}
        >
          🔔
          {unreadCount > 0 && <span className="notification-badge">{unreadCount}</span>}
        </button>
      </header>

      <nav className="tabs" role="tablist" aria-label="功能页签">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'today'}
          className={`tab-btn${tab === 'today' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('today')}
        >
          今天
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'tasks'}
          className={`tab-btn${tab === 'tasks' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('tasks')}
        >
          任务
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'chat'}
          className={`tab-btn${tab === 'chat' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('chat')}
        >
          对话
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'documents'}
          className={`tab-btn${tab === 'documents' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('documents')}
        >
          文档
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'executions'}
          className={`tab-btn${tab === 'executions' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('executions')}
        >
          执行
        </button>
        <button type="button" role="tab" aria-selected={tab === 'calendar'}
          className={`tab-btn${tab === 'calendar' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('calendar')}>日历</button>
        <button type="button" role="tab" aria-selected={tab === 'memories'}
          className={`tab-btn${tab === 'memories' ? ' tab-btn--active' : ''}`}
          onClick={() => selectTab('memories')}>记忆</button>
      </nav>

      {showNotifications && (
        <NotificationCenter
          onClose={() => setShowNotifications(false)}
          onUnreadChange={setUnreadCount}
        />
      )}

      {tab === 'today' ? <TodayView /> : tab === 'memories' ? <MemoryPanel /> : tab === 'calendar' ? <CalendarPanel /> : tab === 'tasks' ? (
        <TaskSection />
      ) : tab === 'documents' ? (
        <DocumentsView />
      ) : tab === 'executions' ? (
        <ExecutionPanel />
      ) : (
        <ChatView />
      )}
    </div>
  )
}
