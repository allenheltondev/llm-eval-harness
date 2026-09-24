/**
 * Hash routes: a link to one evaluation or one run that survives a reload.
 *
 * The app has no router and does not need one — five tabs and two kinds of
 * "open this thing" link. The routes live in the URL fragment (`#/evals/<id>`)
 * so they work unchanged behind any static host: the server never sees the
 * fragment, so there is no rewrite rule to configure. The CLI prints exactly
 * these links (`nimbus eval --remote`), which is the reason they exist.
 *
 *   #/workbench  #/evals  #/history  #/guardrails  #/about
 *   #/evals/<evaluation id>
 *   #/runs/<run id>          (the History tab, with that run open)
 */

import { useCallback, useEffect, useState } from 'react'

export type TabId = 'workbench' | 'evals' | 'history' | 'guardrails' | 'about'

export const TAB_IDS: TabId[] = ['workbench', 'evals', 'history', 'guardrails', 'about']

export interface Route {
  tab: TabId
  /** With `tab: 'evals'` — the evaluation to open. */
  evaluationId?: string
  /** With `tab: 'history'` — the run to open. */
  runId?: string
}

export const DEFAULT_ROUTE: Route = { tab: 'workbench' }

function isTab(value: string): value is TabId {
  return (TAB_IDS as string[]).includes(value)
}

function decode(segment: string | undefined): string | undefined {
  if (!segment) return undefined
  try {
    return decodeURIComponent(segment)
  } catch {
    return undefined
  }
}

/** `#/evals/abc` -> `{ tab: 'evals', evaluationId: 'abc' }`; anything unknown -> the default. */
export function parseRoute(hash: string): Route {
  const [first, second] = hash.replace(/^#\/?/, '').split('/')
  if (first === 'runs') {
    const runId = decode(second)
    return runId ? { tab: 'history', runId } : { tab: 'history' }
  }
  if (first && isTab(first)) {
    const evaluationId = first === 'evals' ? decode(second) : undefined
    return evaluationId ? { tab: 'evals', evaluationId } : { tab: first }
  }
  return DEFAULT_ROUTE
}

/** The inverse of `parseRoute`. */
export function routeHash(route: Route): string {
  if (route.tab === 'evals' && route.evaluationId) return evaluationHref(route.evaluationId)
  if (route.tab === 'history' && route.runId) return runHref(route.runId)
  return `#/${route.tab}`
}

export function evaluationHref(evaluationId: string): string {
  return `#/evals/${encodeURIComponent(evaluationId)}`
}

export function runHref(runId: string): string {
  return `#/runs/${encodeURIComponent(runId)}`
}

/**
 * The current route, following the address bar, and a way to change it.
 *
 * `navigate` updates state synchronously (rather than waiting for the
 * asynchronous `hashchange`) and writes the hash for the address bar; a
 * pasted link or the back button arrives through `hashchange`.
 */
export function useHashRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))

  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  const navigate = useCallback((next: Route) => {
    setRoute(next)
    const hash = routeHash(next)
    if (window.location.hash !== hash) window.location.hash = hash
  }, [])

  return [route, navigate]
}
