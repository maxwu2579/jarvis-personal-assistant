import { useState } from 'react'
import { localDatetimeToIso } from '../datetime'
import type { NewTaskInput } from '../types'

interface TaskFormProps {
  onCreate: (input: NewTaskInput) => Promise<void>
}

export default function TaskForm({ onCreate }: TaskFormProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [dueAt, setDueAt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSubmit = title.trim().length > 0 && !submitting

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await onCreate({
        title: title.trim(),
        description: description.trim(),
        dueAt: dueAt ? localDatetimeToIso(dueAt) : null,
      })
      setTitle('')
      setDescription('')
      setDueAt('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="task-form" onSubmit={handleSubmit}>
      <div className="task-form-row">
        <input
          className="task-form-title"
          type="text"
          placeholder="要做什么？（例如：周五前提交周报）"
          aria-label="任务标题"
          value={title}
          maxLength={200}
          disabled={submitting}
          onChange={(e) => setTitle(e.target.value)}
        />
        <button type="submit" className="btn btn--primary" disabled={!canSubmit}>
          {submitting ? '创建中…' : '创建草稿'}
        </button>
      </div>
      <div className="task-form-row task-form-row--bottom">
        <textarea
          className="task-form-desc"
          placeholder="描述（可选）"
          aria-label="任务描述（可选）"
          rows={2}
          value={description}
          disabled={submitting}
          onChange={(e) => setDescription(e.target.value)}
        />
        <label className="task-form-due">
          截止时间（可选）
          <input
            type="datetime-local"
            value={dueAt}
            disabled={submitting}
            onChange={(e) => setDueAt(e.target.value)}
          />
        </label>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <p className="form-hint">创建的任务始终是「草稿」；确认后才能完成或延期。</p>
    </form>
  )
}
