/**
 * The History tab: a filterable, paged list of past runs with a detail panel,
 * a two-run compare mode and an NDJSON export.
 *
 * Row selection state (detail vs. compare) lives here rather than in the
 * store — it is purely a view concern, and keeping it local means switching
 * tabs away and back does not need to remember what was open.
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Input,
  Select,
  SkeletonLoader,
  StatusBadge
} from '@readysetcloud/ui'
import { statusTone } from '../../components/status'
import { useNotify } from '../../components/notify'
import { api } from '../../api'
import type { Page, RunDetail, RunSummary } from '../../api'
import {
  selectHasMore,
  selectNeedsRefresh,
  useHistoryStore,
  useModelStore,
  type HistoryFilters
} from '../../stores'
import CompareView from './CompareView'
import RunDetailView from './RunDetailView'

const STATUS_OPTIONS = ['completed', 'error', 'cancelled']

function formatTs(ts: string): string {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString()
}

function formatTokens(run: RunSummary): string {
  const total = run.metrics?.total_tokens
  return total === undefined || total === null ? '—' : total.toLocaleString()
}

/* -------------------------------------------------------------------------- */
/* Filter bar                                                                 */
/* -------------------------------------------------------------------------- */

function FilterBar({ filters }: { filters: HistoryFilters }) {
  const setFilters = useHistoryStore(state => state.setFilters)
  const models = useModelStore(state => state.models)
  const modelsLoaded = useModelStore(state => state.modelsLoaded)

  function setModel(value: string) {
    void setFilters({ model_id: value === '' ? null : value })
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {modelsLoaded && models.length > 0 ? (
        <Select
          label="Model"
          data-testid="history-filter-model"
          value={filters.model_id ?? ''}
          onChange={event => setModel(event.target.value)}
        >
          <option value="">All models</option>
          {models.map(model => (
            <option key={model.model_id} value={model.model_id}>
              {model.name}
            </option>
          ))}
        </Select>
      ) : (
        <Input
          label="Model"
          data-testid="history-filter-model"
          type="text"
          placeholder="model id…"
          value={filters.model_id ?? ''}
          onChange={event => setModel(event.target.value)}
        />
      )}

      <Select
        label="Status"
        data-testid="history-filter-status"
        value={filters.status ?? ''}
        onChange={event =>
          void setFilters({ status: event.target.value === '' ? null : event.target.value })
        }
      >
        <option value="">All statuses</option>
        {STATUS_OPTIONS.map(status => (
          <option key={status} value={status}>
            {status}
          </option>
        ))}
      </Select>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Row                                                                        */
/* -------------------------------------------------------------------------- */

interface RunRowProps {
  run: RunSummary
  selected: boolean
  compareChecked: boolean
  compareDisabled: boolean
  onSelect: () => void
  onToggleCompare: (checked: boolean) => void
}

function RunRow({
  run,
  selected,
  compareChecked,
  compareDisabled,
  onSelect,
  onToggleCompare
}: RunRowProps) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const remove = useHistoryStore(state => state.remove)
  const loading = useHistoryStore(state => state.loading)
  const notify = useNotify()

  async function handleDelete() {
    setDeleting(true)
    try {
      await remove(run.id)
    } finally {
      setDeleting(false)
      setConfirmingDelete(false)
    }
    // `remove` reports failure through the store's `error` (shown above the
    // table) rather than throwing, so success is "the row is gone".
    if (!useHistoryStore.getState().items.some(item => item.id === run.id)) {
      notify('Run deleted', { variant: 'success' })
    }
  }

  return (
    <tr
      className={`border-b border-border last:border-0 cursor-pointer hover:bg-muted ${
        selected ? 'bg-primary-50' : ''
      }`}
      data-testid="history-row"
      data-run-id={run.id}
      onClick={onSelect}
    >
      <td className="py-2 pr-3" onClick={event => event.stopPropagation()}>
        <input
          type="checkbox"
          aria-label={`Compare run ${run.id}`}
          data-testid="history-compare-checkbox"
          className="h-4 w-4 rounded border-border text-primary-600"
          checked={compareChecked}
          disabled={compareDisabled}
          onChange={event => onToggleCompare(event.target.checked)}
        />
      </td>
      <td className="py-2 pr-4 text-sm text-muted-foreground whitespace-nowrap">
        {formatTs(run.ts)}
      </td>
      <td className="py-2 pr-4 text-sm font-mono text-foreground break-all">{run.model_id}</td>
      <td className="py-2 pr-4">
        <StatusBadge tone={statusTone(run.status)} role={undefined}>
          {run.status}
        </StatusBadge>
      </td>
      <td className="py-2 pr-4 text-sm text-muted-foreground tabular-nums">{formatTokens(run)}</td>
      <td className="py-2 pl-2 text-right" onClick={event => event.stopPropagation()}>
        {confirmingDelete ? (
          <div className="flex items-center justify-end gap-2">
            <span className="text-xs text-muted-foreground">Delete?</span>
            <Button
              variant="error"
              size="sm"
              data-testid="history-delete-confirm"
              disabled={loading}
              loading={deleting}
              onClick={() => void handleDelete()}
            >
              Confirm
            </Button>
            <Button
              variant="ghost"
              size="sm"
              data-testid="history-delete-cancel"
              disabled={deleting}
              onClick={() => setConfirmingDelete(false)}
            >
              Cancel
            </Button>
          </div>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            data-testid="history-delete-btn"
            aria-label={`Delete run ${run.id}`}
            onClick={() => setConfirmingDelete(true)}
          >
            Delete
          </Button>
        )}
      </td>
    </tr>
  )
}

/* -------------------------------------------------------------------------- */
/* Export                                                                     */
/* -------------------------------------------------------------------------- */

function ExportButton({ filters }: { filters: HistoryFilters }) {
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)
  const notify = useNotify()

  async function handleExport() {
    setExporting(true)
    setExportError(null)
    const rows: RunDetail[] = []
    try {
      await api.runs.exportAll(filters, { onEvent: run => rows.push(run) })
      const ndjson = rows.map(run => JSON.stringify(run)).join('\n') + (rows.length > 0 ? '\n' : '')
      const blob = new Blob([ndjson], { type: 'application/x-ndjson' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'llm-eval-harness-runs.ndjson'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
      notify(`Exported ${rows.length.toLocaleString()} ${rows.length === 1 ? 'run' : 'runs'}`, {
        variant: 'success'
      })
    } catch (error) {
      setExportError(error instanceof Error ? error.message : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <Button
        variant="secondary"
        size="sm"
        data-testid="history-export-btn"
        loading={exporting}
        loadingLabel="Exporting…"
        onClick={() => void handleExport()}
      >
        Export NDJSON
      </Button>
      {exportError && (
        <Alert variant="error" className="text-xs">
          {exportError}
        </Alert>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Loading placeholder                                                        */
/* -------------------------------------------------------------------------- */

/** Skeleton rows for a first load; the visually hidden status line is what screen readers get. */
function ListSkeleton({ text, testId }: { text: string; testId: string }) {
  return (
    <div data-testid={testId}>
      <span className="sr-only" role="status">
        {text}
      </span>
      <SkeletonLoader count={3} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Cloud runs                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * The "Cloud runs" filter reads `GET /runs?execution=cloud` directly (like
 * `ExportButton` above, it bypasses `historyStore` — those rows come from the
 * DynamoDB `RUN` GSI1 partition, not the SQLite-backed store this page's main
 * list/paging is built around). Additive to the model/status filters:
 * whichever of those is set is carried onto the cloud query too.
 */
function CloudRunsPanel({ filters }: { filters: HistoryFilters }) {
  const [items, setItems] = useState<RunSummary[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Bumped by "Try again" after a failed first page, to re-run the fetch effect.
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api.runs
      .list({ ...filters, execution: 'cloud' })
      .then((page: Page<RunSummary>) => {
        if (cancelled) return
        setItems(page.items)
        setNextCursor(page.next_cursor)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not load cloud runs')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [filters, reloadKey])

  async function handleLoadMore() {
    if (!nextCursor || loading) return
    setLoading(true)
    setError(null)
    try {
      const page = await api.runs.list({ ...filters, execution: 'cloud', cursor: nextCursor })
      setItems(current => [...current, ...page.items])
      setNextCursor(page.next_cursor)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load cloud runs')
    } finally {
      setLoading(false)
    }
  }

  function retry() {
    // A failed first page reloads it; a failed "Load more" retries that page.
    if (items.length > 0 && nextCursor) void handleLoadMore()
    else setReloadKey(key => key + 1)
  }

  return (
    <section aria-labelledby="cloud-runs-heading" data-testid="cloud-runs-panel">
      <Card>
        <CardHeader>
          <h2 id="cloud-runs-heading" className="card-title">
            Cloud runs
          </h2>
          <p className="mt-1 text-xs text-muted-foreground" data-testid="cloud-runs-note">
            These are cloud-lane evaluation runs — executed on Bedrock AgentCore and persisted to
            your AWS account&apos;s DynamoDB table, not this machine&apos;s local history.
          </p>
        </CardHeader>
        <CardBody>
          {error && (
            <ErrorState
              className="mb-3"
              heading="Could not load cloud runs"
              message={error}
              action={{ label: 'Try again', onClick: retry }}
            />
          )}

          {loading && items.length === 0 && (
            <ListSkeleton text="Loading cloud runs…" testId="cloud-runs-loading" />
          )}

          {!loading && items.length === 0 && !error && (
            <div data-testid="cloud-runs-empty">
              <EmptyState title="No cloud runs match these filters." />
            </div>
          )}

          {items.length > 0 && (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-left" data-testid="cloud-runs-table">
                  <thead>
                    <tr className="border-b border-border text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      <th className="py-2 pr-4">Time</th>
                      <th className="py-2 pr-4">Model</th>
                      <th className="py-2 pr-4">Status</th>
                      <th className="py-2 pr-4">Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map(run => (
                      <tr
                        key={run.id}
                        className="border-b border-border last:border-0"
                        data-testid="cloud-run-row"
                      >
                        <td className="py-2 pr-4 text-sm text-muted-foreground whitespace-nowrap">
                          {formatTs(run.ts)}
                        </td>
                        <td className="py-2 pr-4 text-sm font-mono text-foreground break-all">
                          {run.model_id}
                        </td>
                        <td className="py-2 pr-4">
                          <StatusBadge tone={statusTone(run.status)} role={undefined}>
                            {run.status}
                          </StatusBadge>
                        </td>
                        <td className="py-2 pr-4 text-sm text-muted-foreground tabular-nums">
                          {formatTokens(run)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {nextCursor && (
                <div className="mt-4 flex justify-center">
                  <Button
                    variant="secondary"
                    size="sm"
                    data-testid="cloud-runs-load-more-btn"
                    loading={loading}
                    loadingLabel="Loading…"
                    onClick={() => void handleLoadMore()}
                  >
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </CardBody>
      </Card>
    </section>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

interface HistoryPageProps {
  /** A run to open on arrival (the `#/runs/<id>` link). */
  runId?: string | null
  /** Told when the open run changes, so the address bar can follow. */
  onSelectRun?: (runId: string | null) => void
}

export default function HistoryPage({ runId = null, onSelectRun }: HistoryPageProps = {}) {
  const items = useHistoryStore(state => state.items)
  const loading = useHistoryStore(state => state.loading)
  const loaded = useHistoryStore(state => state.loaded)
  const error = useHistoryStore(state => state.error)
  const filters = useHistoryStore(state => state.filters)
  const needsRefresh = useHistoryStore(selectNeedsRefresh)
  const hasMore = useHistoryStore(selectHasMore)
  const loadFirstPage = useHistoryStore(state => state.loadFirstPage)
  const loadMore = useHistoryStore(state => state.loadMore)

  const loadModels = useModelStore(state => state.loadModels)

  const [selectedRunId, setSelectedRunId] = useState<string | null>(runId)

  // A link followed while already on this tab (e.g. a run link on an
  // evaluation, or the back button) moves the selection with it.
  useEffect(() => {
    setSelectedRunId(runId)
  }, [runId])

  function selectRun(id: string | null) {
    setSelectedRunId(id)
    onSelectRun?.(id)
  }
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [cloudRunsFilter, setCloudRunsFilter] = useState(false)

  useEffect(() => {
    if (needsRefresh) void loadFirstPage()
  }, [needsRefresh, loadFirstPage])

  useEffect(() => {
    void loadModels()
  }, [loadModels])

  function toggleCompare(runId: string, checked: boolean) {
    setCompareIds(current => {
      if (checked) return current.includes(runId) ? current : [...current, runId].slice(-2)
      return current.filter(id => id !== runId)
    })
  }

  const comparing = compareIds.length === 2

  return (
    <div className="space-y-4" data-testid="history-page">
      <section aria-labelledby="history-heading">
        <Card>
          <CardHeader className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="history-heading" className="card-title">
              Run history
            </h2>
            <div className="flex flex-wrap items-center gap-3">
              {/* A lone inline toggle: the package's Field stacks its label
                  above the control, which does not suit a toolbar checkbox. */}
              <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  data-testid="history-cloud-filter"
                  className="h-4 w-4 rounded border-border text-primary-600"
                  checked={cloudRunsFilter}
                  onChange={event => setCloudRunsFilter(event.target.checked)}
                />
                Cloud runs
              </label>
              <Button
                variant="secondary"
                size="sm"
                data-testid="history-refresh-btn"
                disabled={loading}
                onClick={() => void loadFirstPage()}
              >
                Refresh
              </Button>
              <ExportButton filters={filters} />
            </div>
          </CardHeader>

          <CardBody>
            <div className="mb-4">
              <FilterBar filters={filters} />
            </div>

            {error && (
              <ErrorState
                className="mb-3"
                heading="Could not load runs"
                message={error.message}
                action={{ label: 'Try again', onClick: () => void loadFirstPage() }}
              />
            )}

            {loading && items.length === 0 && (
              <ListSkeleton text="Loading runs…" testId="history-loading" />
            )}

            {loaded && !loading && items.length === 0 && !error && (
              <div data-testid="history-empty">
                <EmptyState
                  title="No runs match these filters."
                  description="Runs from the Workbench appear here once they finish."
                />
              </div>
            )}

            {items.length > 0 && (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-left" data-testid="history-table">
                    <thead>
                      <tr className="border-b border-border text-xs font-medium text-muted-foreground uppercase tracking-wide">
                        <th className="py-2 pr-3">Compare</th>
                        <th className="py-2 pr-4">Time</th>
                        <th className="py-2 pr-4">Model</th>
                        <th className="py-2 pr-4">Status</th>
                        <th className="py-2 pr-4">Tokens</th>
                        <th className="py-2 pl-2 text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map(run => (
                        <RunRow
                          key={run.id}
                          run={run}
                          selected={selectedRunId === run.id}
                          compareChecked={compareIds.includes(run.id)}
                          compareDisabled={comparing && !compareIds.includes(run.id)}
                          onSelect={() => selectRun(selectedRunId === run.id ? null : run.id)}
                          onToggleCompare={checked => toggleCompare(run.id, checked)}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>

                {hasMore && (
                  <div className="mt-4 flex justify-center">
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid="history-load-more-btn"
                      loading={loading}
                      loadingLabel="Loading…"
                      onClick={() => void loadMore()}
                    >
                      Load more
                    </Button>
                  </div>
                )}
              </>
            )}
          </CardBody>
        </Card>
      </section>

      {cloudRunsFilter && <CloudRunsPanel filters={filters} />}

      {comparing ? (
        <CompareView runIds={[compareIds[0], compareIds[1]]} onClose={() => setCompareIds([])} />
      ) : (
        selectedRunId && <RunDetailView key={selectedRunId} runId={selectedRunId} />
      )}
    </div>
  )
}
