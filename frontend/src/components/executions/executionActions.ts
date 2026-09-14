import type { Execution, ExecutionStep } from '../../types'

const CANCELLABLE = new Set<Execution['status']>([
  'CREATED',
  'QUEUED',
  'CLAIMED',
  'RUNNING',
])

const CONFIRMABLE_EXECUTIONS = new Set<Execution['status']>([
  'QUEUED',
  'CLAIMED',
  'RUNNING',
])

const DECIDABLE_STEPS = new Set<ExecutionStep['status']>([
  'PENDING',
  'RUNNING',
  'FAILED',
])

export function canEnqueue(execution: Execution): boolean {
  return execution.status === 'CREATED'
}

export function canCancel(execution: Execution): boolean {
  return CANCELLABLE.has(execution.status)
}

export function canRetry(execution: Execution): boolean {
  return execution.status === 'FAILED'
}

export function canDecideStep(execution: Execution, step: ExecutionStep): boolean {
  return (
    CONFIRMABLE_EXECUTIONS.has(execution.status) &&
    DECIDABLE_STEPS.has(step.status) &&
    step.requires_confirmation &&
    (step.confirmation_status === 'PENDING' || step.confirmation_status === 'EXPIRED')
  )
}

export function isTerminalExecution(execution: Execution): boolean {
  return ['SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'].includes(execution.status)
}
