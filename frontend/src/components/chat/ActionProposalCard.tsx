import { formatDue } from '../../datetime'
import type { ActionProposal } from '../../types'

interface Props {
  request: string
  proposal: ActionProposal
}

const STATUS_LABELS: Record<ActionProposal['status'], string> = {
  READY: '信息完整',
  NEEDS_CLARIFICATION: '需要补充信息',
  UNSUPPORTED: '当前不支持',
  INVALID: '安全校验未通过',
}

const ACTION_LABELS = {
  create_task: '创建任务提案',
  create_task_with_reminder: '任务与提醒提案',
  create_calendar_event: 'Outlook 日历事件提案',
} as const

export default function ActionProposalCard({ request, proposal }: Props) {
  return (
    <article className={`action-proposal action-proposal--${proposal.status.toLowerCase()}`}>
      <div className="action-proposal-header">
        <strong>{proposal.action ? ACTION_LABELS[proposal.action] : '行动提案'}</strong>
        <span className="action-proposal-status">{STATUS_LABELS[proposal.status]}</span>
      </div>

      <p className="action-proposal-request">你的请求：{request}</p>

      {proposal.task && (
        <dl className="action-proposal-fields">
          <div>
            <dt>Task title</dt>
            <dd>{proposal.task.title ?? '待补充'}</dd>
          </div>
          <div>
            <dt>Task due_at</dt>
            <dd>{proposal.task.due_at ? formatDue(proposal.task.due_at) : '未设置'}</dd>
          </div>
          {proposal.reminder && (
            <div>
              <dt>Reminder remind_at</dt>
              <dd>{proposal.reminder.remind_at ? formatDue(proposal.reminder.remind_at) : '待补充'}</dd>
            </div>
          )}
        </dl>
      )}

      {proposal.calendar_event && (
        <dl className="action-proposal-fields">
          <div><dt>事件标题</dt><dd>{proposal.calendar_event.title ?? '待补充'}</dd></div>
          <div><dt>开始</dt><dd>{proposal.calendar_event.start_at ? formatDue(proposal.calendar_event.start_at) : '待补充'}</dd></div>
          <div><dt>结束</dt><dd>{proposal.calendar_event.end_at ? formatDue(proposal.calendar_event.end_at) : '待补充'}</dd></div>
          <div><dt>地点</dt><dd>{proposal.calendar_event.location ?? '未设置'}</dd></div>
        </dl>
      )}

      {proposal.missing_fields.length > 0 && (
        <p className="action-proposal-missing">
          缺少：{proposal.missing_fields.join('、')}
        </p>
      )}
      {proposal.clarification_question && (
        <p className="action-proposal-question">{proposal.clarification_question}</p>
      )}
      <p className="action-proposal-explanation">{proposal.explanation}</p>
      <p className="action-proposal-readonly">
        只读预览 · 预览本身不会执行；只有 READY 提案下方的明确确认才会创建内容
      </p>
    </article>
  )
}
