import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { ApiError, cancelMemoryProposal, confirmMemoryProposal, generateMemoryProposal } from '../../api'
import type { MemoryCanonicalValue, MemoryConfirmResult, MemoryProposal, PersonalMemory } from '../../types'

// Phase 10B: explicit natural-language Memory proposal → preview → explicit confirm.
// Pending proposals live only in backend RAM (10 min). Nothing is kept in browser
// storage, so a refresh does not restore a preview.

export interface MemoryProposalHandle {
  /** Cancel any pending proposal (e.g. before manual edit/delete). */
  discard: () => void
}

type View =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; proposal: MemoryProposal; confirming: boolean; uncertain: boolean }
  | { kind: 'already'; memory: PersonalMemory | null }
  | { kind: 'clarify' | 'unsupported' | 'invalid' | 'conflict' | 'error'; message: string }
  | { kind: 'expired' }
  | { kind: 'applied'; result: MemoryConfirmResult }

const keyLabels: Record<string, string> = {
  preferred_study_time: '偏好学习时段',
  preferred_session_duration: '单次学习时长',
  usual_available_days: '通常可用日',
}
const categoryLabels: Record<string, string> = { preference: '偏好', stable_fact: '稳定背景', constraint: '固定限制' }
const periodLabels: Record<string, string> = { morning: '上午', afternoon: '下午', evening: '晚上' }
const dayLabels: Record<string, string> = { mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' }
const examples = ['记住我通常晚上学习', 'Remember that I prefer 45-minute study sessions.', '把我的通常可用日改为周一和周三']

function formatValue(key: string, value: MemoryCanonicalValue | null): string {
  if (value === null) return '（无）'
  if (key === 'preferred_study_time' && typeof value === 'string') return `${periodLabels[value] ?? value}（${value}）`
  if (key === 'preferred_session_duration' && typeof value === 'number') return `${value} 分钟`
  if (key === 'usual_available_days' && Array.isArray(value)) return value.map(d => dayLabels[d] ?? d).join('、')
  return JSON.stringify(value)
}

interface Props {
  disabled: boolean
  onApplied: () => void | Promise<void>
}

const MemoryProposalSection = forwardRef<MemoryProposalHandle, Props>(function MemoryProposalSection({ disabled, onApplied }, ref) {
  const [text, setText] = useState('')
  const [view, setView] = useState<View>({ kind: 'idle' })
  const token = useRef(0)                  // invalidates stale async responses
  const pendingId = useRef<string | null>(null)
  const confirming = useRef(false)
  const mounted = useRef(true)
  const appliedRef = useRef(onApplied)
  useEffect(() => { appliedRef.current = onApplied }, [onApplied])

  const cancelOnServer = useCallback((id: string) => {
    cancelMemoryProposal(id).then(result => {
      // An uncertain confirm may already have applied: the cancel reports that history.
      if (result.status === 'APPLIED' && mounted.current) void appliedRef.current()
    }).catch(() => { /* expires on its own after 10 minutes */ })
  }, [])

  const discard = useCallback(() => {
    token.current += 1
    const id = pendingId.current
    pendingId.current = null
    if (id) cancelOnServer(id)
    if (mounted.current) setView({ kind: 'idle' })
  }, [cancelOnServer])

  useImperativeHandle(ref, () => ({ discard }), [discard])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const id = pendingId.current
      pendingId.current = null
      if (id) cancelOnServer(id)
    }
  }, [cancelOnServer])

  async function generate() {
    const content = text.trim()
    if (!content || confirming.current) return
    discard()
    const mine = ++token.current
    setView({ kind: 'loading' })
    try {
      const result = await generateMemoryProposal(text)
      const stale = mine !== token.current || !mounted.current
      if (stale) {
        // A stale READY must never reactivate an old preview; release its slot.
        if (result.proposal) cancelOnServer(result.proposal.proposal_id)
        return
      }
      if (result.status === 'READY' && result.proposal) {
        pendingId.current = result.proposal.proposal_id
        setView({ kind: 'ready', proposal: result.proposal, confirming: false, uncertain: false })
      } else if (result.status === 'ALREADY_SAVED') {
        setView({ kind: 'already', memory: result.existing_memory })
      } else if (result.status === 'NEEDS_CLARIFICATION') {
        setView({ kind: 'clarify', message: result.message })
      } else if (result.status === 'UNSUPPORTED') {
        setView({ kind: 'unsupported', message: result.message })
      } else {
        setView({ kind: 'invalid', message: result.message })
      }
    } catch (e) {
      if (mine === token.current && mounted.current) {
        setView({ kind: 'error', message: e instanceof Error ? e.message : '生成提案失败' })
      }
    }
  }

  async function confirm(proposal: MemoryProposal) {
    if (confirming.current) return
    confirming.current = true
    const mine = token.current
    setView({ kind: 'ready', proposal, confirming: true, uncertain: false })
    try {
      const result = await confirmMemoryProposal(proposal.proposal_id)
      if (pendingId.current === proposal.proposal_id) pendingId.current = null
      if (mounted.current) {
        if (mine === token.current) setView({ kind: 'applied', result })
        void appliedRef.current()
      }
    } catch (e) {
      if (!mounted.current || mine !== token.current) return
      const status = e instanceof ApiError ? e.status : 0
      const code = e instanceof ApiError ? e.code : undefined
      if (status === 0 || status >= 500) {
        // Outcome unknown (e.g. response lost). Keep the SAME proposal ID for a retry.
        setView({ kind: 'ready', proposal, confirming: false, uncertain: true })
        return
      }
      pendingId.current = null
      if (status === 410) setView({ kind: 'expired' })
      else if (code === 'MEMORY_KEY_CONFLICT' || code === 'MEMORY_VERSION_CONFLICT' || code === 'MEMORY_TARGET_NOT_FOUND') {
        setView({ kind: 'conflict', message: e instanceof Error ? e.message : '记忆已变化' })
      } else setView({ kind: 'error', message: e instanceof Error ? e.message : '确认失败' })
    } finally {
      confirming.current = false
    }
  }

  const busy = view.kind === 'loading' || (view.kind === 'ready' && view.confirming)

  return <section className="task-form memory-proposal" aria-labelledby="memory-proposal-title">
    <h3 id="memory-proposal-title">用一句话提议保存记忆</h3>
    <p className="form-hint">目前仅支持三类记忆：偏好学习时段（上午 / 下午 / 晚上）、单次学习时长（5–180 分钟）、通常可用日（周一至周日）。每次只能包含一条。其他说法会被拒绝或要求澄清。</p>
    <p className="form-hint">示例：{examples.map((example, i) => <span key={example}>{i > 0 && ' · '}<code>{example}</code></span>)}</p>
    <p className="form-hint">隐私：生成提案时，仅把这一句话发送给已配置的语言模型服务用于解析；不会发送已保存的记忆、对话、文档、任务或日历。提案只在后端内存中保留 10 分钟，刷新页面或后端重启后会丢失；确认前不会保存任何内容。</p>
    <form className="memory-actions" onSubmit={e => { e.preventDefault(); void generate() }}>
      <label>记忆请求<input type="text" value={text} maxLength={1000} disabled={disabled || busy} placeholder={examples[0]}
        aria-label="记忆请求" onChange={e => {
          setText(e.target.value)
          if (view.kind !== 'idle') discard()   // any edit invalidates the old proposal
        }} /></label>
      <button className="btn btn--primary" type="submit" disabled={disabled || busy || !text.trim()}>{view.kind === 'loading' ? '生成中…' : '生成提案'}</button>
    </form>

    {view.kind === 'ready' && <div className="memory-proposal-preview" role="region" aria-label="记忆提案预览">
      <p><span className={`badge badge--${view.proposal.operation === 'CREATE' ? 'create' : 'update'}`}>{view.proposal.operation === 'CREATE' ? '新建 CREATE' : '更新 UPDATE'}</span> {keyLabels[view.proposal.key] ?? view.proposal.key}</p>
      <dl className="memory-proposal-fields">
        <dt>类别</dt><dd>{categoryLabels[view.proposal.category] ?? view.proposal.category}</dd>
        <dt>标识名称</dt><dd><code>{view.proposal.key}</code></dd>
        {view.proposal.operation === 'UPDATE'
          ? <><dt>内容</dt><dd>{formatValue(view.proposal.key, view.proposal.old_value)} → {formatValue(view.proposal.key, view.proposal.new_value)}</dd></>
          : <><dt>内容</dt><dd>{formatValue(view.proposal.key, view.proposal.new_value)}</dd></>}
        <dt>来源</dt><dd>用户确认（user_confirmed）</dd>
        <dt>有效期至</dt><dd><time dateTime={view.proposal.expires_at}>{new Date(view.proposal.expires_at).toLocaleString()}</time></dd>
      </dl>
      {view.uncertain && <p className="banner banner--error" role="alert">确认结果未知（可能是网络中断）。可以使用同一提案再次确认；系统不会重复保存。</p>}
      <div className="memory-actions">
        <button type="button" className="btn btn--primary" disabled={disabled || view.confirming}
          onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
          onClick={() => void confirm(view.proposal)}>{view.confirming ? '保存中…' : view.uncertain ? '重试确认保存' : '确认保存'}</button>
        <button type="button" className="btn" disabled={view.confirming} onClick={discard}>取消</button>
      </div>
    </div>}
    {view.kind === 'loading' && <p role="status">正在生成提案…</p>}
    {view.kind === 'already' && <p className="banner banner--success" role="status">这条记忆已保存且内容相同，未做任何更改。{view.memory && <> 当前：{keyLabels[view.memory.key] ?? view.memory.key} = {formatValue(view.memory.key, view.memory.value as MemoryCanonicalValue)}</>}</p>}
    {view.kind === 'clarify' && <p className="banner banner--error" role="alert">需要澄清：{view.message} 请按上方支持的格式重新表述，每次一条。</p>}
    {view.kind === 'unsupported' && <p className="banner banner--error" role="alert">不支持：{view.message}</p>}
    {view.kind === 'invalid' && <p className="banner banner--error" role="alert">无效：{view.message}</p>}
    {view.kind === 'conflict' && <p className="banner banner--error" role="alert">冲突：{view.message} 该提案已失效，请重新生成并确认。</p>}
    {view.kind === 'expired' && <p className="banner banner--error" role="alert">提案已过期或已取消，未保存任何内容。请重新生成。</p>}
    {view.kind === 'error' && <p className="banner banner--error" role="alert">{view.message}</p>}
    {view.kind === 'applied' && <p className="banner banner--success" role="status">
      {view.result.replayed
        ? `此提案此前已确认（历史记录：${view.result.operation === 'CREATE' ? '新建' : '更新'}，版本 ${view.result.applied_version}）。这不代表该记忆现在仍然存在，请以下方列表为准。`
        : `已保存（${view.result.operation === 'CREATE' ? '新建' : '更新'}，版本 ${view.result.applied_version}）。`}
    </p>}
  </section>
})

export default MemoryProposalSection
