import type { ExecutionStatus } from '../../types'
import { EXECUTION_STATUS_LABELS } from '../../types'

export default function ExecutionStatusBadge({ status }: { status: ExecutionStatus }) {
  return (
    <span className={`badge execution-status execution-status--${status.toLowerCase()}`}>
      {EXECUTION_STATUS_LABELS[status]}
    </span>
  )
}
