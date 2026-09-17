/** MetricsBar: every stat formats from the run store, with dashes for what is not known yet. */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import MetricsBar from '../MetricsBar'
import { INITIAL_RUN_STATE, useRunStore } from '../../../stores'

function stat(label: string): string {
  return screen.getByText(label).nextElementSibling?.textContent ?? ''
}

beforeEach(() => {
  useRunStore.setState({ ...INITIAL_RUN_STATE })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('MetricsBar', () => {
  it('shows a dash for every stat before a run has produced metrics', () => {
    render(<MetricsBar />)
    for (const label of [
      'Input tokens',
      'Output tokens',
      'Total tokens',
      'Latency',
      'Cycles',
      'Elapsed'
    ]) {
      expect(stat(label)).toBe('—')
    }
  })

  it('formats counts with separators and sub-second latency in milliseconds', () => {
    useRunStore.setState({
      metrics: {
        input_tokens: 1234,
        output_tokens: 56,
        total_tokens: 1290,
        latency_ms: 842.4,
        cycle_count: 2
      }
    })
    render(<MetricsBar />)
    expect(stat('Input tokens')).toBe('1,234')
    expect(stat('Output tokens')).toBe('56')
    expect(stat('Total tokens')).toBe('1,290')
    expect(stat('Latency')).toBe('842 ms')
    expect(stat('Cycles')).toBe('2')
  })

  it('formats latency of a second or more in seconds', () => {
    useRunStore.setState({ metrics: { latency_ms: 2345 } })
    render(<MetricsBar />)
    expect(stat('Latency')).toBe('2.35 s')
  })

  it('freezes elapsed at endedAt once the run is over', () => {
    useRunStore.setState({ startedAt: 1_000, endedAt: 4_250 })
    render(<MetricsBar />)
    expect(stat('Elapsed')).toBe('3.25 s')
  })

  it('reads elapsed off the clock while the run is still going', () => {
    vi.useFakeTimers({ now: 5_600 })
    useRunStore.setState({ startedAt: 5_000, endedAt: null })
    render(<MetricsBar />)
    expect(stat('Elapsed')).toBe('600 ms')
  })
})
