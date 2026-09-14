import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, calendarConnectUrl, calendarWriteAuthorizeUrl, calendarConnection, calendarEvents, defaultCalendar, disconnectCalendar } from '../../api'
import { formatDue, localDatetimeToIso, toLocalDatetimeValue } from '../../datetime'
import type { CalendarConnection, CalendarEvent, CalendarMetadata } from '../../types'

const availability = { free: '空闲', tentative: '暂定', busy: '忙碌', oof: '外出', workingElsewhere: '异地工作', unknown: '状态未知' }

export default function CalendarPanel() {
  const [connection, setConnection] = useState<CalendarConnection | null>(null)
  const [calendar, setCalendar] = useState<CalendarMetadata | null>(null)
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [start, setStart] = useState(() => { const d = new Date(); d.setHours(0, 0, 0, 0); return toLocalDatetimeValue(d.toISOString()) })
  const [end, setEnd] = useState(() => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + 7); return toLocalDatetimeValue(d.toISOString()) })
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  const guard = useRef(false)

  const fail = (e: unknown) => e instanceof ApiError
    ? `${e.message}${e.code ? ` · ${e.code}` : ''}${e.correlationId ? ` · 请求 ${e.correlationId}` : ''}`
    : '无法读取日历，请重试。'

  const refresh = useCallback(async () => {
    if (guard.current) return
    guard.current = true
    const seq = ++sequence.current
    setLoading(true); setError(''); setEvents([]); setLoaded(false)
    try {
      const status = await calendarConnection()
      if (seq !== sequence.current) return
      setConnection(status)
      if (!status.connected) { setCalendar(null); return }
      const a = new Date(start), b = new Date(end)
      if (!Number.isFinite(a.getTime()) || !Number.isFinite(b.getTime()) || b <= a || b.getTime() - a.getTime() > 31 * 86400000) {
        setError('请选择有效时间范围：结束晚于开始，且不超过 31 天。'); return
      }
      const [meta, range] = await Promise.all([defaultCalendar(), calendarEvents(localDatetimeToIso(start), localDatetimeToIso(end))])
      if (seq !== sequence.current) return
      setCalendar(meta); setEvents(range.events); setLoaded(true)
    } catch (e) { if (seq === sequence.current) { setError(fail(e)); if (e instanceof ApiError && e.status === 401) setConnection(null) } }
    finally { guard.current = false; if (seq === sequence.current) setLoading(false) }
  }, [start, end])

  useEffect(() => {
    let active = true
    const counter = sequence
    const seq = counter.current
    void calendarConnection().then(s => { if (active && seq === counter.current) setConnection(s) }).catch(e => { if (active && seq === counter.current) setError(fail(e)) })
    return () => { active = false; counter.current++ }
  }, [])

  const disconnect = async () => {
    if (guard.current) return
    guard.current = true; setLoading(true); setError('')
    const seq = ++sequence.current
    try {
      const status = await disconnectCalendar()
      if (seq === sequence.current) { setConnection(status); setEvents([]); setCalendar(null); setLoaded(false) }
    } catch (e) { if (seq === sequence.current) setError(fail(e)) }
    finally { guard.current = false; if (seq === sequence.current) setLoading(false) }
  }

  const changeRange = (value: string, which: 'start' | 'end') => {
    if (which === 'start') setStart(value); else setEnd(value)
    setEvents([]); setLoaded(false)
  }

  return <section className="calendar-panel" aria-label="Outlook 日历">
    <h2>Outlook 日历</h2>
    <p>只读日历 · 时间按当前浏览器时区显示 · 最多查询 31 天</p>
    <p role="status">{connection?.connected ? `已连接 · ${connection.account_hint || 'Microsoft Outlook'}` : '未连接'}{calendar ? ` · ${calendar.name}` : ''}</p>
    {connection?.connected && <p>读取权限：已授权 · 写入权限：{connection.write_authorized ? '已授权' : '未授权'}</p>}
    {connection && !connection.configured && <p>需要先在后端配置 Microsoft 应用的 Client ID。</p>}
    <div className="calendar-actions">
      <form action={calendarConnectUrl} method="post" target="_blank" rel="noopener">
        <button type="submit" disabled={loading || !connection?.configured}>连接 Outlook</button>
      </form>
      {connection?.connected && !connection.write_authorized && <form action={calendarWriteAuthorizeUrl} method="post" target="_blank" rel="noopener">
        <button type="submit" disabled={loading}>启用日历写入权限</button>
      </form>}
      {connection?.connected && <button type="button" disabled={loading} onClick={() => void disconnect()}>断开本地连接</button>}
    </div>
    <p>授权将在 Microsoft 页面完成。完成后返回此页点击刷新。断开连接只清除本地授权凭据。</p>
    <div className="calendar-range">
      <label>开始时间<input type="datetime-local" value={start} disabled={loading} onChange={e => changeRange(e.target.value, 'start')} /></label>
      <label>结束时间<input type="datetime-local" value={end} disabled={loading} onChange={e => changeRange(e.target.value, 'end')} /></label>
      <button type="button" disabled={loading} onClick={() => void refresh()}>{loading ? '读取中…' : '刷新日历'}</button>
    </div>
    {error && <p role="alert" className="banner banner--error">{error}</p>}
    {loading && <p role="status">正在读取日历…</p>}
    {!loading && loaded && events.length === 0 && <p>所选范围没有日历事件。</p>}
    {!loading && loaded && events.length > 0 && <p role="status">已读取 {events.length} 个事件。占用状态来自 Outlook，不包含自动排程。</p>}
    <ul className="calendar-events">{events.map(event => <li key={event.external_event_id}>
      <strong>{event.title}</strong>{event.cancelled && <span> · 已取消</span>}
      <p>{event.is_all_day ? `全天 · ${new Date(event.start_at).toLocaleDateString('zh-CN')} — ${new Date(event.end_at).toLocaleDateString('zh-CN')}（结束边界不含）` : `${formatDue(event.start_at)} — ${formatDue(event.end_at)}`}</p>
      <p>{availability[event.show_as]}{event.location ? ` · ${event.location}` : ''}</p>
    </li>)}</ul>
  </section>
}
