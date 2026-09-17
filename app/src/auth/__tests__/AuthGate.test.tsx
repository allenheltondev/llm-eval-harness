/**
 * AuthGate: /health decides between "render the app" and "render sign-in",
 * and a 401 from the API afterwards drops the session with a notice.
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, mockFetch, urlsOf } from '../../api/__tests__/helpers'
import { http, setAuthTokenProvider } from '../../api/http'
import AuthGate, { SESSION_EXPIRED_NOTICE } from '../AuthGate'
import { AUTH_STORAGE_KEY, configureAuth, getAuthConfig, readSession } from '../core'

const HEALTH_OPEN = { status: 'ok', auth: { required: false } }
const HEALTH_AUTH = {
  status: 'ok',
  auth: {
    required: true,
    provider: 'cognito',
    region: 'eu-west-1',
    user_pool_id: 'eu-west-1_Pool',
    client_id: 'client-9'
  }
}

function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').padEnd(4, '=')
  return `h.${b64(JSON.stringify(payload))}.s`
}

function seedSession(expiresAt = 9e9) {
  localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ idToken: fakeJwt({ email: 'a@b.c' }), refreshToken: 'rt', expiresAt })
  )
}

beforeEach(() => {
  localStorage.clear()
  configureAuth(null)
  setAuthTokenProvider(null)
})

afterEach(() => {
  vi.unstubAllGlobals()
  setAuthTokenProvider(null)
  configureAuth(null)
})

describe('AuthGate', () => {
  it('shows the fallback while /health is in flight', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {}))
    )
    render(
      <AuthGate fallback={<p>booting</p>}>
        <p>app</p>
      </AuthGate>
    )
    expect(screen.getByText('booting')).toBeInTheDocument()
    expect(screen.queryByText('app')).not.toBeInTheDocument()
  })

  it('renders the app directly when auth is not required, without touching the auth core', async () => {
    const spy = mockFetch(jsonResponse(HEALTH_OPEN))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByText('app')).toBeInTheDocument()
    expect(urlsOf(spy)).toEqual(['http://localhost:8000/api/v1/health'])
    expect(getAuthConfig()).toBeNull()
    expect(screen.queryByTestId('login-page')).not.toBeInTheDocument()
  })

  it('renders the app when /health omits the auth block (older server)', async () => {
    mockFetch(jsonResponse({ status: 'ok' }))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByText('app')).toBeInTheDocument()
  })

  it('renders the app when /health fails — a down server must not lock a local tool', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('refused')))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByText('app')).toBeInTheDocument()
  })

  it('shows the sign-in screen when auth is required and no session exists', async () => {
    mockFetch(jsonResponse(HEALTH_AUTH))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByTestId('login-page')).toBeInTheDocument()
    expect(screen.queryByText('app')).not.toBeInTheDocument()
    expect(getAuthConfig()).toEqual({ region: 'eu-west-1', clientId: 'client-9' })
  })

  it('renders the app when auth is required and a session exists, sending the bearer header', async () => {
    seedSession()
    const spy = mockFetch(jsonResponse(HEALTH_AUTH), jsonResponse({ runs: [] }))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByText('app')).toBeInTheDocument()

    await http.get('/runs')
    const headers = spy.mock.calls[1]?.[1]?.headers ?? {}
    expect(headers.authorization).toBe(`Bearer ${readSession()?.idToken}`)
  })

  it('drops the session and shows the expiry notice when the API answers 401', async () => {
    seedSession()
    mockFetch(
      jsonResponse(HEALTH_AUTH),
      jsonResponse({ error: { code: 'unauthorized', message: 'nope', detail: null } }, 401),
      jsonResponse({}) // RevokeToken
    )
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByText('app')).toBeInTheDocument()

    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 401 })
    })

    expect(await screen.findByTestId('login-page')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(SESSION_EXPIRED_NOTICE)
    expect(readSession()).toBeNull()
  })

  it('a 401 while already signed out does not raise a notice', async () => {
    mockFetch(
      jsonResponse(HEALTH_AUTH),
      jsonResponse({ error: { code: 'unauthorized', message: 'nope', detail: null } }, 401)
    )
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByTestId('login-page')
    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 401 })
    })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('does not apply a health result that arrives after unmount', async () => {
    let resolve!: (r: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(r => (resolve = r)))
    )
    const { unmount } = render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    // The request reaches `fetch` a microtask after mount (the header step is
    // async), so let it get there before pulling the component down.
    await act(async () => {})
    unmount()
    await act(async () => {
      resolve(jsonResponse(HEALTH_AUTH))
    })
    await waitFor(() => expect(getAuthConfig()).toBeNull())
  })
})
