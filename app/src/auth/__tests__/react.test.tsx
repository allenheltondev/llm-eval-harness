/** AuthProvider/useAuth: state tracks the session document, live. */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, mockFetch } from '../../api/__tests__/helpers'
import { AUTH_STORAGE_KEY, configureAuth, signIn } from '../core'
import { AuthProvider, useAuth } from '../react'

function Probe() {
  const { required, signedIn, user, signOut } = useAuth()
  return (
    <div>
      <span data-testid="required">{String(required)}</span>
      <span data-testid="signed-in">{String(signedIn)}</span>
      <span data-testid="email">{typeof user.email === 'string' ? user.email : ''}</span>
      <button type="button" onClick={() => void signOut()}>
        out
      </button>
    </div>
  )
}

function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `h.${b64(JSON.stringify(payload))}.s`
}

beforeEach(() => {
  localStorage.clear()
  configureAuth({ region: 'us-east-1', clientId: 'c' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  configureAuth(null)
})

describe('useAuth outside a provider', () => {
  it('answers "not required, signed out" with inert actions', async () => {
    render(<Probe />)
    expect(screen.getByTestId('required').textContent).toBe('false')
    expect(screen.getByTestId('signed-in').textContent).toBe('false')
    const spy = mockFetch(jsonResponse({}))
    await act(async () => {
      screen.getByRole('button').click()
    })
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('AuthProvider', () => {
  it('starts from the stored session and exposes its claims', () => {
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: fakeJwt({ email: 'a@b.c' }), expiresAt: 9e9 })
    )
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    )
    expect(screen.getByTestId('required').textContent).toBe('true')
    expect(screen.getByTestId('signed-in').textContent).toBe('true')
    expect(screen.getByTestId('email').textContent).toBe('a@b.c')
  })

  it('re-renders on sign-in and sign-out', async () => {
    mockFetch(
      jsonResponse({ AuthenticationResult: { IdToken: fakeJwt({ email: 'n@e.w' }) } }),
      jsonResponse({})
    )
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    )
    expect(screen.getByTestId('signed-in').textContent).toBe('false')

    await act(async () => {
      await signIn('n@e.w', 'pw')
    })
    expect(screen.getByTestId('signed-in').textContent).toBe('true')
    expect(screen.getByTestId('email').textContent).toBe('n@e.w')

    await act(async () => {
      screen.getByRole('button').click()
    })
    expect(screen.getByTestId('signed-in').textContent).toBe('false')
    expect(screen.getByTestId('email').textContent).toBe('')
  })

  it('picks up a sign-out made in another tab via the storage event', async () => {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 'x.y.z', expiresAt: 9e9 }))
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    )
    expect(screen.getByTestId('signed-in').textContent).toBe('true')
    localStorage.removeItem(AUTH_STORAGE_KEY)
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: AUTH_STORAGE_KEY }))
    })
    expect(screen.getByTestId('signed-in').textContent).toBe('false')
  })

  it('ignores storage events for other keys', async () => {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 'x.y.z', expiresAt: 9e9 }))
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    )
    localStorage.removeItem(AUTH_STORAGE_KEY)
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'something-else' }))
    })
    expect(screen.getByTestId('signed-in').textContent).toBe('true')
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: null }))
    })
    expect(screen.getByTestId('signed-in').textContent).toBe('false')
  })
})
