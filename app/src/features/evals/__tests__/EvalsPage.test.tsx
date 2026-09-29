/**
 * EvalsPage: past-evaluations list rendering and the row Cancel action.
 * `DeterminismLauncher` renders inside the page too, so its dependent stores
 * are given enough state to mount without crashing.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { EvaluationDetail } from '../../../api'

const healthMock = vi.fn().mockResolvedValue({
  status: 'ok',
  aws: { region: 'us-east-1', credentials: 'ok' },
  cloud_evals: { configured: false }
})

const getEvaluationMock = vi.fn()
const compareEvaluationsMock = vi.fn()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      health: healthMock,
      // Never settles: the launcher's tool picker is not under test here.
      tools: vi.fn(() => new Promise(() => {})),
      evaluations: {
        ...actual.api.evaluations,
        get: getEvaluationMock,
        compare: compareEvaluationsMock
      }
    }
  }
})

const EvalsPage = (await import('../EvalsPage')).default
const {
  DEFAULT_RUN_CONFIG,
  DEFAULT_SETTINGS,
  INITIAL_EVAL_STATE,
  useEvalStore,
  useMcpServerStore,
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
  useMcpServerStore.setState({
    servers: [],
    loaded: true,
    loadServers: vi.fn().mockResolvedValue(undefined)
  })
  healthMock.mockClear()
  getEvaluationMock.mockReset()
  compareEvaluationsMock.mockReset()
  compareEvaluationsMock.mockReturnValue(new Promise(() => {}))
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

  it('says so when there are no evaluations yet', () => {
    useEvalStore.setState({ evaluations: [] })
    render(<EvalsPage />)

    expect(screen.getByText('No evaluations yet')).toBeInTheDocument()
    expect(screen.queryByTestId('eval-list')).not.toBeInTheDocument()
  })

  it('shows a loading placeholder while the first page loads', () => {
    useEvalStore.setState({ evaluations: [], listLoading: true })
    render(<EvalsPage />)

    expect(screen.getByTestId('eval-list-loading')).toHaveTextContent('Loading evaluations…')
    expect(screen.queryByText('No evaluations yet')).not.toBeInTheDocument()
  })

  it('reports a list load failure and retries with the current filter', () => {
    useEvalStore.setState({
      evaluations: [],
      listError: { message: 'Network down', code: 'network_error' }
    })
    render(<EvalsPage />)
    loadEvaluations.mockClear()

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load evaluations')
    expect(screen.getByRole('alert')).toHaveTextContent('Network down')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(loadEvaluations).toHaveBeenCalledWith({})
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

  it('follows a linked evaluation that is still running', () => {
    render(<EvalsPage evaluationId="eval-running" />)

    expect(followEvaluation).toHaveBeenCalledTimes(1)
    expect(followEvaluation).toHaveBeenCalledWith('eval-running')
  })

  it('follows a linked running evaluation once it has been fetched', async () => {
    getEvaluationMock.mockResolvedValue({ ...rows[1], id: 'eval-elsewhere', status: 'pending' })
    render(<EvalsPage evaluationId="eval-elsewhere" />)

    await waitFor(() => expect(followEvaluation).toHaveBeenCalledWith('eval-elsewhere'))
    expect(followEvaluation).toHaveBeenCalledTimes(1)
  })

  it('does not follow a linked evaluation that has finished', async () => {
    getEvaluationMock.mockResolvedValue({ ...rows[0], id: 'eval-older' })
    render(<EvalsPage evaluationId="eval-older" />)

    await waitFor(() => expect(screen.getByTestId('eval-detail')).toBeInTheDocument())
    expect(followEvaluation).not.toHaveBeenCalled()
  })

  it('does not re-attach to a linked evaluation it is already following', () => {
    useEvalStore.setState({ activeEvaluationId: 'eval-running', status: 'running' })
    render(<EvalsPage evaluationId="eval-running" />)

    expect(followEvaluation).not.toHaveBeenCalled()
  })

  it('selecting the same running row twice follows it once', () => {
    render(<EvalsPage />)
    const row = screen.getByTestId('eval-row-eval-running')

    fireEvent.click(row)
    fireEvent.click(row)

    expect(followEvaluation).toHaveBeenCalledTimes(1)
  })
})

describe('EvalsPage: comparing models', () => {
  function suiteRow(id: string, status = 'completed'): EvaluationDetail {
    return {
      ...rows[0],
      id,
      kind: 'suite',
      status,
      result: status === 'completed' ? rows[0].result : null
    }
  }

  function withSuites(...ids: string[]) {
    useEvalStore.setState({ evaluations: [...rows, ...ids.map(id => suiteRow(id))] })
  }

  it('offers the comparison checkbox on finished suite evaluations only', () => {
    useEvalStore.setState({
      evaluations: [...rows, suiteRow('suite-done'), suiteRow('suite-running', 'running')]
    })
    render(<EvalsPage />)

    expect(screen.getByTestId('eval-compare-checkbox-suite-done')).toBeInTheDocument()
    // Not a suite (a determinism run), and not finished (no result to compare yet).
    expect(screen.queryByTestId('eval-compare-checkbox-eval-completed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('eval-compare-checkbox-suite-running')).not.toBeInTheDocument()
    expect(screen.queryByTestId('eval-compare-btn')).not.toBeInTheDocument()
  })

  it('needs two ticked evaluations before it will compare', () => {
    withSuites('s1', 's2')
    const onCompare = vi.fn()
    render(<EvalsPage onCompare={onCompare} />)

    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s1'))
    const button = screen.getByTestId('eval-compare-btn')
    expect(button).toHaveTextContent('Compare models (1)')
    expect(button).toBeDisabled()

    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s2'))
    expect(button).toHaveTextContent('Compare models (2)')
    expect(button).toBeEnabled()

    fireEvent.click(button)
    expect(onCompare).toHaveBeenCalledWith(['s1', 's2'])
  })

  it('compares in the order the evaluations were ticked', () => {
    withSuites('s1', 's2', 's3')
    const onCompare = vi.fn()
    render(<EvalsPage onCompare={onCompare} />)

    for (const id of ['s3', 's1', 's2']) {
      fireEvent.click(screen.getByTestId(`eval-compare-checkbox-${id}`))
    }
    fireEvent.click(screen.getByTestId('eval-compare-btn'))

    expect(onCompare).toHaveBeenCalledWith(['s3', 's1', 's2'])
  })

  it('unticking takes an evaluation back out, and the button goes with the last one', () => {
    withSuites('s1', 's2')
    render(<EvalsPage />)

    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s1'))
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s2'))
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s1'))
    expect(screen.getByTestId('eval-compare-btn')).toHaveTextContent('(1)')

    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s2'))
    expect(screen.queryByTestId('eval-compare-btn')).not.toBeInTheDocument()
  })

  it('stops at six, the most one comparison can hold', () => {
    const ids = ['s1', 's2', 's3', 's4', 's5', 's6', 's7']
    withSuites(...ids)
    render(<EvalsPage />)

    for (const id of ids.slice(0, 6)) {
      fireEvent.click(screen.getByTestId(`eval-compare-checkbox-${id}`))
    }

    expect(screen.getByTestId('eval-compare-checkbox-s7')).toBeDisabled()
    expect(screen.getByTestId('eval-compare-checkbox-s6')).toBeEnabled() // still untickable
  })

  it('ticking a box does not also open the evaluation', () => {
    withSuites('s1')
    const onSelectEvaluation = vi.fn()
    render(<EvalsPage onSelectEvaluation={onSelectEvaluation} />)

    const box = screen.getByTestId('eval-compare-checkbox-s1')
    fireEvent.click(box)
    fireEvent.keyDown(box, { key: ' ' })
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(onSelectEvaluation).not.toHaveBeenCalled()
  })

  it('shows the comparison when opened by link, for the evaluations named', () => {
    render(<EvalsPage compareIds={['s1', 's2']} />)

    expect(screen.getByTestId('model-compare')).toBeInTheDocument()
    expect(compareEvaluationsMock.mock.calls[0][0]).toEqual(['s1', 's2'])
  })

  it('shows no comparison for fewer than two evaluations', () => {
    render(<EvalsPage compareIds={['s1']} />)

    expect(screen.queryByTestId('model-compare')).not.toBeInTheDocument()
    render(<EvalsPage compareIds={null} />)
    expect(screen.queryByTestId('model-compare')).not.toBeInTheDocument()
  })

  it('closing the comparison clears the ticks and tells the address bar', () => {
    withSuites('s1', 's2')
    const onCompare = vi.fn()
    render(<EvalsPage compareIds={['s1', 's2']} onCompare={onCompare} />)
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s1'))
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s2'))

    fireEvent.click(screen.getByTestId('model-compare-close'))

    expect(onCompare).toHaveBeenCalledWith(null)
    expect(screen.queryByTestId('eval-compare-btn')).not.toBeInTheDocument()
  })

  it('can be used with no listener for compare events', () => {
    withSuites('s1', 's2')
    render(<EvalsPage compareIds={['s1', 's2']} />)
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s1'))
    fireEvent.click(screen.getByTestId('eval-compare-checkbox-s2'))

    fireEvent.click(screen.getByTestId('eval-compare-btn'))
    fireEvent.click(screen.getByTestId('model-compare-close'))

    expect(screen.getByTestId('evals-page')).toBeInTheDocument()
  })
})

describe('EvalsPage: a link that opens something', () => {
  it('is kept when the page mounts under React StrictMode', () => {
    // StrictMode runs effects twice in development. The second pass is the same
    // mount, not a filter switch, so it must not clear what the link opened.
    const onSelectEvaluation = vi.fn()

    render(
      <StrictMode>
        <EvalsPage evaluationId="eval-completed" onSelectEvaluation={onSelectEvaluation} />
      </StrictMode>
    )

    expect(onSelectEvaluation).not.toHaveBeenCalled()
    expect(screen.getByTestId('eval-result')).toBeInTheDocument()
  })

  it('is still dropped when the filter really is switched', () => {
    const onSelectEvaluation = vi.fn()
    render(<EvalsPage evaluationId="eval-completed" onSelectEvaluation={onSelectEvaluation} />)

    fireEvent.click(screen.getByTestId('eval-cloud-filter'))

    expect(onSelectEvaluation).toHaveBeenCalledWith(null)
  })
})
