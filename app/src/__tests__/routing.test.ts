/**
 * Hash routes: the links the CLI prints must open the same thing after a
 * reload, and anything unrecognised must land somewhere sensible.
 */

import { describe, expect, it } from 'vitest'
import { evaluationHref, parseRoute, routeHash, runHref, TAB_IDS } from '../routing'

describe('routing', () => {
  it('opens an evaluation from its link', () => {
    expect(parseRoute('#/evals/abc123')).toEqual({ tab: 'evals', evaluationId: 'abc123' })
  })

  it('opens a run on the History tab', () => {
    expect(parseRoute('#/runs/run-1')).toEqual({ tab: 'history', runId: 'run-1' })
    expect(parseRoute('#/runs')).toEqual({ tab: 'history' })
  })

  it('maps every tab to itself', () => {
    for (const tab of TAB_IDS) expect(parseRoute(`#/${tab}`)).toEqual({ tab })
  })

  it('falls back to the Workbench for anything else', () => {
    expect(parseRoute('')).toEqual({ tab: 'workbench' })
    expect(parseRoute('#/nowhere')).toEqual({ tab: 'workbench' })
    expect(parseRoute('#')).toEqual({ tab: 'workbench' })
  })

  it('ignores an id on a tab that has none', () => {
    expect(parseRoute('#/about/abc')).toEqual({ tab: 'about' })
  })

  it('survives ids that need escaping, and drops ones that are malformed', () => {
    const id = 'a b/c'
    expect(parseRoute(evaluationHref(id))).toEqual({ tab: 'evals', evaluationId: id })
    expect(parseRoute('#/evals/%E0%A4%A')).toEqual({ tab: 'evals' })
  })

  it('round-trips every route', () => {
    for (const route of [
      { tab: 'evals' as const, evaluationId: 'e1' },
      { tab: 'history' as const, runId: 'r1' },
      { tab: 'guardrails' as const },
      { tab: 'evals' as const }
    ]) {
      expect(parseRoute(routeHash(route))).toEqual(route)
    }
    expect(runHref('r1')).toBe('#/runs/r1')
  })
})
