/**
 * The Evals tab: a determinism-evaluation launcher, a results panel for
 * whichever evaluation is selected, and the list of past evaluations.
 *
 * Only one evaluation is ever "followed" client-side (`evalStore.activeEvaluationId`);
 * selecting a running/pending row attaches to it with `followEvaluation` so its
 * live progress shows here, while selecting a finished row just renders the
 * result already carried on its list entry (`EvaluationDetail.result`) — no
 * extra fetch needed, though `refreshEvaluation` is used to pick up the final
 * status right after a cancel.
 */

import { useEffect, useRef, useState, type MouseEvent } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  SkeletonLoader,
  StatusBadge
} from '@readysetcloud/ui'
import { statusTone } from '../../components/status'
import DeterminismLauncher from './DeterminismLauncher'
import EvalProgress from './EvalProgress'
import EvalResultView from './EvalResultView'
import EvaluationDetailView, { sourceLabel } from './EvaluationDetailView'
import { useEvalStore } from '../../stores'
import { api } from '../../api'
import type { EvaluationDetail, EvaluationStatus } from '../../api'

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled'
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function isCancellable(status: EvaluationStatus | string): boolean {
  return status === 'pending' || status === 'running'
}

function formatTs(ts: string): string {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString()
}

interface EvalsPageProps {
  /** An evaluation to open on arrival — the `#/evals/<id>` link the CLI prints. */
  evaluationId?: string | null
  /** Told when the open evaluation changes, so the address bar can follow. */
  onSelectEvaluation?: (evaluationId: string | null) => void
}

