import type { Execution, ExecutionStatus } from '../../types'
import { EXECUTION_STATUS_LABELS } from '../../types'
import { formatDue } from '../../datetime'
import ExecutionStatusBadge from './ExecutionStatusBadge'

interface Props {
  executions: Execution[]
  selectedId: number | null
  initialLoading: boolean
  refreshing: boolean
  error: string | null
  status: ExecutionStatus | ''
  executionType: string
  page: number
  pageSize: number
  onStatusChange: (status: ExecutionStatus | '') => void
  onTypeChange: (executionType: string) => void
  onPageChange: (page: number) => void
  onSelect: (id: number) => void
  onRefresh: () => void
}

const STATUSES = Object.keys(EXECUTION_STATUS_LABELS) as ExecutionStatus[]

export default function ExecutionList(props: Props) {
  const hasNext = props.executions.length === props.pageSize

  return (
    <section className="execution-list-panel">
      <div className="execution-section-heading">
        <div>
          <h2>执行列表</h2>
          <p>{props.refreshing ? '正在刷新后端状态…' : '按真实 Execution 状态浏览'}</p>
        </div>
        <button
          type="button"
          className="btn btn--ghost"
          disabled={props.initialLoading || props.refreshing}
          onClick={props.onRefresh}
        >
          刷新
        </button>
      </div>

      <div className="execution-filters">
        <label className="execution-field">
          <span>Status</span>
          <select
            value={props.status}
            onChange={(event) => props.onStatusChange(event.target.value as ExecutionStatus | '')}
          >
            <option value="">全部状态</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {EXECUTION_STATUS_LABELS[status]} ({status})
              </option>
            ))}
          </select>
        </label>
        <label className="execution-field">
          <span>Execution Type</span>
          <select
            value={props.executionType}
            onChange={(event) => props.onTypeChange(event.target.value)}
          >
            <option value="">全部类型</option>
            <option value="reminder.send">reminder.send</option>
            <option value="documents.reindex">documents.reindex</option>
            <option value="system.ping">system.ping</option>
          </select>
        </label>
      </div>

      {props.error && (
        <div className="banner banner--error" role="alert">
          {props.executions.length > 0
            ? `刷新失败，以下为旧数据：${props.error}`
            : props.error}
        </div>
      )}

      {props.initialLoading ? (
        <p className="execution-state" aria-busy="true">初次加载中…</p>
      ) : !props.error && props.executions.length === 0 ? (
        <p className="execution-state">当前筛选条件下没有 Execution。</p>
      ) : (
        <ul className="execution-list">
          {props.executions.map((execution) => (
            <li key={execution.id}>
              <button
                type="button"
                className={`execution-list-item${props.selectedId === execution.id ? ' execution-list-item--active' : ''}`}
                onClick={() => props.onSelect(execution.id)}
              >
                <span className="execution-list-main">
                  <strong>{execution.execution_type}</strong>
                  <ExecutionStatusBadge status={execution.status} />
                </span>
                <span className="execution-list-meta">
                  <span>#{execution.id}</span>
                  <span>创建 {formatDue(execution.created_at)}</span>
                  <span>计划 {execution.run_at ? formatDue(execution.run_at) : '立即'}</span>
                  <span>Correlation {execution.correlation_id ?? '—'}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="execution-pagination">
        <button
          type="button"
          className="btn btn--ghost"
          disabled={props.page === 0 || props.initialLoading}
          onClick={() => props.onPageChange(props.page - 1)}
        >
          上一页
        </button>
        <span>第 {props.page + 1} 页</span>
        <button
          type="button"
          className="btn btn--ghost"
          disabled={!hasNext || props.initialLoading}
          onClick={() => props.onPageChange(props.page + 1)}
        >
          下一页
        </button>
      </div>
    </section>
  )
}
