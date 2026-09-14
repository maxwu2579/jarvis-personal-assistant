import { STATUS_LABELS, type TaskStatus } from '../types'

export default function StatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`badge badge--${status.toLowerCase()}`}>{STATUS_LABELS[status]}</span>
}
