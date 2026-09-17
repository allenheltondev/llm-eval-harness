/**
 * The bearer-token hook in the HTTP layer: with a provider installed every
 * JSON request and every NDJSON stream carries `authorization`, a 401 is
 * reported back, and without a provider nothing changes.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, setAuthTokenProvider, withAuthHeader } from '../http'
import { getNdjson, streamNdjson } from '../stream'
import { headersOf, jsonResponse, mockFetch, ndjsonResponse } from './helpers'

afterEach(() => {
  setAuthTokenProvider(null)
  vi.unstubAllGlobals()
})

describe('withAuthHeader', () => {
  it('returns the headers untouched without a provider', async () => {
    const headers = { accept: 'application/json' }
    expect(await withAuthHeader(headers)).toBe(headers)
  })

  it('adds a bearer header from the provider', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    expect(await withAuthHeader({ accept: 'x' })).toEqual({ accept: 'x', authorization: 'Bearer tok' })
  })

  it('leaves the headers alone when the provider has no token or throws', async () => {
    setAuthTokenProvider({ getToken: async () => null })
    expect(await withAuthHeader({ accept: 'x' })).toEqual({ accept: 'x' })
    setAuthTokenProvider({
      getToken: async () => {
        throw new Error('refresh exploded')
      }
    })
    expect(await withAuthHeader({ accept: 'x' })).toEqual({ accept: 'x' })
  })

  it('never overrides an explicit authorization header', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    expect(await withAuthHeader({ authorization: 'Basic abc' })).toEqual({
      authorization: 'Basic abc'
    })
  })
})

describe('JSON requests', () => {
  it('send the bearer header when a provider is installed', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    const spy = mockFetch(jsonResponse({ ok: true }))
    await http.get('/runs')
    expect(headersOf(spy).authorization).toBe('Bearer tok')
  })

  it('send no bearer header without a provider', async () => {
    const spy = mockFetch(jsonResponse({ ok: true }))
    await http.get('/runs')
    expect(headersOf(spy).authorization).toBeUndefined()
  })

  it('report a 401 to the provider and still reject with the ApiError', async () => {
    const onUnauthorized = vi.fn()
    setAuthTokenProvider({ getToken: async () => 'tok', onUnauthorized })
    mockFetch(jsonResponse({ error: { code: 'unauthorized', message: 'no', detail: null } }, 401))
    await expect(http.get('/runs')).rejects.toMatchObject({ status: 401, code: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('do not report other failures', async () => {
    const onUnauthorized = vi.fn()
    setAuthTokenProvider({ getToken: async () => 'tok', onUnauthorized })
    mockFetch(jsonResponse({ error: { code: 'not_found', message: 'no', detail: null } }, 404))
    await expect(http.get('/runs/x')).rejects.toMatchObject({ status: 404 })
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('tolerate a provider without an onUnauthorized hook', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    mockFetch(jsonResponse({}, 401))
    await expect(http.get('/runs')).rejects.toMatchObject({ status: 401 })
  })
})

describe('NDJSON streams', () => {
  it('POST streams carry the bearer header', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    const spy = mockFetch(ndjsonResponse(['{"type":"a"}\n']))
    const events: unknown[] = []
    await streamNdjson('/runs', {}, { onEvent: (e) => events.push(e) })
    expect(headersOf(spy).authorization).toBe('Bearer tok')
    expect(headersOf(spy).accept).toBe('application/x-ndjson')
    expect(events).toEqual([{ type: 'a' }])
  })

  it('GET streams carry the bearer header', async () => {
    setAuthTokenProvider({ getToken: async () => 'tok' })
    const spy = mockFetch(ndjsonResponse([]))
    await getNdjson('/evaluations/e1/events', { onEvent: () => undefined })
    expect(headersOf(spy).authorization).toBe('Bearer tok')
  })

  it('a 401 on a stream is reported to the provider', async () => {
    const onUnauthorized = vi.fn()
    setAuthTokenProvider({ getToken: async () => 'tok', onUnauthorized })
    mockFetch(jsonResponse({ error: { code: 'unauthorized', message: 'no', detail: null } }, 401))
    await expect(streamNdjson('/runs', {}, { onEvent: () => undefined })).rejects.toMatchObject({
      status: 401
    })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })
})
