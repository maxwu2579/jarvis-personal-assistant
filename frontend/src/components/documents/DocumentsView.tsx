import { useCallback, useEffect, useRef, useState } from 'react'
import {
  askRag,
  deleteDocument,
  indexDocument,
  listDocumentChunks,
  listDocuments,
  reindexDocument,
  searchDocuments,
  uploadDocument,
} from '../../api'
import type {
  Document,
  DocumentChunk,
  RAGAnswerResponse,
  SearchItem,
  SearchMode,
} from '../../types'
import {
  DOCUMENT_STATUS_LABELS,
  RAG_STATUS_LABELS,
  RETRIEVAL_STATUS_LABELS,
  SEARCH_MODE_LABELS,
  formatBytes,
} from '../../types'

const ACCEPT = '.txt,.md,.pdf,.docx'

const TOP_K_OPTIONS = [5, 10, 20]

// RAG 提问的后端上限是 top_k ≤ 10
const RAG_TOP_K_OPTIONS = [3, 5, 10]

function formatScore(value: number): string {
  return value.toFixed(4)
}

export default function DocumentsView() {
  const [documents, setDocuments] = useState<Document[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [chunks, setChunks] = useState<DocumentChunk[]>([])
  const [chunksLoading, setChunksLoading] = useState(false)
  const [pendingIds, setPendingIds] = useState<Set<number>>(new Set())
  const [indexingIds, setIndexingIds] = useState<Set<number>>(new Set())
  // ---- 检索（Phase 4B） ----
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<SearchMode>('hybrid')
  const [topK, setTopK] = useState(5)
  const [scope, setScope] = useState<'all' | number>('all')
  const [results, setResults] = useState<SearchItem[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  // ---- RAG 问答（Phase 4C） ----
  const [ragQuestion, setRagQuestion] = useState('')
  const [ragMode, setRagMode] = useState<SearchMode>('hybrid')
  const [ragTopK, setRagTopK] = useState(5)
  const [ragScope, setRagScope] = useState<'all' | number>('all')
  const [ragAnswer, setRagAnswer] = useState<RAGAnswerResponse | null>(null)
  const [ragAsking, setRagAsking] = useState(false)
  const [ragError, setRagError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 竞态防护：卸载或慢请求返回后不得覆盖新状态
  const mountedRef = useRef(true)
  const refreshSeqRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current
    try {
      const list = await listDocuments()
      if (seq !== refreshSeqRef.current || !mountedRef.current) return
      setDocuments(list)
      setError(null)
    } catch (e) {
      if (seq !== refreshSeqRef.current || !mountedRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === refreshSeqRef.current && mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const showNotice = useCallback((message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(null), 4000)
  }, [])

  const handleUpload = async (): Promise<void> => {
    const input = fileInputRef.current
    const file = input?.files?.[0]
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const doc = await uploadDocument(file)
      await refresh()
      if (input) input.value = ''
      if (doc.status === 'FAILED') {
        showNotice(`「${doc.original_filename}」上传成功但解析失败`)
      } else {
        showNotice(`已上传并处理「${doc.original_filename}」`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  const handleExpand = async (doc: Document): Promise<void> => {
    if (expandedId === doc.id) {
      setExpandedId(null)
      setChunks([])
      return
    }
    setExpandedId(doc.id)
    setChunksLoading(true)
    setError(null)
    try {
      const chunkList = await listDocumentChunks(doc.id)
      if (expandedId === doc.id) return // 期间已收起
      setChunks(chunkList)
    } catch (e) {
      setExpandedId(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setChunksLoading(false)
    }
  }

  const handleDelete = async (doc: Document): Promise<void> => {
    setPendingIds((prev) => new Set(prev).add(doc.id))
    try {
      await deleteDocument(doc.id)
      if (expandedId === doc.id) {
        setExpandedId(null)
        setChunks([])
      }
      await refresh()
      showNotice(`已删除「${doc.original_filename}」`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPendingIds((prev) => {
        const next = new Set(prev)
        next.delete(doc.id)
        return next
      })
    }
  }

  const handleIndex = async (doc: Document): Promise<void> => {
    setIndexingIds((prev) => new Set(prev).add(doc.id))
    setError(null)
    try {
      const result = await indexDocument(doc.id)
      await refresh()
      showNotice(
        `「${doc.original_filename}」已建立索引（${result.embeddings_created} 块，跳过 ${result.embeddings_skipped}）`,
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setIndexingIds((prev) => {
        const next = new Set(prev)
        next.delete(doc.id)
        return next
      })
    }
  }

  const handleReindex = async (doc: Document): Promise<void> => {
    if (!window.confirm(`确定重建「${doc.original_filename}」的检索索引吗？`)) return
    setIndexingIds((prev) => new Set(prev).add(doc.id))
    setError(null)
    try {
      const result = await reindexDocument(doc.id)
      await refresh()
      showNotice(`「${doc.original_filename}」索引已重建（${result.embeddings_created} 块）`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setIndexingIds((prev) => {
        const next = new Set(prev)
        next.delete(doc.id)
        return next
      })
    }
  }

  const handleSearch = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault()
    if (!query.trim()) return
    setSearching(true)
    setSearchError(null)
    try {
      const response = await searchDocuments({
        query: query.trim(),
        mode,
        topK,
        documentIds: scope === 'all' ? undefined : [scope],
      })
      setResults(response.items)
    } catch (err) {
      setResults(null)
      setSearchError(err instanceof Error ? err.message : String(err))
    } finally {
      setSearching(false)
    }
  }

  const scopeLabel = (docId: number | 'all'): string =>
    docId === 'all'
      ? '全部文档'
      : documents.find((d) => d.id === docId)?.original_filename ?? `#${docId}`

  // RAG 范围下拉只列已索引文档（后端对未索引文档返回 409）
  const indexedDocuments = documents.filter(
    (d) => d.status === 'READY' && d.retrieval_status === 'INDEXED',
  )

  const handleAsk = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault()
    if (!ragQuestion.trim()) return
    setRagAsking(true)
    setRagError(null)
    try {
      const response = await askRag({
        question: ragQuestion.trim(),
        mode: ragMode,
        topK: ragTopK,
        documentIds: ragScope === 'all' ? undefined : [ragScope],
      })
      setRagAnswer(response)
    } catch (err) {
      setRagAnswer(null)
      setRagError(err instanceof Error ? err.message : String(err))
    } finally {
      setRagAsking(false)
    }
  }


  return (
    <section className="documents">
      {notice && (
        <div className="banner banner--success" aria-live="polite">
          {notice}
        </div>
      )}
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

      <form
        className="doc-upload"
        onSubmit={(e) => {
          e.preventDefault()
          void handleUpload()
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPT}
          disabled={uploading}
          aria-label="选择文档（txt/md/pdf/docx）"
        />
        <button type="submit" className="btn btn--primary" disabled={uploading}>
          {uploading ? '上传处理中…' : '上传'}
        </button>
      </form>
      <p className="form-hint">
        支持 .txt / .md / .pdf（仅文本）/ .docx；单文件最大 10MB，拒绝宏文档与 ZIP
        炸弹。
      </p>

      {/* ---- 检索面板（Phase 4B） ---- */}
      <form className="search-panel" onSubmit={(e) => void handleSearch(e)}>
        <input
          className="search-query"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索文档内容（支持中英文）…"
          aria-label="搜索关键词"
          maxLength={1000}
        />
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as SearchMode)}
          aria-label="检索模式"
        >
          {(Object.keys(SEARCH_MODE_LABELS) as SearchMode[]).map((m) => (
            <option key={m} value={m}>
              {SEARCH_MODE_LABELS[m]}
            </option>
          ))}
        </select>
        <select
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value))}
          aria-label="返回条数"
        >
          {TOP_K_OPTIONS.map((n) => (
            <option key={n} value={n}>
              前 {n} 条
            </option>
          ))}
        </select>
        <select
          value={scope === 'all' ? 'all' : String(scope)}
          onChange={(e) =>
            setScope(e.target.value === 'all' ? 'all' : Number(e.target.value))
          }
          aria-label="搜索范围"
        >
          <option value="all">全部文档</option>
          {documents
            .filter((d) => d.status === 'READY')
            .map((d) => (
              <option key={d.id} value={String(d.id)}>
                {d.original_filename}
              </option>
            ))}
        </select>
        <button type="submit" className="btn btn--primary" disabled={searching}>
          {searching ? '检索中…' : '检索'}
        </button>
      </form>
      <p className="form-hint">
        Keyword：FTS5 + BM25；Vector：本地词汇哈希向量（LocalHash，非语义模型）；Hybrid：RRF
        分数融合。分数越大越相关。
      </p>

      {searchError && (
        <div className="banner banner--error" role="alert">
          {searchError}
        </div>
      )}
      {results !== null && (
        <div className="search-results">
          <h3 className="search-results-title">
            {results.length > 0
              ? `找到 ${results.length} 个结果${scopeLabel(scope) !== '全部文档' ? `（范围：${scopeLabel(scope)}）` : ''}`
              : '没有匹配结果'}
          </h3>
          {results.length > 0 && (
            <ul className="search-items">
              {results.map((item) => (
                <li key={item.chunk_id} className="search-item">
                  <div className="search-item-meta">
                    <span className="search-item-doc">{item.document_title}</span>
                    <span>块 #{item.chunk_index}</span>
                    <span>
                      字符 {item.char_start}–{item.char_end}
                    </span>
                    <span className="search-item-score">
                      综合 {formatScore(item.final_score)} · 关键词{' '}
                      {formatScore(item.keyword_score)} · 向量{' '}
                      {formatScore(item.vector_score)}
                    </span>
                  </div>
                  <p className="search-item-content">{item.content}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ---- RAG 问答面板（Phase 4C） ---- */}
      <form className="rag-panel" onSubmit={(e) => void handleAsk(e)}>
        <input
          className="rag-query"
          type="text"
          value={ragQuestion}
          onChange={(e) => setRagQuestion(e.target.value)}
          placeholder="向文档提问（答案仅基于检索到的证据）…"
          aria-label="RAG 问题"
          maxLength={2000}
        />
        <select
          value={ragMode}
          onChange={(e) => setRagMode(e.target.value as SearchMode)}
          aria-label="RAG 检索模式"
        >
          {(Object.keys(SEARCH_MODE_LABELS) as SearchMode[]).map((m) => (
            <option key={m} value={m}>
              {SEARCH_MODE_LABELS[m]}
            </option>
          ))}
        </select>
        <select
          value={ragTopK}
          onChange={(e) => setRagTopK(Number(e.target.value))}
          aria-label="RAG 检索条数"
        >
          {RAG_TOP_K_OPTIONS.map((n) => (
            <option key={n} value={n}>
              候选 {n} 块
            </option>
          ))}
        </select>
        <select
          value={ragScope === 'all' ? 'all' : String(ragScope)}
          onChange={(e) =>
            setRagScope(e.target.value === 'all' ? 'all' : Number(e.target.value))
          }
          aria-label="RAG 文档范围"
        >
          <option value="all">全部已索引文档</option>
          {indexedDocuments.map((d) => (
            <option key={d.id} value={String(d.id)}>
              {d.original_filename}
            </option>
          ))}
        </select>
        <button type="submit" className="btn btn--primary" disabled={ragAsking}>
          {ragAsking ? '回答中…' : '提问'}
        </button>
      </form>
      <p className="form-hint">
        答案仅基于检索到的证据块；证据不足时拒绝作答。引用（C1、C2……）由后端
        分配并指向真实文档块，模型无法伪造引用位置。
      </p>

      {ragError && (
        <div className="banner banner--error" role="alert">
          <span>{ragError}</span>
          <button
            type="button"
            className="banner-close"
            aria-label="关闭错误提示"
            onClick={() => setRagError(null)}
          >
            ×
          </button>
        </div>
      )}
      {ragAnswer !== null && (
        <div className="rag-answer">
          <div className="rag-answer-header">
            <span
              className={`badge badge--rag badge--${ragAnswer.status.toLowerCase()}`}
            >
              {RAG_STATUS_LABELS[ragAnswer.status]}
            </span>
            <span className="rag-answer-meta">
              候选 {ragAnswer.retrieval.candidate_count} · 入上下文{' '}
              {ragAnswer.retrieval.returned_count} 块
              {ragAnswer.usage.model !== null &&
                ` · ${ragAnswer.usage.model}`}
              {ragAnswer.usage.latency_ms !== null &&
                ` · ${(ragAnswer.usage.latency_ms / 1000).toFixed(2)}s`}
            </span>
          </div>
          <p className="rag-answer-text">{ragAnswer.answer}</p>
          {ragAnswer.status === 'INSUFFICIENT_EVIDENCE' && (
            <p className="rag-answer-note">
              没有足够证据：后端固定拒答文案，未调用模型（usage.model 为空）。
            </p>
          )}
          {ragAnswer.citations.length > 0 && (
            <>
              <h4 className="rag-citations-title">引用证据（{ragAnswer.citations.length}）</h4>
              <ul className="rag-citations">
                {ragAnswer.citations.map((citation) => (
                  <li key={citation.citation_id} className="rag-citation">
                    <div className="rag-citation-meta">
                      <span className="rag-citation-id">
                        {citation.citation_id}
                      </span>
                      <span className="rag-citation-doc">
                        {citation.document_title}
                      </span>
                      <span>块 #{citation.chunk_index}</span>
                      <span>
                        字符 {citation.char_start}–{citation.char_end}
                      </span>
                      <span className="rag-citation-score">
                        相关度 {formatScore(citation.retrieval_score)}
                      </span>
                    </div>
                    {/* quote 按纯文本渲染（React 默认转义），绝不插入原始 HTML */}
                    <p className="rag-citation-quote">{citation.quote}</p>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {loading ? (
        <p className="doc-list-status" aria-busy="true">
          加载中…
        </p>
      ) : documents.length === 0 ? (
        <p className="doc-list-status">还没有文档，从上方选择一个文件开始。</p>
      ) : (
        <ul className="doc-items">
          {documents.map((doc) => (
            <li key={doc.id} className="doc-item">
              <div className="doc-item-header">
                <div className="doc-item-main">
                  <span className="doc-item-title">{doc.original_filename}</span>
                  <span className={`badge badge--${doc.status.toLowerCase()}`}>
                    {DOCUMENT_STATUS_LABELS[doc.status]}
                  </span>
                  <span
                    className={`badge badge--retrieval badge--${doc.retrieval_status.toLowerCase()}`}
                    title={`已索引 ${doc.indexed_chunk_count} 个块`}
                  >
                    {RETRIEVAL_STATUS_LABELS[doc.retrieval_status]}
                    {doc.retrieval_status === 'INDEXED' &&
                      `（${doc.indexed_chunk_count}）`}
                  </span>
                </div>
                <div className="doc-item-meta">
                  <span>{formatBytes(doc.size_bytes)}</span>
                  <span>{doc.sha256.slice(0, 12)}…</span>
                </div>
              </div>

              {doc.error_message && (
                <p className="doc-item-error" role="alert">
                  {doc.error_message}
                </p>
              )}

              <div className="doc-item-actions">
                {doc.status === 'READY' && (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => void handleExpand(doc)}
                    disabled={pendingIds.has(doc.id)}
                  >
                    {expandedId === doc.id ? '收起块预览' : '查看块预览'}
                  </button>
                )}
                {doc.status === 'READY' && doc.retrieval_status !== 'INDEXED' && (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => void handleIndex(doc)}
                    disabled={indexingIds.has(doc.id) || pendingIds.has(doc.id)}
                  >
                    {indexingIds.has(doc.id)
                      ? '索引中…'
                      : doc.retrieval_status === 'INDEX_FAILED'
                        ? '重试索引'
                        : '建立索引'}
                  </button>
                )}
                {doc.retrieval_status === 'INDEXED' && (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => void handleReindex(doc)}
                    disabled={indexingIds.has(doc.id) || pendingIds.has(doc.id)}
                  >
                    {indexingIds.has(doc.id) ? '重建中…' : '重建索引'}
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => void handleDelete(doc)}
                  disabled={pendingIds.has(doc.id) || indexingIds.has(doc.id)}
                >
                  {pendingIds.has(doc.id) ? '删除中…' : '删除'}
                </button>
              </div>

              {expandedId === doc.id &&
                (chunksLoading ? (
                  <p className="doc-chunks-status">块加载中…</p>
                ) : (
                  <ul className="doc-chunks">
                    {chunks.map((chunk) => (
                      <li key={chunk.id} className="doc-chunk">
                        <div className="doc-chunk-meta">
                          <span>#{chunk.chunk_index}</span>
                          <span>字符 {chunk.char_start}–{chunk.char_end}</span>
                          <span>≈{chunk.token_estimate} tokens</span>
                        </div>
                        <p className="doc-chunk-content">{chunk.content}</p>
                      </li>
                    ))}
                    {chunks.length === 0 && (
                      <li className="doc-chunk-empty">该文档没有可预览的块</li>
                    )}
                  </ul>
                ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
