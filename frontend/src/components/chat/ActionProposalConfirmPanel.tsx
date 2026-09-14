import { formatDue } from '../../datetime'
import { calendarConnectUrl, calendarWriteAuthorizeUrl } from '../../api'
import type { ActionProposal, ActionProposalConfirmation, ExecutionDetail } from '../../types'
import ActionProposalCard from './ActionProposalCard'

interface Props {
  request: string
  proposal: ActionProposal
  result?: ActionProposalConfirmation
  confirming: boolean
  onConfirm: () => void
  writeAuthorized: boolean
  readAuthorized: boolean
  onRefreshWriteAuth: () => void
  executionDetail?: ExecutionDetail
}

export default function ActionProposalConfirmPanel({
  request,
  proposal,
  result,
  confirming,
  onConfirm,
  writeAuthorized,
  readAuthorized,
  onRefreshWriteAuth,
  executionDetail,
}: Props) {
  return (
    <div className="action-confirm-panel">
      <ActionProposalCard request={request} proposal={proposal} />

      {result && (
        <section className="action-confirm-result" aria-label="行动确认结果">
          <strong>{result.execution ? (result.replayed ? '已恢复原日历执行' : '日历创建任务已排队') : (result.replayed ? '已恢复原确认结果' : '创建成功')}</strong>
          {result.task && <p>
            Task #{result.task.id} — {result.task.title}（{result.task.status}）
          </p>}
          {result.task?.due_at && <p>截止时间：{formatDue(result.task.due_at)}</p>}
          {result.reminder && (
            <p>
              Reminder #{result.reminder.id} — {formatDue(result.reminder.remind_at)}
            </p>
          )}
          {result.execution && <p>Execution #{result.execution.id} · {executionDetail?.status ?? result.execution.status}</p>}
          {executionDetail?.result && <p>Outlook 已确认：{executionDetail.result.title} · {formatDue(executionDetail.result.start_at)} — {formatDue(executionDetail.result.end_at)}{executionDetail.result.location ? ` · ${executionDetail.result.location}` : ''}</p>}
          {executionDetail?.last_error_message && <p role="alert">{executionDetail.last_error_message}</p>}
        </section>
      )}

      {!result && proposal.status === 'READY' && (
        <div className="action-confirm-controls">
          <p>
            {proposal.action === 'create_calendar_event'
              ? '确认后将排队创建 1 个 Outlook 日历事件'
              : proposal.action === 'create_task_with_reminder'
              ? '确认后将创建 1 个 Task + 1 个 Reminder'
              : '确认后将创建 1 个 Task'}
          </p>
          {proposal.action === 'create_calendar_event' && !writeAuthorized && <>
            <p>{readAuthorized ? '需要授权 Outlook 日历写入权限。' : '请先连接 Outlook 只读日历，再授权写入权限。'}</p>
            <form action={readAuthorized ? calendarWriteAuthorizeUrl : calendarConnectUrl} method="post" target="_blank" rel="noopener">
              <button type="submit">{readAuthorized ? '启用日历写入权限' : '连接 Outlook'}</button>
            </form>
            <button type="button" onClick={onRefreshWriteAuth}>刷新权限状态</button>
          </>}
          {proposal.action === 'create_calendar_event' ? (
            <button type="button" className="btn btn--primary"
              disabled={confirming || !writeAuthorized} onClick={onConfirm}>
              {confirming ? '确认处理中…' : '明确确认创建日历事件'}
            </button>
          ) : (
            <button type="button" className="btn btn--primary"
              disabled={confirming} onClick={onConfirm}>
              {confirming ? '确认处理中…' : '明确确认创建'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
