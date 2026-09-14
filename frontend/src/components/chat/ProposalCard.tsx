import { formatDue } from '../../datetime'
import type { Task, TaskProposal } from '../../types'

interface ProposalCardProps {
  proposal: TaskProposal
  task: Task
  confirming: boolean
  onConfirm: (task: Task) => void
}

const TASK_STATUS_LABELS: Record<Task['status'], string> = {
  DRAFT: '草稿 · 尚未确认',
  CONFIRMED: '已确认',
  DONE: '已完成',
}

export default function ProposalCard({ proposal, task, confirming, onConfirm }: ProposalCardProps) {
  const args = proposal.arguments

  return (
    <div className={`proposal-card proposal-card--${task.status.toLowerCase()}`}>
      <div className="proposal-card-header">
        <span className="proposal-card-title">{args.title}</span>
        <span className={`badge badge--${task.status.toLowerCase()}`}>
          {TASK_STATUS_LABELS[task.status]}
        </span>
      </div>

      {args.description && <p className="proposal-card-desc">{args.description}</p>}

      <div className="proposal-card-meta">
        {args.due_at && <span>截止：{formatDue(args.due_at)}</span>}
        <span className="proposal-card-id">任务 #{task.id}</span>
      </div>

      <p className="proposal-card-explanation">{proposal.explanation}</p>

      {task.status === 'DRAFT' && (
        <button
          type="button"
          className="btn btn--primary"
          disabled={confirming}
          onClick={() => onConfirm(task)}
        >
          {confirming ? '确认中…' : '确认任务'}
        </button>
      )}
      {task.status !== 'DRAFT' && (
        <p className="proposal-card-note">任务已{task.status === 'CONFIRMED' ? '确认' : '完成'}，无更多操作。</p>
      )}
    </div>
  )
}