export default function EvalsPage({
  evaluationId = null,
  onSelectEvaluation
}: EvalsPageProps = {}) {
  const evaluations = useEvalStore(state => state.evaluations)
  const listLoading = useEvalStore(state => state.listLoading)
  const listError = useEvalStore(state => state.listError)
  const nextCursor = useEvalStore(state => state.nextCursor)
  const loadEvaluations = useEvalStore(state => state.loadEvaluations)
  const loadMoreEvaluations = useEvalStore(state => state.loadMoreEvaluations)
  const refreshEvaluation = useEvalStore(state => state.refreshEvaluation)

  const activeEvaluationId = useEvalStore(state => state.activeEvaluationId)
  const activeStatus = useEvalStore(state => state.status)
  const activeResult = useEvalStore(state => state.result)
  const followEvaluation = useEvalStore(state => state.followEvaluation)
  const cancelEvaluation = useEvalStore(state => state.cancelEvaluation)

  const [selectedId, setSelectedIdState] = useState<string | null>(evaluationId)
  const [cloudFilter, setCloudFilter] = useState(false)
  // An evaluation opened by link need not be on the loaded page of the list
  // (an older one, or one from the other lane), so it is fetched on its own.
  const [linked, setLinked] = useState<EvaluationDetail | null>(null)
  const [linkedError, setLinkedError] = useState<string | null>(null)
  const filterInitialized = useRef(false)

  function setSelectedId(id: string | null) {
    setSelectedIdState(id)
    onSelectEvaluation?.(id)
  }

  const listFilters = cloudFilter ? { execution: 'cloud' as const } : {}

  useEffect(() => {
    void loadEvaluations(listFilters)
    // Switching the filter drops the previous selection: a cursor (and the
    // rows it paged in) is only valid for the filter set it was issued under.
    // The first run is the page mounting, not a switch, and must keep a
    // selection that arrived by link.
    if (filterInitialized.current) setSelectedId(null)
    filterInitialized.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cloudFilter, loadEvaluations])

  // A link followed while already on this tab (or the back button).
  useEffect(() => {
    setSelectedIdState(evaluationId)
  }, [evaluationId])

  const listed = evaluations.find(row => row.id === selectedId) ?? null

  useEffect(() => {
    setLinkedError(null)
    if (selectedId === null || listed !== null) {
      setLinked(null)
      return
    }
    if (linked?.id === selectedId) return
    let cancelled = false
    api.evaluations
      .get(selectedId)
      .then(detail => {
        if (!cancelled) setLinked(detail)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLinked(null)
          setLinkedError(error instanceof Error ? error.message : String(error))
        }
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, listed === null])

  function handleSelectRow(row: EvaluationDetail) {
    setSelectedId(row.id)
  }

  async function handleCancelRow(row: EvaluationDetail, event: MouseEvent) {
    event.stopPropagation()
    if (row.id !== activeEvaluationId) void followEvaluation(row.id)
    await cancelEvaluation()
    await refreshEvaluation(row.id)
  }

  const selectedRow = listed ?? (linked?.id === selectedId ? linked : null)
  const selectedStatus = selectedRow?.status ?? null

  // An evaluation still in flight is followed live, however it was selected --
  // a clicked row, or a link (the CLI prints one as it starts) that may only
  // resolve once the row has been fetched. The active id is read at the time,
  // not depended on: following sets it, and a newly launched evaluation
  // replacing it must not pull the selection's stream back.
  useEffect(() => {
    if (selectedId === null || selectedStatus === null || !isCancellable(selectedStatus)) return
    if (useEvalStore.getState().activeEvaluationId === selectedId) return
    void followEvaluation(selectedId)
  }, [selectedId, selectedStatus, followEvaluation])
  const isSelectedActive = selectedId !== null && selectedId === activeEvaluationId
  const showLiveProgress =
    isSelectedActive &&
    (activeStatus === 'starting' || activeStatus === 'running' || activeStatus === 'grading')
  const resultToShow = isSelectedActive
    ? (activeResult ?? selectedRow?.result ?? null)
    : (selectedRow?.result ?? null)

  return (
    <div className="space-y-6" data-testid="evals-page">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 lg:gap-6">
        <DeterminismLauncher onStarted={id => setSelectedId(id)} />

        <Card role="region" aria-labelledby="eval-results-heading">
          <CardHeader>
            <h2 id="eval-results-heading" className="card-title">
              Results
            </h2>
          </CardHeader>

          <CardBody>
            {selectedId === null && (
              <EmptyState
                title="No evaluation selected"
                description="Start a new evaluation or select one from the list below to see its results."
              />
            )}

            {selectedId !== null && showLiveProgress && <EvalProgress />}

            {selectedId !== null && !showLiveProgress && resultToShow && (
              <EvalResultView result={resultToShow} />
            )}

            {selectedId !== null && linkedError && !selectedRow && (
              <Alert variant="error" data-testid="eval-link-error">
                Could not load evaluation {selectedId}: {linkedError}
              </Alert>
            )}

            {selectedId !== null && !showLiveProgress && !resultToShow && !linkedError && (
              <p className="text-sm text-muted-foreground" data-testid="eval-no-result">
                {isSelectedActive && activeStatus === 'error'
                  ? 'The evaluation failed before it produced a result.'
                  : isSelectedActive && activeStatus === 'cancelled'
                    ? 'The evaluation was cancelled before it produced a result.'
                    : 'This evaluation has no result yet.'}
              </p>
            )}
          </CardBody>
        </Card>
      </div>

      {selectedRow && <EvaluationDetailView evaluation={selectedRow} result={resultToShow} />}

      <Card role="region" aria-labelledby="eval-list-heading">
        <CardHeader className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="eval-list-heading" className="card-title">
            Past evaluations
          </h2>
          {/* A lone filter checkbox sits inline in the header, so it keeps its
              wrapping label rather than a stacked Field. */}
          <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground cursor-pointer">
            <input
              type="checkbox"
              data-testid="eval-cloud-filter"
              className="h-4 w-4 rounded border-border accent-primary-600"
              checked={cloudFilter}
              onChange={event => setCloudFilter(event.target.checked)}
            />
            Cloud
          </label>
        </CardHeader>

        <CardBody>
          {listLoading && evaluations.length === 0 && (
            <div role="status" data-testid="eval-list-loading">
              <span className="sr-only">Loading evaluations…</span>
              <SkeletonLoader count={3} />
            </div>
          )}

          {listError && (
            <ErrorState
              className="mb-3"
              heading="Could not load evaluations"
              message={listError.message}
              action={{ label: 'Retry', onClick: () => void loadEvaluations(listFilters) }}
            />
          )}

          {!listLoading && evaluations.length === 0 && !listError && (
            <EmptyState
              title="No evaluations yet"
              description="Evaluations started here or from the CLI will be listed here."
            />
          )}

          {evaluations.length > 0 && (
            <ul className="divide-y divide-border" data-testid="eval-list">
              {evaluations.map(row => (
                <li key={row.id}>
                  {/* A `div[role=button]`, not a real `<button>`: a Cancel button lives inside
                      it, and nesting interactive controls inside a `<button>` is invalid HTML. */}
                  <div
                    role="button"
                    tabIndex={0}
                    data-testid={`eval-row-${row.id}`}
                    onClick={() => handleSelectRow(row)}
                    onKeyDown={event => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        handleSelectRow(row)
                      }
                    }}
                    className={`w-full text-left py-2 px-2 -mx-2 rounded-lg flex flex-wrap items-center justify-between gap-2 hover:bg-muted/50 cursor-pointer ${
                      selectedId === row.id ? 'bg-primary-50' : ''
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-xs font-medium text-muted-foreground uppercase">
                        {row.kind}
                      </span>
                      <Badge
                        variant={row.execution === 'cloud' ? 'primary' : 'neutral'}
                        data-testid={`eval-lane-${row.id}`}
                      >
                        {row.execution}
                      </Badge>
                      {sourceLabel(row.source) && (
                        <Badge variant="primary" data-testid={`eval-source-${row.id}`}>
                          {sourceLabel(row.source)}
                        </Badge>
                      )}
                      <StatusBadge tone={statusTone(row.status)} role={undefined}>
                        {statusLabel(row.status)}
                      </StatusBadge>
                      <span className="text-xs text-muted-foreground">{formatTs(row.ts)}</span>
                    </div>

                    <div className="flex items-center gap-2">
                      {row.result?.grade && (
                        <span className="text-sm font-semibold text-foreground">
                          {row.result.grade}
                        </span>
                      )}
                      {row.result?.score !== null && row.result?.score !== undefined && (
                        <span className="text-xs text-muted-foreground">
                          {row.result.score}/100
                        </span>
                      )}
                      {isCancellable(row.status) && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={event => void handleCancelRow(row, event)}
                        >
                          Cancel
                        </Button>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {nextCursor && (
            <Button
              variant="secondary"
              size="sm"
              className="mt-3"
              loading={listLoading}
              loadingLabel="Loading…"
              onClick={() => void loadMoreEvaluations()}
            >
              Load more
            </Button>
          )}
        </CardBody>
      </Card>
    </div>
  )
}
