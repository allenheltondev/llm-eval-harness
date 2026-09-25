// @vitest-environment-options {"url": "https://nimbus.readysetcloud.io/"}
/**
 * On nimbus.readysetcloud.io, Nimbus's session never leaves its own origin.
 *
 * This file runs on a real `https://nimbus.readysetcloud.io` origin, so the
 * auth package's parent-domain bridge sees the host it infers
 * `.readysetcloud.io` from, and jsdom stores the package's `Secure` cookies
 * the way a browser would. The session is written through the package's own
 * refresh path (Cognito faked at `fetch`).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  AUTH_KEY,
  configureAuth,
  getFreshIdToken,
  readSession,
  signOut,
  type AuthConfig
} from '@readysetcloud/ui/auth'
import { authConfig } from '../AuthGate'

const HEALTH_AUTH = { region: 'us-east-1', client_id: 'nimbus-client' }

function jwt(claims: object): string {
  const part = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, '')
  return `${part({ alg: 'none' })}.${part(claims)}.sig`
}

const FRESH_ID_TOKEN = jwt({ aud: 'nimbus-client', email: 'dev@example.com' })

/** An expired local session, so the next token request refreshes it. */
function seedExpiredSession() {
  localStorage.setItem(
    AUTH_KEY,
    JSON.stringify({ idToken: 'old', refreshToken: 'nimbus-refresh', expiresAt: 0 })
  )
}

function fakeCognito() {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ AuthenticationResult: { IdToken: FRESH_ID_TOKEN, ExpiresIn: 3600 } }),
          { status: 200 }
        )
      )
  )
}

function cookieNames(): string[] {
  return document.cookie
    .split(';')
    .map(part => part.trim().split('=')[0])
    .filter(Boolean)
}

function clearCookies() {
  for (const name of cookieNames()) {
    for (const domain of ['', '; Domain=.readysetcloud.io']) {
      document.cookie = `${name}=; Path=/; Max-Age=0${domain}; Secure`
    }
  }
}

async function refreshWith(config: AuthConfig) {
  configureAuth(config)
  seedExpiredSession()
  fakeCognito()
  return getFreshIdToken()
}

beforeEach(() => {
  clearCookies()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  configureAuth(null)
  clearCookies()
  localStorage.clear()
})

describe("Nimbus's session on nimbus.readysetcloud.io", () => {
  it('would be put in a parent-domain cookie under the package defaults', async () => {
    // The control: proves this origin really does switch the bridge on, so
    // the test below is not passing for want of a readysetcloud.io host.
    expect(await refreshWith({ region: 'us-east-1', clientId: 'nimbus-client' })).toBe(
      FRESH_ID_TOKEN
    )
    expect(cookieNames()).toContain('rsc_auth')
  })

  it('stays in localStorage, with no cookie a sibling subdomain could read', async () => {
    expect(await refreshWith(authConfig(HEALTH_AUTH))).toBe(FRESH_ID_TOKEN)

    expect(cookieNames()).toEqual([])
    expect(JSON.parse(localStorage.getItem(AUTH_KEY)!).idToken).toBe(FRESH_ID_TOKEN)
  })

  it("does not adopt a sibling app's session from the shared cookie", () => {
    const sibling = btoa(
      JSON.stringify({ idToken: jwt({ aud: 'other-app' }), refreshToken: 'x', expiresAt: 9e9 })
    ).replace(/=+$/, '')
    document.cookie = `rsc_auth=${sibling}; Path=/; Domain=.readysetcloud.io; Secure`
    configureAuth(authConfig(HEALTH_AUTH))

    expect(readSession()).toBeNull()
  })

  it("signs out without writing to the siblings' shared cookie", async () => {
    configureAuth(authConfig(HEALTH_AUTH))
    localStorage.setItem(
      AUTH_KEY,
      JSON.stringify({ idToken: FRESH_ID_TOKEN, expiresAt: Math.floor(Date.now() / 1000) + 600 })
    )

    await signOut()

    expect(cookieNames()).toEqual([])
    expect(localStorage.getItem(AUTH_KEY)).toBeNull()
  })
})
