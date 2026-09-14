import type { Reminder, Task } from '../types'
import TaskItem from './TaskItem'

interface TaskListProps {
  tasks: Task[]
  loading: boolean
  pendingIds: Set<number>
  reminderByTask: (taskId: number) => Reminder | undefined
  onConfirm: (task: Task) => void
  onComplete: (task: Task) => void
  onPostpone: (task: Task, dueAt: string) => void
}

export default function TaskList({
  tasks,
  loading,
  pendingIds,
  reminderByTask,
  onConfirm,
  onComplete,
  onPostpone,
}: TaskListProps) {
  if (loading) {
    return (
      <section className="task-list" aria-busy="true">
        <p className="task-list-status">加载中…</p>
      </section>
    )
  }

  if (tasks.length === 0) {
    return (
      <section className="task-list">
        <p className="task-list-status">还没有任务，从上方创建一个草稿开始。</p>
      </section>
    )
  }

  return (
    <section className="task-list">
      <ul className="task-items">
        {tasks.map((task) => (
          <TaskItem
            key={task.id}
            task={task}
            reminder={reminderByTask(task.id)}
            pending={pendingIds.has(task.id)}
            onConfirm={onConfirm}
            onComplete={onComplete}
            onPostpone={onPostpone}
          />
        ))}
      </ul>
    </section>
  )
}
