import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, createMemory, deleteMemory, getMemory, listMemories, updateMemory } from '../../api'
import type { MemoryCategory, MemoryScalar, MemoryValue, PersonalMemory } from '../../types'

const categories: Record<MemoryCategory, string> = { preference: '偏好', stable_fact: '稳定背景', constraint: '固定限制' }
type ValueType = 'text' | 'number' | 'boolean' | 'list' | 'object'
type Cell = { name: string; type: 'text' | 'number' | 'boolean'; text: string }
const blankCell = (): Cell => ({ name: '', type: 'text', text: '' })
const cellFrom = (value: MemoryScalar, name = ''): Cell => ({
  name, type: typeof value === 'string' ? 'text' : typeof value === 'number' ? 'number' : 'boolean', text: String(value),
})
function scalar(cell: Cell): MemoryScalar {
  if (cell.type === 'text') return cell.text
  if (cell.type === 'boolean') return cell.text === 'true'
  if (!cell.text.trim() || !Number.isFinite(Number(cell.text))) throw new Error('请输入有效的有限数字。')
  const number = Number(cell.text)
  if (Number.isInteger(number) && !Number.isSafeInteger(number)) throw new Error('数字超出此界面的精确整数范围。')
  return number
}

export default function MemoryPanel() {
  const [items, setItems] = useState<PersonalMemory[]>([])
  const [filter, setFilter] = useState<MemoryCategory | ''>('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState<PersonalMemory | null>(null)
  const [category, setCategory] = useState<MemoryCategory>('preference')
  const [key, setKey] = useState('')
  const [type, setType] = useState<ValueType>('text')
  const [cells, setCells] = useState<Cell[]>([blankCell()])
  const [conflict, setConflict] = useState(false)
  const [deleting, setDeleting] = useState<PersonalMemory | null>(null)
  const sequence = useRef(0)
  const mounted = useRef(true)
  const mutation = useRef(false)

  const refresh = useCallback(async () => {
    const seq = ++sequence.current
    setLoading(true)
    try {
      const result = await listMemories(filter || undefined)
      if (mounted.current && seq === sequence.current) setItems(result)
    } catch (e) {
      if (mounted.current && seq === sequence.current) setError(e instanceof Error ? e.message : '读取失败')
    } finally {
      if (mounted.current && seq === sequence.current) setLoading(false)
    }
  }, [filter])
  useEffect(() => { mounted.current = true; void refresh(); return () => { mounted.current = false } }, [refresh])

  function reset() {
    setEditing(null); setCategory('preference'); setKey(''); setType('text'); setCells([blankCell()]); setConflict(false)
  }
  function edit(item: PersonalMemory) {
    setEditing(item); setCategory(item.category); setKey(item.key); setConflict(false); setError('')
    const value = item.value
    if (Array.isArray(value)) { setType('list'); setCells(value.map(v => cellFrom(v))) }
    else if (typeof value === 'object') { setType('object'); setCells(Object.entries(value).map(([k, v]) => cellFrom(v, k))) }
    else { const cell = cellFrom(value); setType(cell.type); setCells([cell]) }
  }
  async function run(action: () => Promise<void>) {
    if (mutation.current) return
    mutation.current = true; setBusy(true); setError(''); setNotice('')
    try { await action() }
    catch (e) {
      if (mounted.current) {
        if (e instanceof ApiError && e.code === 'MEMORY_VERSION_CONFLICT') {
          setConflict(true); setError('这条记忆已被修改。请重新读取，再决定是否保存。')
        } else setError(e instanceof Error ? e.message : '操作失败')
      }
    } finally { mutation.current = false; if (mounted.current) setBusy(false) }
  }
  function value(): MemoryValue {
    if (type === 'list') return cells.map(scalar)
    if (type === 'object') {
      if (cells.some(c => !c.name.trim()) || new Set(cells.map(c => c.name)).size !== cells.length) throw new Error('字段名称不能为空或重复。')
      return Object.fromEntries(cells.map(c => [c.name, scalar(c)]))
    }
    return scalar(cells[0])
  }
  function patchCell(index: number, patch: Partial<Cell>) { setCells(prev => prev.map((c, i) => i === index ? { ...c, ...patch } : c)) }

  return <section className="memory-panel" aria-label="个人记忆管理">
    <h2>个人记忆</h2>
    <p>保存相对稳定的偏好、个人背景和固定限制，例如「通常晚上学习」或「喜欢 45 分钟学习时段」。目前仅在此处管理，不会自动加入对话。</p>
    <p className="form-hint">任务是否完成请查看任务；提醒是否发送请查看提醒；会议时间以 Microsoft 日历为准；执行结果和文档内容请查看对应页面。</p>
    <p className="form-hint">请勿保存密码、API 密钥或验证码。</p>
    {error && <p className="banner banner--error" role="alert">{error}</p>}
    {notice && <p className="banner banner--success" role="status">{notice}</p>}
    <form className="task-form memory-form" onSubmit={e => {
      e.preventDefault()
      if (conflict) return
      void run(async () => {
        const payload = { category, key, value: value() }
        if (new TextEncoder().encode(JSON.stringify(payload.value)).length > 4096) throw new Error('内容超过 4 KiB，请缩短。')
        if (editing) await updateMemory(editing.id, payload, editing.version)
        else await createMemory(payload)
        if (!mounted.current) return
        reset(); setNotice('记忆已保存。'); await refresh()
      })
    }}>
      <h3>{editing ? `编辑：${editing.key}` : '新建记忆'}</h3>
      <fieldset disabled={busy}>
        <label>类别<select value={category} onChange={e => setCategory(e.target.value as MemoryCategory)}>{Object.entries(categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>标识名称<input required maxLength={80} pattern="[a-z][a-z0-9_]{0,79}" value={key} onChange={e => setKey(e.target.value)} placeholder="preferred_study_time" /></label>
        <p className="form-hint">使用小写英文字母开头，可含数字和下划线，最多 80 个字符。</p>
        <label>内容类型<select value={type} onChange={e => {
          const next = e.target.value as ValueType; setType(next); setCells([{ ...blankCell(), type: next === 'number' || next === 'boolean' ? next : 'text', text: next === 'boolean' ? 'false' : '' }])
        }}><option value="text">文字</option><option value="number">数字</option><option value="boolean">是 / 否</option><option value="list">列表</option><option value="object">字段组</option></select></label>
        {cells.map((cell, i) => <div className="memory-value-row" key={i}>
          {type === 'object' && <input aria-label={`字段名称 ${i + 1}`} required maxLength={80} value={cell.name} onChange={e => patchCell(i, { name: e.target.value })} />}
          {(type === 'list' || type === 'object') && <select aria-label={`条目类型 ${i + 1}`} value={cell.type} onChange={e => patchCell(i, { type: e.target.value as Cell['type'], text: e.target.value === 'boolean' ? 'false' : '' })}><option value="text">文字</option><option value="number">数字</option><option value="boolean">是 / 否</option></select>}
          {cell.type === 'boolean' ? <select aria-label={`内容 ${i + 1}`} value={cell.text} onChange={e => patchCell(i, { text: e.target.value })}><option value="true">是</option><option value="false">否</option></select>
            : cell.type === 'text' ? <textarea aria-label={`内容 ${i + 1}`} maxLength={4096} rows={2} value={cell.text} onChange={e => patchCell(i, { text: e.target.value })} />
              : <input aria-label={`内容 ${i + 1}`} type="number" step="any" required value={cell.text} onChange={e => patchCell(i, { text: e.target.value })} />}
          {(type === 'list' || type === 'object') && <button type="button" className="btn" onClick={() => setCells(cells.filter((_, n) => n !== i))}>移除条目 {i + 1}</button>}
        </div>)}
        {(type === 'list' || type === 'object') && <button type="button" className="btn" disabled={cells.length >= (type === 'list' ? 64 : 32)} onClick={() => setCells([...cells, blankCell()])}>添加条目</button>}
        <div className="memory-actions">
          <button className="btn btn--primary" type="submit" disabled={conflict}>{busy ? '处理中…' : '保存记忆'}</button>
          {editing && <button type="button" className="btn" onClick={reset}>取消编辑</button>}
          {conflict && editing && <button type="button" className="btn" onClick={() => void run(async () => { const latest = await getMemory(editing.id); if (mounted.current) edit(latest) })}>重新读取最新内容</button>}
        </div>
      </fieldset>
    </form>
    <div className="memory-actions"><label>筛选类别<select disabled={busy} value={filter} onChange={e => setFilter(e.target.value as MemoryCategory | '')}><option value="">全部</option>{Object.entries(categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label><button type="button" className="btn" disabled={busy || loading} onClick={() => void refresh()}>刷新列表</button></div>
    {loading ? <p role="status">读取记忆中…</p> : items.length === 0 ? <p>暂无记忆。可在上方明确保存一条。</p> : <ul className="memory-list">{items.map(item => <li className="task-form" key={item.id}>
      <h3>{item.key}</h3><p>{categories[item.category]} · 来源：手动（{item.source_type}） · 版本 {item.version}</p>
      <pre>{typeof item.value === 'string' ? item.value : JSON.stringify(item.value, null, 2)}</pre>
      <p className="form-hint">更新于 <time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleString()}</time></p>
      <div className="memory-actions"><button type="button" className="btn" disabled={busy} onClick={() => edit(item)}>编辑 {item.key}</button><button type="button" className="btn" disabled={busy} onClick={() => setDeleting(item)}>删除 {item.key}</button></div>
    </li>)}</ul>}
    {deleting && <div className="memory-confirm" role="alertdialog" aria-modal="false" aria-labelledby="memory-delete-title" aria-describedby="memory-delete-description">
      <h3 id="memory-delete-title">删除 {deleting.key}？</h3>
      <p id="memory-delete-description">这会从 JARVIS 中移除该记忆。无法撤销。</p>
      <div className="memory-actions"><button autoFocus type="button" className="btn" disabled={busy} onClick={() => setDeleting(null)}>取消</button><button type="button" className="btn" disabled={busy} onClick={() => void run(async () => {
        await deleteMemory(deleting.id)
        if (!mounted.current) return
        if (editing?.id === deleting.id) reset()
        setDeleting(null); setNotice('记忆已删除。'); await refresh()
      })}>确认删除</button></div>
    </div>}
  </section>
}
