/**
 * EvaluationDetailView: the full record of one evaluation — source, what was
 * run, each suite case's verdict, and a link to every run.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { ToastProvider } from '@readysetcloud/ui'
import EvaluationDetailView, {
  batchSlots,
  describeAssertion,
  failedChecks,
  sourceLabel
} from '../EvaluationDetailView'
import { INITIAL_MCP_SERVER_STATE, useMcpServerStore } from '../../../stores'
import type { EvaluationDetail, EvaluationResult, SuiteCaseResult } from '../../../api'

const JUDGE = { model_id: 'amazon.nova-pro-v1:0', system_prompt_used: false, rubric_used: true }

const suiteResult: EvaluationResult = {
  grade: 'C',
  score: 72,
  reasoning: '1/3 cases passed',
  judge: JUDGE,
  metrics: { pass_rate: 1 / 3 },
  run_ids: ['r-a1', 'r-a2', 'r-b1'],
  failed_runs: [{ index: 3, case_id: 'c', error: { code: 'provider_error', message: 'boom' } }],
  suite: { name: 'support', repeats: 2, pass_threshold: 0.7 },
  cases: [
    {
      id: 'refund-window',
      status: 'passed',
      passed: true,
      score: 0.95,
      scores: [1, 0.9],
      reasoning: 'Says 30 days.',
      error: null,
      run_ids: ['r-a1', 'r-a2'],
      runs: { total: 2, succeeded: 2 }
    },
    {
      id: 'sunday-hours',
      status: 'failed',
      passed: false,
      score: 0.1,
      scores: [0.2, 0],
      reasoning: 'Says 9am; the case requires 10am.',
      error: null,
      run_ids: ['r-b1'],
      runs: { total: 2, succeeded: 1 }
    },
    {
      id: 'off-topic',
      status: 'error',
      passed: false,
      score: null,
      scores: [0, 0],
      reasoning: null,
      error: { code: 'provider_error', message: 'model unavailable' },
      run_ids: [],
      runs: { total: 2, succeeded: 0 }
    }
  ]
}

const suiteEvaluation: EvaluationDetail = {
  id: 'eval-suite',
  ts: '2026-09-24T10:00:00Z',
  kind: 'suite',
  status: 'completed',
  execution: 'cloud',
  source: 'cli',
  config: {
    kind: 'suite',
    n: 6,
    run_config: {
      model_id: 'claude-x',
      provider: 'bedrock',
      user_prompt: '',
      system_prompt: 'You are support.',
      inference: { temperature: 0.2 }
    },
    rubric: 'Be strict.',
    grader: { model_id: 'amazon.nova-pro-v1:0', system_prompt: null, provider: 'bedrock' },
    source: 'cli',
    suite: {
      name: 'support',
      run_config: { model_id: 'claude-x', user_prompt: '' },
      cases: [
        { id: 'refund-window', input: 'How long do refunds take?', expected: '30 days' },
        { id: 'sunday-hours', input: 'Open Sunday?', criteria: 'Must say 10am.' },
        { id: 'off-topic', input: 'Write a poem' }
      ],
      repeats: 2,
      pass_threshold: 0.7
    }
  },
  run_ids: ['r-a1', 'r-a2', 'r-b1'],
  result: suiteResult,
  progress: null,
  error: null
}

const determinismEvaluation: EvaluationDetail = {
  id: 'eval-det',
  ts: '2026-09-24T10:00:00Z',
  kind: 'determinism',
  status: 'completed',
  execution: 'local',
  config: {
    kind: 'determinism',
    n: 2,
    run_config: { model_id: 'claude-x', user_prompt: 'Assess order B456', toolset: 'orders' },
    rubric: null,
    grader: { model_id: 'amazon.nova-pro-v1:0', system_prompt: 'Judge hard.' }
  },
  run_ids: ['run-1', 'run-2'],
  result: {
    grade: 'A',
    score: 95,
    reasoning: null,
    judge: JUDGE,
    metrics: {},
    run_ids: ['run-1', 'run-2'],
    failed_runs: []
  },
  progress: null,
  error: null
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('EvaluationDetailView', () => {
  it('shows one row per suite case with its verdict, scores, reasoning and runs', () => {
    render(<EvaluationDetailView evaluation={suiteEvaluation} result={suiteResult} />)

    const passed = screen.getByTestId('eval-case-refund-window')
    expect(within(passed).getByText('Pass')).toBeInTheDocument()
    expect(within(passed).getByText('0.95')).toBeInTheDocument()
    expect(within(passed).getByText('1.00 · 0.90')).toBeInTheDocument()
    expect(within(passed).getByText('Says 30 days.')).toBeInTheDocument()
    const links = within(passed).getAllByRole('link')
    expect(links.map(link => link.getAttribute('href'))).toEqual(['#/runs/r-a1', '#/runs/r-a2'])

    const failed = screen.getByTestId('eval-case-sunday-hours')
    expect(within(failed).getByText('Fail')).toBeInTheDocument()
    expect(within(failed).getByText('0.20 · 0.00')).toBeInTheDocument()

    const errored = screen.getByTestId('eval-case-off-topic')
    expect(within(errored).getByText('Error')).toBeInTheDocument()
    expect(within(errored).getByText('model unavailable')).toBeInTheDocument()
    expect(within(errored).getByText('—')).toBeInTheDocument()
  })

  it("shows each case's input, expected answer and criteria from the stored suite", () => {
    render(<EvaluationDetailView evaluation={suiteEvaluation} result={suiteResult} />)

    const refund = screen.getByTestId('eval-case-refund-window')
    expect(within(refund).getByText('How long do refunds take?')).toBeInTheDocument()
    expect(within(refund).getByText('30 days')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('eval-case-sunday-hours')).getByText('Must say 10am.')
    ).toBeInTheDocument()
  })

  it('says where the evaluation came from and what it ran', () => {
    render(<EvaluationDetailView evaluation={suiteEvaluation} result={suiteResult} />)

    expect(screen.getByTestId('eval-detail-source')).toHaveTextContent('CLI')
    expect(screen.getByRole('heading', { name: 'support' })).toBeInTheDocument()
    const config = screen.getByTestId('eval-config')
    expect(within(config).getByText('claude-x (bedrock)')).toBeInTheDocument()
    expect(within(config).getByText('You are support.')).toBeInTheDocument()
    expect(within(config).getByText('temperature=0.2')).toBeInTheDocument()
    expect(
      within(config).getByText('support — 3 cases × 2 repeats, passing at 0.7')
    ).toBeInTheDocument()
    expect(within(config).getByText('amazon.nova-pro-v1:0 (bedrock)')).toBeInTheDocument()
    expect(within(config).getByText('Be strict.')).toBeInTheDocument()
    // A suite's prompts are its cases; there is no single user prompt to show.
    expect(within(config).queryByText('User prompt')).not.toBeInTheDocument()
  })

  it('names the MCP servers the evaluated run config used', () => {
    useMcpServerStore.setState({
      ...INITIAL_MCP_SERVER_STATE,
      servers: [
        {
          id: 'mcp-1',
          name: 'GitHub MCP',
          url: 'https://mcp.github.example/mcp',
          header_names: [],
          created_at: '2026-09-01T00:00:00Z',
          updated_at: '2026-09-01T00:00:00Z'
        }
      ],
      loaded: true,
      loadServers: vi.fn().mockResolvedValue(undefined)
    })
    const evaluation: EvaluationDetail = {
      ...determinismEvaluation,
      config: {
        ...determinismEvaluation.config,
        run_config: {
          ...determinismEvaluation.config.run_config!,
          mcp_servers: ['mcp-1', 'mcp-gone']
        }
      }
    }
    render(<EvaluationDetailView evaluation={evaluation} result={evaluation.result} />)

    const config = screen.getByTestId('eval-config')
    expect(within(config).getByText('MCP servers')).toBeInTheDocument()
    expect(within(config).getByText('GitHub MCP, mcp-gone')).toBeInTheDocument()
  })

  it('lists and links every run of a non-suite evaluation', () => {
    render(
      <EvaluationDetailView
        evaluation={determinismEvaluation}
        result={determinismEvaluation.result}
      />
    )

    const runs = screen.getByTestId('eval-runs')
    expect(within(runs).getByRole('link', { name: 'Run 1' })).toHaveAttribute(
      'href',
      '#/runs/run-1'
    )
    expect(within(runs).getByRole('link', { name: 'Run 2' })).toHaveAttribute(
      'href',
      '#/runs/run-2'
    )
    const config = screen.getByTestId('eval-config')
    expect(within(config).getByText('Assess order B456')).toBeInTheDocument()
    expect(within(config).getByText('orders')).toBeInTheDocument()
    expect(within(config).getByText('Judge hard.')).toBeInTheDocument()
    expect(screen.queryByTestId('eval-cases')).not.toBeInTheDocument()
    // Recorded before sources existed: no badge, rather than a guess.
    expect(screen.queryByTestId('eval-detail-source')).not.toBeInTheDocument()
  })

  it('links a failed run in its place in the batch, without renumbering the rest', () => {
    const result: EvaluationResult = {
      ...determinismEvaluation.result!,
      run_ids: ['run-1', 'run-3'],
      failed_runs: [
        { index: 1, run_id: 'run-2', error: { code: 'internal_error', message: 'kaboom' } },
        { index: 3, run_id: null, error: { code: 'model_throttled', message: 'slow down' } }
      ]
    }
    render(<EvaluationDetailView evaluation={determinismEvaluation} result={result} />)

    const runs = screen.getByTestId('eval-runs')
    expect(within(runs).getByRole('link', { name: 'Run 1' })).toHaveAttribute(
      'href',
      '#/runs/run-1'
    )
    const failed = within(runs).getByRole('link', { name: 'Run 2' })
    expect(failed).toHaveAttribute('href', '#/runs/run-2')
    expect(failed).toHaveAttribute('title', expect.stringMatching(/^Failed run run-2/))
    expect(within(runs).getByRole('link', { name: 'Run 3' })).toHaveAttribute(
      'href',
      '#/runs/run-3'
    )
    // Never recorded: numbered, but nothing to open.
    expect(within(runs).queryByRole('link', { name: 'Run 4' })).not.toBeInTheDocument()
    expect(within(runs).getByText('Run 4')).toHaveAttribute(
      'title',
      'Run 4 failed before it was recorded'
    )
  })

  it('numbers failures stored without a run id, and lists odd indices after the successes', () => {
    // Older results: no run_id on failures.
    expect(
      batchSlots(['a'], [{ index: 0, error: null }]).map(slot => [slot.label, slot.runId])
    ).toEqual([
      ['Run 1', null],
      ['Run 2', 'a']
    ])
    // Indices that cannot all be placed: successes first, each failure by its own index.
    expect(
      batchSlots(
        ['a', 'b'],
        [
          { index: 7, run_id: 'x', error: null },
          { index: 7, run_id: 'y', error: null }
        ]
      ).map(slot => [slot.label, slot.runId, slot.failed])
    ).toEqual([
      ['Run 1', 'a', false],
      ['Run 2', 'b', false],
      ['Run 8', 'x', true],
      ['Run 8', 'y', true]
    ])
  })

  it('still links the runs of an evaluation with no result yet', () => {
    render(
      <EvaluationDetailView evaluation={{ ...determinismEvaluation, result: null }} result={null} />
    )

    expect(within(screen.getByTestId('eval-runs')).getAllByRole('link')).toHaveLength(2)
  })

  it('warns when the judge prose was shortened to fit', () => {
    render(
      <EvaluationDetailView
        evaluation={suiteEvaluation}
        result={{ ...suiteResult, truncated: true }}
      />
    )

    expect(screen.getByText(/shortened to fit the stored result/)).toBeInTheDocument()
  })

  it('copies a link to this evaluation', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(
      <ToastProvider>
        <EvaluationDetailView evaluation={suiteEvaluation} result={suiteResult} />
      </ToastProvider>
    )

    fireEvent.click(screen.getByTestId('eval-copy-link'))

    // Confirmed with a toast; the button itself keeps its label.
    expect(await screen.findByText('Link copied')).toBeInTheDocument()
    expect(screen.getByTestId('eval-copy-link')).toHaveTextContent('Copy link')
    expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/#\/evals\/eval-suite$/))
  })

  it('names the sources people will recognise', () => {
    expect(sourceLabel('cli')).toBe('CLI')
    expect(sourceLabel('ui')).toBe('Web UI')
    expect(sourceLabel('api')).toBe('API')
    expect(sourceLabel(null)).toBeNull()
    expect(sourceLabel('cron')).toBe('cron')
  })

  it('labels and links runs by repeat, so a failed repeat does not renumber the rest', () => {
    const cases = [
      {
        ...suiteResult.cases![1],
        id: 'flaky',
        scores: [0, 0.8, 0],
        run_ids: ['r-2'],
        repeats: [
          { run_id: 'r-1', ran: false, score: 0 },
          { run_id: 'r-2', ran: true, score: 0.8 },
          { run_id: null, ran: false, score: 0 }
        ]
      }
    ]
    render(<EvaluationDetailView evaluation={suiteEvaluation} result={{ ...suiteResult, cases }} />)

    const row = screen.getByTestId('eval-case-flaky')
    expect(within(row).getByRole('link', { name: '#1' })).toHaveAttribute('href', '#/runs/r-1')
    expect(within(row).getByRole('link', { name: '#1' })).toHaveAttribute(
      'title',
      expect.stringMatching(/^Failed run r-1/)
    )
    expect(within(row).getByRole('link', { name: '#2' })).toHaveAttribute('href', '#/runs/r-2')
    // No run was recorded for the third: numbered, but nothing to open.
    expect(within(row).queryByRole('link', { name: '#3' })).not.toBeInTheDocument()
    expect(within(row).getByText('#3')).toHaveAttribute(
      'title',
      'Repeat 3 failed before a run was recorded'
    )
  })

  describe('assertions', () => {
    const checked: SuiteCaseResult = {
      id: 'format',
      status: 'failed',
      passed: false,
      score: 0.9,
      scores: [0.9, 0.9, 0],
      reasoning: 'Reads well.',
      error: null,
      run_ids: ['r-1', 'r-2'],
      runs: { total: 3, succeeded: 2 },
      judged: true,
      assertions: { total: 4, passed: 1, failed: 3 },
      repeats: [
        {
          run_id: 'r-1',
          ran: true,
          score: 0.9,
          assertions_passed: false,
          assertions: [
            { type: 'contains', passed: false, detail: 'output does not contain "30"' },
            { type: 'json_valid', passed: true, detail: 'valid JSON' }
          ]
        },
        {
          run_id: 'r-2',
          ran: true,
          score: 0.9,
          assertions_passed: false,
          assertions: [
            { type: 'contains', passed: false, detail: 'second time' },
            { type: 'json_valid', passed: false, detail: null }
          ]
        },
        { run_id: null, ran: false, score: 0, assertions: null, assertions_passed: null }
      ]
    }
    const unjudged: SuiteCaseResult = {
      ...suiteResult.cases![0],
      id: 'checks-only',
      judged: false,
      reasoning: null,
      assertions: { total: 1, passed: 1, failed: 0 },
      repeats: [
        {
          run_id: 'r-a1',
          ran: true,
          score: 1,
          assertions_passed: true,
          assertions: [{ type: 'max_tool_calls', passed: true, detail: '1 tool calls <= 2' }]
        }
      ]
    }
    const evaluation: EvaluationDetail = {
      ...suiteEvaluation,
      config: {
        ...suiteEvaluation.config,
        suite: {
          ...suiteEvaluation.config.suite!,
          cases: [
            {
              id: 'format',
              input: 'Answer in JSON',
              assert: [
                { type: 'contains', value: '30', case_sensitive: true },
                { type: 'json_valid' }
              ]
            },
            {
              id: 'checks-only',
              input: 'Go',
              judge: false,
              assert: [{ type: 'max_tool_calls', value: 2 }]
            }
          ]
        }
      }
    }

    it('shows each case’s tally and failed checks, with the repeats they failed on', () => {
      render(
        <EvaluationDetailView
          evaluation={evaluation}
          result={{ ...suiteResult, cases: [checked, unjudged, suiteResult.cases![2]] }}
        />
      )

      expect(screen.getByRole('columnheader', { name: 'Assertions' })).toBeInTheDocument()
      const cell = screen.getByTestId('eval-case-format-assertions')
      expect(within(cell).getByText('1/4 passed')).toBeInTheDocument()
      const failures = within(cell).getAllByRole('listitem').slice(0, 2)
      expect(failures[0]).toHaveTextContent(
        'contains (repeats 1, 2 of 3): output does not contain "30"'
      )
      expect(failures[1]).toHaveTextContent('json_valid (repeat 2 of 3)')
      // Every verdict of every repeat, behind a disclosure; a repeat that did not run says so.
      expect(within(cell).getByText('By repeat')).toBeInTheDocument()
      expect(within(cell).getByText('did not run', { exact: false })).toBeInTheDocument()
      expect(within(cell).getByText('— valid JSON')).toBeInTheDocument()

      const only = screen.getByTestId('eval-case-checks-only')
      expect(within(only).getByText('Not judged: scored by its assertions.')).toBeInTheDocument()
      expect(within(only).getByText('1/1 passed')).toBeInTheDocument()
      // A case without checks in a suite that has some: a dash, not an empty cell.
      expect(within(screen.getByTestId('eval-case-off-topic')).getAllByText('—')).toHaveLength(2)
    })

    it('shows the checks each case was defined with', () => {
      render(
        <EvaluationDetailView
          evaluation={evaluation}
          result={{ ...suiteResult, cases: [checked, unjudged] }}
        />
      )

      const row = screen.getByTestId('eval-case-format')
      expect(
        within(row).getByText('contains {"value":"30","case_sensitive":true}')
      ).toBeInTheDocument()
      const definition = within(row).getByText('Case').closest('details')!
      expect(within(definition).getByText('Assertions')).toBeInTheDocument()
      expect(within(definition).getByText('json_valid')).toBeInTheDocument()
    })

    it('adds no column to a suite without assertions', () => {
      render(<EvaluationDetailView evaluation={suiteEvaluation} result={suiteResult} />)

      expect(screen.queryByRole('columnheader', { name: 'Assertions' })).not.toBeInTheDocument()
    })

    it('describes a check by its type and the fields it sets', () => {
      expect(describeAssertion({ type: 'no_tool_errors' })).toBe('no_tool_errors')
      expect(
        describeAssertion({ type: 'tool_called', name: 'f', args: null, times: 1, min_times: null })
      ).toBe('tool_called {"name":"f","times":1}')
    })

    it('groups a case’s failures by check, in list order', () => {
      expect(failedChecks(checked)).toEqual([
        { position: 0, type: 'contains', repeats: [1, 2], detail: 'output does not contain "30"' },
        { position: 1, type: 'json_valid', repeats: [2], detail: null }
      ])
      expect(failedChecks({ ...checked, repeats: undefined })).toEqual([])
    })
  })
})
