/** How the numbers in a comparison read: a missing figure is a dash, never a zero. */

import { describe, expect, it } from 'vitest'
import { duration, percent, points, probability, ratio, scoreDelta } from '../comparisonFormat'

describe('percent', () => {
  it('rounds a rate to a whole percent', () => {
    expect(percent(0.9)).toBe('90%')
    expect(percent(0.125)).toBe('13%')
    expect(percent(0)).toBe('0%')
    expect(percent(1)).toBe('100%')
  })

  it('shows a missing rate as a dash, not as zero', () => {
    expect(percent(null)).toBe('—')
    expect(percent(undefined)).toBe('—')
  })
})

describe('points', () => {
  it('signs a change in percentage points', () => {
    expect(points(0.05)).toBe('+5 pts')
    expect(points(-0.1)).toBe('−10 pts')
  })

  it('does not sign zero, or a change that rounds to it', () => {
    expect(points(0)).toBe('±0 pts')
    expect(points(0.004)).toBe('±0 pts')
    expect(points(-0.004)).toBe('±0 pts')
  })

  it('shows an unmeasured change as a dash', () => {
    expect(points(null)).toBe('—')
    expect(points(undefined)).toBe('—')
  })
})

describe('scoreDelta', () => {
  it('signs a change in score', () => {
    expect(scoreDelta(7)).toBe('+7')
    expect(scoreDelta(-12)).toBe('−12')
    expect(scoreDelta(0)).toBe('±0')
    expect(scoreDelta(null)).toBe('—')
  })
})

describe('ratio', () => {
  it('reads a ratio to the baseline', () => {
    expect(ratio(1.8)).toBe('1.8×')
    expect(ratio(1)).toBe('1.0×')
    expect(ratio(0.25)).toBe('0.3×')
  })

  it('shows an unmeasured ratio as a dash', () => {
    expect(ratio(null)).toBe('—')
    expect(ratio(undefined)).toBe('—')
  })
})

describe('duration', () => {
  it('reads a delay in the unit a person would use', () => {
    expect(duration(850)).toBe('850 ms')
    expect(duration(999)).toBe('999 ms')
    expect(duration(1000)).toBe('1.0 s')
    expect(duration(2450)).toBe('2.5 s')
  })

  it('shows an unmeasured delay as a dash', () => {
    expect(duration(null)).toBe('—')
    expect(duration(undefined)).toBe('—')
  })
})

describe('probability', () => {
  it('gives two places, and does not claim more precision than that', () => {
    expect(probability(0.62)).toBe('0.62')
    expect(probability(1)).toBe('1.00')
    expect(probability(0.049)).toBe('0.05')
  })

  it('says "less than" instead of printing a tiny number', () => {
    expect(probability(0.004)).toBe('<0.01')
  })
})
