/** 日期时间工具：与后端「due_at 必须带时区」约定配套。 */

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/**
 * 把 datetime-local 输入值（无时区、按浏览器本地时间解释）转换为
 * 带本地时区偏移的 ISO 8601 字符串（例如 2026-08-10T09:00:00+08:00）。
 * 后端拒绝无时区时间，因此必须显式携带偏移。
 */
export function localDatetimeToIso(value: string): string {
  const d = new Date(value)
  const offsetMin = -d.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMin)
  const local = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  return `${local}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** 把后端返回的 UTC ISO 字符串转成本地时间的 datetime-local 输入值。 */
export function toLocalDatetimeValue(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 本地化展示时间（UTC ISO → 本地格式）。 */
export function formatDue(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
