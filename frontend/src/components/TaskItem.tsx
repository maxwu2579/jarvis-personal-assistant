import { useState } from 'react'
import { formatDue, localDatetimeToIso, toLocalDatetimeValue } from '../datetime'
import { REMINDER_STATUS_LABELS, type Reminder, type Task } from '../types'
import StatusBadge from './StatusBadge'

interface TaskItemProps {
  task: Task
  reminder: Reminder | undefined
  pending: boolean
  onConfirm: (task: Task) => void
  onComplete: (task: Task) => void
  onPostpone: (task: Task, dueAt: string) => void
}

export default function TaskItem({
  task,
  reminder,
  pending,
  onConfirm,
  onComplete,
  onPostpone,
}: TaskItemProps) {
  const [postponing, setPostponing] = useState(false)
  const [newDue, setNewDue] = useState('')

  const isOverdue =
    task.due_at !== null && task.status !== 'DONE' && new Date(task.due_at) < new Date()

  const startPostpone = (): void => {
    setNewDue(task.due_at ? toLocalDatetimeValue(task.due_at) : '')
    setPostponing(true)
  }

  const submitPostpone = (e: React.FormEvent<HTMLFormElement>): void => {
    e.preventDefault()
    if (!newDue || pending) return
    onPostpone(task, localDatetimeToIso(newDue))
    setPostponing(false)
  }

  return (
    <li className={`task-item task-item--${task.status.toLowerCase()}`}>
      <div className="task-item-header">
        <span className="task-item-title">{task.title}</span>
        <StatusBadge status={task.status} />
      </div>

      {task.description && <p className="task-item-desc">{task.description}</p>}

      <div className="task-item-meta">
        {task.due_at && (
          <span className={isOverdue ? 'task-item-due task-item-due--overdue' : 'task-item-due'}>
            {isOverdue ? '已过期 · ' : '截止 '}
            {formatDue(task.due_at)}
          </span>
        )}
        {reminder && task.status !== 'DONE' && (
          <span className="task-item-reminder">
            提醒：{REMINDER_STATUS_LABELS[reminder.status]} · {formatDue(reminder.remind_at)}
          </span>
        )}
        {reminder && task.status === 'DONE' && reminder.status === 'CANCELLED' && (
          <span className="task-item-reminder">提醒已取消（任务完成）</span>
        )}
        <span className="task-item-created">创建于 {formatDue(task.created_at)}</span>
      </div>

      {postponing ? (
        <form className="postpone-form" onSubmit={submitPostpone}>
          <label className="postpone-label">
            新的截止时间
            <input
              type="datetime-local"
              value={newDue}
              required
              onChange={(e) => setNewDue(e.target.value)}
            />
          </label>
          <button type="submit" className="btn btn--primary" disabled={!newDue || pending}>
            保存新截止时间
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={pending}
            onClick={() => setPostponing(false)}
          >
            取消
          </button>
        </form>
      ) : (
        <div className="task-item-actions">
          {task.status === 'DRAFT' && (
            <button
              type="button"
              className="btn btn--primary"
              disabled={pending}
              onClick={() => onConfirm(task)}
            >
              {pending
                ? '处理中…'
                : task.due_at !== null
                  ? '确认并安排到期提醒'
                  : '确认'}
            </button>
          )}
          {task.status === 'CONFIRMED' && (
            <>
              <button
                type="button"
                className="btn btn--primary"
                disabled={pending}
                onClick={() => onComplete(task)}
              >
                {pending ? '处理中…' : '完成'}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={pending}
                onClick={startPostpone}
              >
                延期
              </button>
            </>
          )}
          {task.status === 'DONE' && <span className="task-item-done-note">已归档</span>}
        </div>
      )}
    </li>
  )
}
