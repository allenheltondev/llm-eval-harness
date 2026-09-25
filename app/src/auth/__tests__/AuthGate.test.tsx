/**
 * AuthGate: /health decides between "render the app" and "sign in" (the
 * Ready, Set, Cloud auth package's flows), a 401 from the API afterwards
 * drops the session with a notice, and a 403 -- signed in, but not granted
 * this stack -- shows the no-access screen.
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTH_KEY, configureAuth, getConfig, readSession } from '@readysetcloud/ui/auth'
import { jsonResponse, mockFetch, urlsOf } from '../../api/__tests__/helpers'
import { http, setAuthTokenProvider } from '../../api/http'
import AuthGate, { SESSION_EXPIRED_NOTICE } from '../AuthGate'
import { useSession } from '../session'

const HEALTH_OPEN = { status: 'ok', auth: { required: false, supports_required_group: true } }
const HEALTH_AUTH = {
  status: 'ok',
  auth: {
    required: true,
    provider: 'cognito',
    region: 'eu-west-1',
    user_pool_id: 'eu-west-1_Pool',
    client_id: 'client-9',
    required_group: 'nimbus',
    supports_required_group: true
  }
}

function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `h.${b64(JSON.stringify(payload))}.s`
}

function seedSession(claims: Record<string, unknown> = { email: 'ada@example.com' }) {
  localStorage.setItem(
    AUTH_KEY,
    JSON.stringify({
      idToken: fakeJwt(claims),
      refreshToken: 'rt',
      expiresAt: Math.floor(Date.now() / 1000) + 3600
    })
  )
}

const ERROR_BODY = (code: string) => ({ error: { code, message: 'nope', detail: null } })

/** Shows what the app sees of the session. */
function WhoAmI() {
  const { required, signedIn, user } = useSession()
  return (
    <p>
      app {String(required)} {String(signedIn)} {String(user.email ?? '')}
    </p>
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

  it('shows a loading page by default while /health is in flight', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {}))
    )
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('renders the app directly when auth is not required, with no session and no auth config', async () => {
    const spy = mockFetch(jsonResponse(HEALTH_OPEN))
    render(
      <AuthGate>
        <WhoAmI />
      </AuthGate>
    )
    expect(await screen.findByText('app false false')).toBeInTheDocument()
    expect(urlsOf(spy)).toEqual(['http://localhost:8000/api/v1/health'])
    expect(await getConfig()).toBeNull()
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

  it('shows the sign-in form, configured from /health, when no session exists', async () => {
    mockFetch(jsonResponse(HEALTH_AUTH))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
    expect(screen.queryByText('app')).not.toBeInTheDocument()
    expect(await getConfig()).toMatchObject({
      region: 'eu-west-1',
      clientId: 'client-9',
      // Not the package's shared `rsc_auth`: sibling RSC apps' tokens are for
      // their own clients, and our sign-out must not mark theirs signed out.
      sharedCookieName: 'nimbus_auth_client-9'
    })
  })

  it('switches between sign-in, account creation and password reset', async () => {
    mockFetch(jsonResponse(HEALTH_AUTH))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByRole('heading', { name: 'Sign in' })

    fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' }))
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Create an account' }))
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('offers no sign-up on an invitation-only pool (no required group)', async () => {
    mockFetch(jsonResponse({ ...HEALTH_AUTH, auth: { ...HEALTH_AUTH.auth, required_group: null } }))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByRole('heading', { name: 'Sign in' })

    expect(screen.queryByRole('button', { name: 'Create an account' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Forgot password?' })).toBeInTheDocument()
  })

  it('renders the app with the session when one exists, sending the bearer header', async () => {
    seedSession()
    const spy = mockFetch(jsonResponse(HEALTH_AUTH), jsonResponse({ items: [] }))
    render(
      <AuthGate>
        <WhoAmI />
      </AuthGate>
    )
    expect(await screen.findByText('app true true ada@example.com')).toBeInTheDocument()

    await http.get('/runs')
    const headers = spy.mock.calls[1]?.[1]?.headers ?? {}
    expect(headers.authorization).toBe(`Bearer ${readSession()?.idToken}`)
  })

  it('drops the session and shows the expiry notice when the API answers 401', async () => {
    seedSession()
    mockFetch(
      jsonResponse(HEALTH_AUTH),
      jsonResponse(ERROR_BODY('unauthorized'), 401),
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

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByText(SESSION_EXPIRED_NOTICE)).toBeInTheDocument()
    expect(readSession()).toBeNull()
  })

  it('a 401 while already signed out raises no notice', async () => {
    mockFetch(jsonResponse(HEALTH_AUTH), jsonResponse(ERROR_BODY('unauthorized'), 401))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByRole('heading', { name: 'Sign in' })
    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 401 })
    })
    expect(screen.queryByText(SESSION_EXPIRED_NOTICE)).not.toBeInTheDocument()
  })

  it('shows who is signed in and the group to ask for when the API answers 403', async () => {
    seedSession({ email: 'ada@example.com', given_name: 'Ada', family_name: 'Lovelace' })
    mockFetch(jsonResponse(HEALTH_AUTH), jsonResponse(ERROR_BODY('forbidden'), 403))
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByText('app')

    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 403 })
    })

    const screenBody = await screen.findByTestId('no-access')
    expect(screenBody).toHaveTextContent('You are signed in as Ada Lovelace')
    expect(screenBody).toHaveTextContent('add you to the nimbus group')
    expect(screen.queryByText('app')).not.toBeInTheDocument()
    // Still signed in: granting the account fixes this, not signing in again.
    expect(readSession()).not.toBeNull()
  })

  it('signing out of the no-access screen returns to sign-in, and a new session starts over', async () => {
    seedSession()
    mockFetch(
      jsonResponse(HEALTH_AUTH),
      jsonResponse(ERROR_BODY('forbidden'), 403),
      jsonResponse({}) // RevokeToken
    )
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByText('app')
    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 403 })
    })

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(readSession()).toBeNull()

    // Another account signs in (another tab, say): the app, not the old refusal.
    await act(async () => {
      seedSession({ email: 'grace@example.com' })
      window.dispatchEvent(new StorageEvent('storage', { key: AUTH_KEY }))
    })
    expect(await screen.findByText('app')).toBeInTheDocument()
  })

  it('without a named group the no-access screen still says to ask for access', async () => {
    seedSession({})
    mockFetch(
      jsonResponse({ ...HEALTH_AUTH, auth: { ...HEALTH_AUTH.auth, required_group: null } }),
      jsonResponse(ERROR_BODY('forbidden'), 403)
    )
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByText('app')
    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 403 })
    })

    const screenBody = await screen.findByTestId('no-access')
    expect(screenBody).toHaveTextContent('This account has not been granted access')
    expect(screenBody).toHaveTextContent("Ask the stack's owner for access.")
  })
})

