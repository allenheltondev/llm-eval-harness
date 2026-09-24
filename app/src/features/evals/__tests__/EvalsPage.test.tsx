/**
 * EvalsPage: past-evaluations list rendering and the row Cancel action.
 * `DeterminismLauncher` renders inside the page too, so its dependent stores
 * are given enough state to mount without crashing.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { EvaluationDetail } from '../../../api'

const healthMock = vi.fn().mockResolvedValue({
  status: 'ok',
  aws: { region: 'us-east-1', credentials: 'ok' },
  cloud_evals: { configured: false }
})

const getEvaluationMock = vi.fn()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      health: healthMock,
      evaluations: { ...actual.api.evaluations, get: getEvaluationMock }
    }
  }
})

const EvalsPage = (await import('../EvalsPage')).default
const {
  DEFAULT_RUN_CONFIG,
  DEFAULT_SETTINGS,
  INITIAL_EVAL_STATE,
  useEvalStore,
  useModelStore,
  useRunConfigStore,
  useSettingsStore
} = await import('../../../stores')

const loadEvaluations = vi.fn().mockResolvedValue(undefined)
const loadMoreEvaluations = vi.fn().mockResolvedValue(undefined)
const refreshEvaluation = vi.fn().mockResolvedValue(undefined)
const followEvaluation = vi.fn().mockResolvedValue(undefined)
const cancelEvaluation = vi.fn().mockResolvedValue(undefined)

const rows: EvaluationDetail[] = [
  {
    id: 'eval-completed',
    ts: '2026-08-10T12:00:00Z',
    kind: 'determinism',
    status: 'completed',
    execution: 'local',
    config: {
      kind: 'determinism',
      n: 4,
      run_config: { model_id: 'm', user_prompt: 'p' },
      rubric: null,
      grader: { model_id: 'amazon.nova-pro-v1:0', system_prompt: null }
    },
    run_ids: ['run-0', 'run-1'],
    result: {
      grade: 'A',
      score: 95,
      reasoning: 'Very consistent.',
      judge: { model_id: 'amazon.nova-pro-v1:0', system_prompt_used: false, rubric_used: false },
      metrics: { runs_analyzed: 4 },
      run_ids: ['run-0', 'run-1'],
      failed_runs: []
    },
    progress: null,
    error: null
  },
  {
    id: 'eval-running',
    ts: '2026-08-12T09:00:00Z',
    kind: 'determinism',
    status: 'running',
    config: {
      kind: 'determinism',
      n: 6,
      run_config: { model_id: 'm', user_prompt: 'p' },
      rubric: null,
      grader: { model_id: 'amazon.nova-pro-v1:0', system_prompt: null }
    },
    run_ids: [],
    result: null,
    progress: null,
    error: null,
    execution: 'cloud'
  }
]

beforeEach(() => {
  healthMock.mockClear()
  getEvaluationMock.mockReset()
  loadEvaluations.mockClear()
  loadMoreEvaluations.mockClear()
  refreshEvaluation.mockClear()
  followEvaluation.mockClear()
  cancelEvaluation.mockClear()

  useEvalStore.setState({
    ...INITIAL_EVAL_STATE,
    evaluations: rows,
    nextCursor: null,
    listLoaded: true,
    loadEvaluations,
    loadMoreEvaluations,
    refreshEvaluation,
    followEvaluation,
    cancelEvaluation
  })
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useSettingsStore.setState({ ...DEFAULT_SETTINGS })
  useModelStore.setState({
    models: [],
    modelsLoaded: true,
    loadModels: vi.fn().mockResolvedValue(undefined)
  })
})

describe('EvalsPage', () => {
  it('loads evaluations on mount', () => {
    render(<EvalsPage />)
    expect(loadEvaluations).toHaveBeenCalledTimes(1)
  })

  it('renders each past evaluation with kind, status badge, timestamp and grade/score', () => {
    render(<EvalsPage />)

    const completedRow = screen.getByTestId('eval-row-eval-completed')
    expect(within(completedRow).getByText('determinism')).toBeInTheDocument()
    expect(within(completedRow).getByText('Completed')).toBeInTheDocument()
    expect(within(completedRow).getByText('A')).toBeInTheDocument()
    expect(within(completedRow).getByText('95/100')).toBeInTheDocument()

    const runningRow = screen.getByTestId('eval-row-eval-running')
    expect(within(runningRow).getByText('Running')).toBeInTheDocument()
    // No result yet, so no grade/score badge for the running row.
    expect(within(runningRow).queryByText(/\/100/)).not.toBeInTheDocument()
  })

  it('shows a Cancel button only on cancellable (pending/running) rows', () => {
    render(<EvalsPage />)

    const completedRow = screen.getByTestId('eval-row-eval-completed')
    const runningRow = screen.getByTestId('eval-row-eval-running')

    expect(within(completedRow).queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    expect(within(runningRow).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })

  it('cancelling a running row that is not already followed attaches then cancels', async () => {
    render(<EvalsPage />)

    const runningRow = screen.getByTestId('eval-row-eval-running')
    fireEvent.click(within(runningRow).getByRole('button', { name: 'Cancel' }))

    expect(followEvaluation).toHaveBeenCalledWith('eval-running')
    expect(cancelEvaluation).toHaveBeenCalledTimes(1)
  })

  it('cancelling the already-followed evaluation does not re-attach', () => {
    useEvalStore.setState({ activeEvaluationId: 'eval-running', status: 'running' })
    render(<EvalsPage />)

    const runningRow = screen.getByTestId('eval-row-eval-running')
    fireEvent.click(within(runningRow).getByRole('button', { name: 'Cancel' }))

    expect(followEvaluation).not.toHaveBeenCalled()
    expect(cancelEvaluation).toHaveBeenCalledTimes(1)
  })

  it('selecting a finished row shows its stored result without following', () => {
    render(<EvalsPage />)

    fireEvent.click(screen.getByTestId('eval-row-eval-completed'))

    expect(followEvaluation).not.toHaveBeenCalled()
    expect(screen.getByTestId('eval-result')).toBeInTheDocument()
    expect(screen.getByTestId('eval-grade')).toHaveTextContent('A')
  })

  it('selecting a running row follows it and shows live progress', () => {
    render(<EvalsPage />)

    fireEvent.click(screen.getByTestId('eval-row-eval-running'))

    expect(followEvaluation).toHaveBeenCalledWith('eval-running')
  })

  it('renders a lane badge per row', () => {
    render(<EvalsPage />)

    expect(screen.getByTestId('eval-lane-eval-completed')).toHaveTextContent('local')
    expect(screen.getByTestId('eval-lane-eval-running')).toHaveTextContent('cloud')
  })

  it('the Cloud filter reloads the list with {execution: "cloud"} and back with none on uncheck', () => {
    render(<EvalsPage />)
    expect(loadEvaluations).toHaveBeenLastCalledWith({})

    fireEvent.click(screen.getByTestId('eval-cloud-filter'))
    expect(loadEvaluations).toHaveBeenLastCalledWith({ execution: 'cloud' })

    fireEvent.click(screen.getByTestId('eval-cloud-filter'))
    expect(loadEvaluations).toHaveBeenLastCalledWith({})
  })

  it('labels where each evaluation was started', () => {
    useEvalStore.setState({
      evaluations: [
        { ...rows[0], source: 'cli' },
        { ...rows[1], source: null }
      ]
    })
    render(<EvalsPage />)

    expect(screen.getByTestId('eval-source-eval-completed')).toHaveTextContent('CLI')
    expect(screen.queryByTestId('eval-source-eval-running')).not.toBeInTheDocument()
  })

  it('opens a linked evaluation from the loaded list without fetching it', () => {
    render(<EvalsPage evaluationId="eval-completed" />)

    expect(screen.getByTestId('eval-result')).toBeInTheDocument()
    expect(screen.getByTestId('eval-detail')).toBeInTheDocument()
    expect(getEvaluationMock).not.toHaveBeenCalled()
    // Mounting is not a filter switch: the linked selection survives it.
    expect(screen.getByTestId('eval-row-eval-completed').className).toContain('bg-primary-50')
  })

  it('arriving by link keeps the link in the address bar', () => {
    const onSelectEvaluation = vi.fn()
    render(<EvalsPage evaluationId="eval-completed" onSelectEvaluation={onSelectEvaluation} />)

    // Mounting loads the list; it must not report "nothing selected" and so
    // rewrite #/evals/<id> to #/evals.
    expect(onSelectEvaluation).not.toHaveBeenCalled()
  })

  it('fetches a linked evaluation that is not on the loaded page', async () => {
    getEvaluationMock.mockResolvedValue({
      ...rows[0],
      id: 'eval-older',
      source: 'cli',
      execution: 'cloud'
    })
    render(<EvalsPage evaluationId="eval-older" />)

    await waitFor(() => expect(screen.getByTestId('eval-detail')).toBeInTheDocument())
    expect(getEvaluationMock).toHaveBeenCalledWith('eval-older')
    expect(screen.getByTestId('eval-detail-source')).toHaveTextContent('CLI')
    expect(screen.getByTestId('eval-grade')).toHaveTextContent('A')
  })

  it('says so when a linked evaluation cannot be loaded', async () => {
    getEvaluationMock.mockRejectedValue(new Error('Evaluation not found'))
    render(<EvalsPage evaluationId="nope" />)

    await waitFor(() =>
      expect(screen.getByTestId('eval-link-error')).toHaveTextContent('Evaluation not found')
    )
    expect(screen.queryByTestId('eval-detail')).not.toBeInTheDocument()
  })

  it('reports the selected evaluation so the address bar can follow', () => {
    const onSelectEvaluation = vi.fn()
    render(<EvalsPage onSelectEvaluation={onSelectEvaluation} />)

    fireEvent.click(screen.getByTestId('eval-row-eval-completed'))

    expect(onSelectEvaluation).toHaveBeenCalledWith('eval-completed')
  })

  it('follows a new link while already on the page', () => {
    const { rerender } = render(<EvalsPage evaluationId={null} />)
    expect(screen.queryByTestId('eval-detail')).not.toBeInTheDocument()

    rerender(<EvalsPage evaluationId="eval-completed" />)

    expect(screen.getByTestId('eval-detail')).toBeInTheDocument()
  })

  it('switching the lane filter clears the selection and tells the address bar', () => {
    const onSelectEvaluation = vi.fn()
    render(<EvalsPage evaluationId="eval-completed" onSelectEvaluation={onSelectEvaluation} />)

    fireEvent.click(screen.getByTestId('eval-cloud-filter'))

    expect(onSelectEvaluation).toHaveBeenLastCalledWith(null)
    expect(screen.queryByTestId('eval-detail')).not.toBeInTheDocument()
  })
})
