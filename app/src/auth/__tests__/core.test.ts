/**
 * The auth core: what goes over the wire to cognito-idp, what lands in
 * localStorage, and how expiry/refresh/revocation behave.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  bodyOf,
  headersOf,
  jsonResponse,
  mockFetch,
  urlOf,
  urlsOf
} from '../../api/__tests__/helpers'
import {
  AUTH_STORAGE_KEY,
  AuthError,
  claims,
  completeNewPassword,
  configureAuth,
  confirmForgotPassword,
  errorMessage,
  forgotPassword,
  getAuthConfig,
  getFreshIdToken,
  isAuthError,
  isSignedIn,
  onAuthChange,
  readSession,
  signIn,
  signOut
} from '../core'

const CONFIG = { region: 'us-east-1', clientId: 'client-123' }
const IDP_URL = 'https://cognito-idp.us-east-1.amazonaws.com/'

/** A structurally valid JWT carrying `payload` (signature is irrelevant here). */
function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${b64('{"alg":"RS256","kid":"k"}')}.${b64(JSON.stringify(payload))}.sig`
}

function cognitoError(type: string, message = 'raw message', status = 400) {
  return jsonResponse({ __type: type, message }, status)
}

function storedSession() {
  return JSON.parse(localStorage.getItem(AUTH_STORAGE_KEY) ?? 'null')
}

beforeEach(() => {
  localStorage.clear()
  configureAuth(CONFIG)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  configureAuth(null)
})

describe('configuration', () => {
  it('stores and clears the config', () => {
    expect(getAuthConfig()).toEqual(CONFIG)
    configureAuth(null)
    expect(getAuthConfig()).toBeNull()
  })

  it('refuses every operation with ConfigMissing when unconfigured', async () => {
    configureAuth(null)
    const spy = mockFetch(jsonResponse({}))
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({ code: 'ConfigMissing' })
    await expect(forgotPassword('a@b.c')).rejects.toMatchObject({ code: 'ConfigMissing' })
    await expect(confirmForgotPassword('a@b.c', '1', 'pw')).rejects.toMatchObject({
      code: 'ConfigMissing'
    })
    await expect(completeNewPassword('a@b.c', 'pw', 's')).rejects.toMatchObject({
      code: 'ConfigMissing'
    })
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('error translation', () => {
  it('maps known Cognito exceptions to friendly copy', () => {
    expect(errorMessage({ __type: 'NotAuthorizedException' })).toBe('Incorrect email or password.')
    expect(errorMessage({ __type: 'x#UserNotFoundException' })).toBe('Incorrect email or password.')
    expect(errorMessage({ __type: 'CodeMismatchException: extra' })).toContain("isn't right")
  })

  it('falls back to the raw message, then to a generic line', () => {
    expect(errorMessage({ __type: 'SomethingNew', message: 'raw' })).toBe('raw')
    expect(errorMessage({})).toBe('Something went wrong — please try again.')
    expect(errorMessage({ message: 42 })).toBe('Something went wrong — please try again.')
  })

  it('isAuthError narrows', () => {
    expect(isAuthError(new AuthError('m', 'c'))).toBe(true)
    expect(isAuthError(new Error('m'))).toBe(false)
    expect(new AuthError('m', 'c').name).toBe('AuthError')
  })
})

describe('the session document', () => {
  it('reads null when absent, malformed, or the wrong shape', () => {
    expect(readSession()).toBeNull()
    localStorage.setItem(AUTH_STORAGE_KEY, 'not json')
    expect(readSession()).toBeNull()
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 1, expiresAt: 2 }))
    expect(readSession()).toBeNull()
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 't', expiresAt: 'soon' }))
    expect(readSession()).toBeNull()
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: 't', expiresAt: 2, refreshToken: 5 })
    )
    expect(readSession()).toBeNull()
    expect(isSignedIn()).toBe(false)
  })

  it('reads a well-formed session', () => {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 't', expiresAt: 2 }))
    expect(readSession()).toEqual({ idToken: 't', expiresAt: 2 })
    expect(isSignedIn()).toBe(true)
  })

  it('decodes claims from the id token, {} when signed out or undecodable', () => {
    expect(claims()).toEqual({})
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: fakeJwt({ email: 'a@b.c', sub: 'u1' }), expiresAt: 9e9 })
    )
    expect(claims()).toEqual({ email: 'a@b.c', sub: 'u1' })
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 'x.@@@.y', expiresAt: 9e9 }))
    expect(claims()).toEqual({})
  })
})

describe('signIn', () => {
  it('posts USER_PASSWORD_AUTH and stores the tokens', async () => {
    vi.useFakeTimers({ now: 1_000_000 * 1000 })
    const spy = mockFetch(
      jsonResponse({
        AuthenticationResult: { IdToken: 'id-1', RefreshToken: 'rt-1', ExpiresIn: 1800 }
      })
    )
    const listener = vi.fn()
    const off = onAuthChange(listener)

    const result = await signIn('a@b.c', 'secret')

    expect(result).toEqual({ kind: 'success' })
    expect(urlOf(spy)).toBe(IDP_URL)
    expect(headersOf(spy)['x-amz-target']).toBe('AWSCognitoIdentityProviderService.InitiateAuth')
    expect(headersOf(spy)['content-type']).toBe('application/x-amz-json-1.1')
    expect(bodyOf(spy)).toEqual({
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: 'client-123',
      AuthParameters: { USERNAME: 'a@b.c', PASSWORD: 'secret' }
    })
    expect(storedSession()).toEqual({
      idToken: 'id-1',
      refreshToken: 'rt-1',
      expiresAt: 1_000_000 + 1800
    })
    expect(listener).toHaveBeenCalledTimes(1)
    off()
  })

  it('defaults ExpiresIn to an hour', async () => {
    vi.useFakeTimers({ now: 5_000 * 1000 })
    mockFetch(jsonResponse({ AuthenticationResult: { IdToken: 'id-1' } }))
    await signIn('a@b.c', 'secret')
    expect(storedSession().expiresAt).toBe(5_000 + 3600)
  })

  it('returns the NEW_PASSWORD_REQUIRED challenge without storing anything', async () => {
    mockFetch(jsonResponse({ ChallengeName: 'NEW_PASSWORD_REQUIRED', Session: 'sess-1' }))
    await expect(signIn('a@b.c', 'temp')).resolves.toEqual({
      kind: 'newPasswordRequired',
      session: 'sess-1'
    })
    expect(readSession()).toBeNull()
  })

  it('tolerates a challenge with no Session', async () => {
    mockFetch(jsonResponse({ ChallengeName: 'NEW_PASSWORD_REQUIRED' }))
    await expect(signIn('a@b.c', 'temp')).resolves.toEqual({
      kind: 'newPasswordRequired',
      session: ''
    })
  })

  it('translates a Cognito rejection into an AuthError with its code', async () => {
    mockFetch(cognitoError('NotAuthorizedException'))
    const error = await signIn('a@b.c', 'wrong').catch(e => e)
    expect(error).toBeInstanceOf(AuthError)
    expect(error.code).toBe('NotAuthorizedException')
    expect(error.message).toBe('Incorrect email or password.')
    expect(readSession()).toBeNull()
  })

  it('uses the status as the code when the error body has no type', async () => {
    mockFetch(jsonResponse({}, 500))
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({ code: 'Http500' })
  })

  it('reports a network failure as NetworkError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({ code: 'NetworkError' })
  })

  it('treats a non-JSON error body as an empty one', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 502, json: () => Promise.reject(new Error()) })
    )
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({
      code: 'Http502',
      message: 'Something went wrong — please try again.'
    })
  })

  it('fails with SessionNotPersisted when storage refuses the tokens', async () => {
    mockFetch(jsonResponse({ AuthenticationResult: { IdToken: 'id-1' } }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({ code: 'SessionNotPersisted' })
    vi.restoreAllMocks()
  })

  it('ignores a success response with no tokens', async () => {
    mockFetch(jsonResponse({ AuthenticationResult: {} }))
    await expect(signIn('a@b.c', 'pw')).rejects.toMatchObject({ code: 'SessionNotPersisted' })
    expect(readSession()).toBeNull()
  })
})

describe('completeNewPassword', () => {
  it('answers the challenge and stores the tokens', async () => {
    const spy = mockFetch(
      jsonResponse({ AuthenticationResult: { IdToken: 'id-2', RefreshToken: 'rt-2' } })
    )
    await completeNewPassword('a@b.c', 'NewPassw0rd', 'sess-1')
    expect(headersOf(spy)['x-amz-target']).toBe(
      'AWSCognitoIdentityProviderService.RespondToAuthChallenge'
    )
    expect(bodyOf(spy)).toEqual({
      ClientId: 'client-123',
      ChallengeName: 'NEW_PASSWORD_REQUIRED',
      Session: 'sess-1',
      ChallengeResponses: { USERNAME: 'a@b.c', NEW_PASSWORD: 'NewPassw0rd' }
    })
    expect(storedSession()).toMatchObject({ idToken: 'id-2', refreshToken: 'rt-2' })
  })
})

describe('forgot password', () => {
  it('sends ForgotPassword then ConfirmForgotPassword with the right payloads', async () => {
    const spy = mockFetch(jsonResponse({}))
    await forgotPassword('a@b.c')
    await confirmForgotPassword('a@b.c', '123456', 'NewPassw0rd')
    expect(headersOf(spy, 0)['x-amz-target']).toBe(
      'AWSCognitoIdentityProviderService.ForgotPassword'
    )
    expect(bodyOf(spy, 0)).toEqual({ ClientId: 'client-123', Username: 'a@b.c' })
    expect(headersOf(spy, 1)['x-amz-target']).toBe(
      'AWSCognitoIdentityProviderService.ConfirmForgotPassword'
    )
    expect(bodyOf(spy, 1)).toEqual({
      ClientId: 'client-123',
      Username: 'a@b.c',
      ConfirmationCode: '123456',
      Password: 'NewPassw0rd'
    })
    expect(readSession()).toBeNull()
  })
})

describe('getFreshIdToken', () => {
  function seed(session: Record<string, unknown>) {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session))
  }

  it('is null when signed out', async () => {
    const spy = mockFetch(jsonResponse({}))
    await expect(getFreshIdToken()).resolves.toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('returns the stored token while it is more than a minute from expiry', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    const spy = mockFetch(jsonResponse({}))
    seed({ idToken: 'id-1', refreshToken: 'rt', expiresAt: 1000 + 61 })
    await expect(getFreshIdToken()).resolves.toBe('id-1')
    expect(spy).not.toHaveBeenCalled()
  })

  it('refreshes within the last minute, keeping the old refresh token', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    const spy = mockFetch(
      jsonResponse({ AuthenticationResult: { IdToken: 'id-2', ExpiresIn: 3600 } })
    )
    seed({ idToken: 'id-1', refreshToken: 'rt', expiresAt: 1000 + 60 })
    await expect(getFreshIdToken()).resolves.toBe('id-2')
    expect(bodyOf(spy)).toEqual({
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: 'client-123',
      AuthParameters: { REFRESH_TOKEN: 'rt' }
    })
    expect(storedSession()).toEqual({ idToken: 'id-2', refreshToken: 'rt', expiresAt: 4600 })
  })

  it('clears the session when expired with no refresh token', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    const spy = mockFetch(jsonResponse({}))
    seed({ idToken: 'id-1', expiresAt: 999 })
    await expect(getFreshIdToken()).resolves.toBeNull()
    expect(readSession()).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('clears the session when expired and unconfigured', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    configureAuth(null)
    seed({ idToken: 'id-1', refreshToken: 'rt', expiresAt: 999 })
    await expect(getFreshIdToken()).resolves.toBeNull()
    expect(readSession()).toBeNull()
  })

  it('clears the session when Cognito rejects the refresh token', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    mockFetch(cognitoError('NotAuthorizedException', 'Refresh Token has been revoked'))
    seed({ idToken: 'id-1', refreshToken: 'rt', expiresAt: 999 })
    await expect(getFreshIdToken()).resolves.toBeNull()
    expect(readSession()).toBeNull()
  })

  it('keeps the session (and yields null) when the refresh call cannot reach Cognito', async () => {
    vi.useFakeTimers({ now: 1000 * 1000 })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    seed({ idToken: 'id-1', refreshToken: 'rt', expiresAt: 999 })
    await expect(getFreshIdToken()).resolves.toBeNull()
    expect(storedSession()).toMatchObject({ idToken: 'id-1', refreshToken: 'rt' })
  })
})

describe('signOut', () => {
  it('drops the session, notifies, and revokes the refresh token', async () => {
    const spy = mockFetch(jsonResponse({}))
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: 'id', refreshToken: 'rt', expiresAt: 9e9 })
    )
    const listener = vi.fn()
    const off = onAuthChange(listener)
    await signOut()
    off()
    expect(readSession()).toBeNull()
    expect(listener).toHaveBeenCalledTimes(1)
    expect(headersOf(spy)['x-amz-target']).toBe('AWSCognitoIdentityProviderService.RevokeToken')
    expect(bodyOf(spy)).toEqual({ ClientId: 'client-123', Token: 'rt' })
  })

  it('skips revocation without a refresh token or config, and survives a failed revoke', async () => {
    const spy = mockFetch(cognitoError('InternalErrorException', 'boom', 500))
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ idToken: 'id', expiresAt: 9e9 }))
    await signOut()
    expect(spy).not.toHaveBeenCalled()

    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: 'id', refreshToken: 'rt', expiresAt: 9e9 })
    )
    await expect(signOut()).resolves.toBeUndefined()
    expect(urlsOf(spy)).toEqual([IDP_URL])
    expect(readSession()).toBeNull()

    configureAuth(null)
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ idToken: 'id', refreshToken: 'rt', expiresAt: 9e9 })
    )
    await signOut()
    expect(spy).toHaveBeenCalledTimes(1)
  })
})

describe('change notification', () => {
  it('a throwing listener does not block the others, and unsubscribing works', async () => {
    mockFetch(jsonResponse({ AuthenticationResult: { IdToken: 'id-1' } }))
    const bad = vi.fn(() => {
      throw new Error('listener bug')
    })
    const good = vi.fn()
    const offBad = onAuthChange(bad)
    const offGood = onAuthChange(good)
    await signIn('a@b.c', 'pw')
    expect(bad).toHaveBeenCalledTimes(1)
    expect(good).toHaveBeenCalledTimes(1)
    offGood()
    await signOut()
    expect(good).toHaveBeenCalledTimes(1)
    expect(bad).toHaveBeenCalledTimes(2)
    offBad()
  })
})