/**
 * The sign-in flows against a fake Cognito: /health answers from the API, and
 * every cognito-idp call is answered by operation (its X-Amz-Target).
 */
describe('AuthGate sign-in, through the package flows', () => {
  type CognitoReply = { status?: number; body: Record<string, unknown> }

  function stubServers(cognito: Record<string, CognitoReply>, apiReplies: Response[] = []) {
    const calls: string[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/health')) return jsonResponse(HEALTH_AUTH)
      if (url.includes('cognito-idp.')) {
        const headers = new Headers(init?.headers)
        const operation = (headers.get('x-amz-target') ?? '').split('.').pop() ?? ''
        calls.push(operation)
        const reply = cognito[operation] ?? { status: 400, body: { __type: 'Unexpected' } }
        return jsonResponse(reply.body, reply.status ?? 200)
      }
      calls.push(url)
      return apiReplies.shift() ?? jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    return calls
  }

  async function submitCredentials() {
    fireEvent.change(await screen.findByLabelText('Email'), {
      target: { value: 'ada@example.com' }
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Correct1horse' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    })
  }

  const SIGNED_IN = {
    body: {
      AuthenticationResult: {
        IdToken: fakeJwt({ email: 'ada@example.com', exp: Math.floor(Date.now() / 1000) + 3600 }),
        RefreshToken: 'rt',
        ExpiresIn: 3600
      }
    }
  }

  it('signs in and renders the app', async () => {
    const calls = stubServers({ InitiateAuth: SIGNED_IN })
    render(
      <AuthGate>
        <WhoAmI />
      </AuthGate>
    )

    await submitCredentials()

    expect(await screen.findByText('app true true ada@example.com')).toBeInTheDocument()
    expect(calls).toContain('InitiateAuth')
    expect(readSession()).not.toBeNull()
  })

  it('clears the session-ended notice once signed in again', async () => {
    seedSession()
    stubServers({ InitiateAuth: SIGNED_IN, RevokeToken: { body: {} } }, [
      jsonResponse(ERROR_BODY('unauthorized'), 401)
    ])
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )
    await screen.findByText('app')
    await act(async () => {
      await expect(http.get('/runs')).rejects.toMatchObject({ status: 401 })
    })
    expect(await screen.findByText(SESSION_EXPIRED_NOTICE)).toBeInTheDocument()

    await submitCredentials()

    expect(await screen.findByText('app')).toBeInTheDocument()
    expect(screen.queryByText(SESSION_EXPIRED_NOTICE)).not.toBeInTheDocument()
  })

  it('takes an unconfirmed account to the confirmation step', async () => {
    const calls = stubServers({
      InitiateAuth: { status: 400, body: { __type: 'UserNotConfirmedException' } },
      ResendConfirmationCode: { body: {} }
    })
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )

    await submitCredentials()

    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeInTheDocument()
    expect(calls).toContain('ResendConfirmationCode')
  })

  it('takes an account that must reset its password to the reset step', async () => {
    const calls = stubServers({
      InitiateAuth: { status: 400, body: { __type: 'PasswordResetRequiredException' } },
      ForgotPassword: { body: {} }
    })
    render(
      <AuthGate>
        <p>app</p>
      </AuthGate>
    )

    await submitCredentials()

    expect(await screen.findByRole('heading', { name: 'Reset your password' })).toBeInTheDocument()
    expect(calls).toContain('ForgotPassword')
  })
})

describe('session', () => {
  it('outside sign-in there is no session, and signing out is a no-op', async () => {
    const { NO_AUTH } = await import('../session')
    expect(NO_AUTH).toMatchObject({ required: false, signedIn: false, user: {} })
    await expect(NO_AUTH.signOut()).resolves.toBeUndefined()
  })
})
