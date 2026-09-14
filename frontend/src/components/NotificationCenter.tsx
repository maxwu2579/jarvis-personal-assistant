import { useCallback, useEffect, useRef, useState } from 'react'
import { getUnreadCount, listNotifications, markNotificationRead } from '../api'
import { formatDue } from '../datetime'
import type { Notification } from '../types'

interface NotificationCenterProps {
  onClose: () => void
  onUnreadChange: (count: number) => void
}

export default function NotificationCenter({ onClose, onUnreadChange }: NotificationCenterProps) {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [markingId, setMarkingId] = useState<number | null>(null)
  // 组件卸载后不再 setState（React 18 不再警告，但避免无谓更新与内存泄漏）
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      // 服务端分页上限 200：一次取满，最近的通知优先（created_at desc）
      const [items, unread] = await Promise.all([listNotifications({ limit: 200 }), getUnreadCount()])
      if (!mountedRef.current) return
      setNotifications(items)
      onUnreadChange(unread.count)
      setError(null)
    } catch (e) {
      if (!mountedRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [onUnreadChange])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleMarkRead = async (notification: Notification): Promise<void> => {
    if (markingId !== null || notification.read_at !== null) return
    setMarkingId(notification.id)
    try {
      await markNotificationRead(notification.id)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarkingId(null)
    }
  }

  return (
    <div className="notification-center" role="dialog" aria-label="通知中心">
      <div className="notification-center-header">
        <h3>通知中心</h3>
        <button type="button" className="banner-close" aria-label="关闭通知中心" onClick={onClose}>
          ×
        </button>
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="banner-close"
            aria-label="关闭错误提示"
            onClick={() => setError(null)}
          >
            ×
          </button>
        </div>
      )}

      {loading && <p className="notification-empty">加载中…</p>}
      {!loading && notifications.length === 0 && (
        <p className="notification-empty">暂无通知</p>
      )}
      {!loading &&
        notifications.map((notification) => (
          <div
            key={notification.id}
            className={`notification-item${notification.read_at === null ? ' notification-item--unread' : ''}`}
          >
            <div className="notification-item-meta">
              <span className="notification-item-title">{notification.title}</span>
              <span className="notification-item-time">{formatDue(notification.created_at)}</span>
            </div>
            <p className="notification-item-body">{notification.body}</p>
            {notification.read_at === null ? (
              <button
                type="button"
                className="btn btn--ghost"
                disabled={markingId === notification.id}
                onClick={() => void handleMarkRead(notification)}
              >
                {markingId === notification.id ? '处理中…' : '标记已读'}
              </button>
            ) : (
              <span className="notification-item-read">已读</span>
            )}
          </div>
        ))}
    </div>
  )
}
