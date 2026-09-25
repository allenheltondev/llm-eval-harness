/**
 * Nimbus's session is isolated from the Ready, Set, Cloud parent-domain
 * cookie. Driven through the package's own cookie bridge (jsdom's host is
 * `localhost`, so the bridge is switched on with an explicit domain).
 *
 * Only reads are observable here: the package writes its cookie `Secure`,
 * which jsdom's http://localhost does not store, so sign-out's `signed_out`
 * marker cannot be asserted -- it goes to the same (renamed) cookie.
 */

import { afterEach, describe, expect, it } from 'vitest'
import { AUTH_KEY, configureAuth, isSignedIn, readSession } from '@readysetcloud/ui/auth'
import { sharedCookieName } from '../AuthGate'

function encode(session: object): string {
  const base64 = btoa(JSON.stringify(session))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return encodeURIComponent(base64)
}

function setCookie(name: string, value: string) {
  document.cookie = `${name}=${value}; Path=/`
}

const SIBLING_SESSION = {
  idToken: 'header.eyJhdWQiOiJzaWJsaW5nLWFwcCJ9.sig',
  refreshToken: 'sibling-refresh',
  expiresAt: Math.floor(Date.now() / 1000) + 3600
}

function configure(name?: string) {
  configureAuth({
    region: 'us-east-1',
    clientId: 'nimbus-client',
    sharedCookieDomain: 'localhost',
    ...(name ? { sharedCookieName: name } : {})
  })
}

afterEach(() => {
  for (const name of ['rsc_auth', sharedCookieName('nimbus-client')]) {
    document.cookie = `${name}=; Path=/; Max-Age=0`
  }
  localStorage.removeItem(AUTH_KEY)
  configureAuth(null)
})

describe("Nimbus's shared session cookie", () => {
  it('is named for this app client', () => {
    expect(sharedCookieName('abc123')).toBe('nimbus_auth_abc123')
  })

  it("does not pick up a sibling app's session from the RSC cookie", () => {
    setCookie('rsc_auth', encode(SIBLING_SESSION))

    // With the package default, the sibling's tokens would be adopted...
    configure()
    expect(readSession()?.refreshToken).toBe('sibling-refresh')
    localStorage.removeItem(AUTH_KEY)

    // ...with Nimbus's own name they are not.
    configure(sharedCookieName('nimbus-client'))
    expect(isSignedIn()).toBe(false)
  })
})
