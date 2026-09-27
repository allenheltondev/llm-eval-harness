/**
 * formatUsd: an estimated cost reads the same everywhere, and an unpriced
 * model reads as "unknown" — never as free.
 */

import { describe, expect, it } from 'vitest'
import { formatUsd, UNKNOWN_COST } from '../cost'

describe('formatUsd', () => {
  it('shows four decimals under a dollar, so small runs are not all $0.00', () => {
    expect(formatUsd(0.001234)).toBe('$0.0012')
    expect(formatUsd(0)).toBe('$0.0000')
  })

  it('shows cents from a dollar up, with separators', () => {
    expect(formatUsd(1)).toBe('$1.00')
    expect(formatUsd(1234.5)).toBe('$1,234.50')
  })

  it('reads null (an unpriced model) as unknown, not zero', () => {
    expect(formatUsd(null)).toBe(UNKNOWN_COST)
  })

  it('reads an absent or non-numeric value as a dash', () => {
    expect(formatUsd(undefined)).toBe('—')
    expect(formatUsd('3')).toBe('—')
    expect(formatUsd(Number.NaN)).toBe('—')
  })
})
