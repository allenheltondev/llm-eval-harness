/** LoginPage: every step drives the right core call and reports outcomes. */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bodyOf, headersOf, jsonResponse, mockFetch } from '../../api/__tests__/helpers'
import { configureAuth, readSession } from '../core'
import LoginPage, { PASSWORD_REQUIREMENTS } from '../LoginPage'

function cognitoError(type: string, message = 'raw') {
  return jsonResponse({ __type: type, message }, 400)
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

async function submit(formName: string) {
  await act(async () => {
    fireEvent.submit(screen.getByRole('form', { name: formName }))
  })
}

beforeEach(() => {
  localStorage.clear()
  configureAuth({ region: 'us-east-1', clientId: 'client-1' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  configureAuth(null)
})

describe('sign in', () => {
  it('signs in with the trimmed email and stores the session', async () => {
    const spy = mockFetch(jsonResponse({ AuthenticationResult: { IdToken: 'id.tok.en' } }))
    render(<LoginPage />)
    type('Email', '  a@b.c ')
    type('Password', 'secret')
    await submit('Sign in')
    expect(bodyOf(spy)).toMatchObject({ AuthParameters: { USERNAME: 'a@b.c', PASSWORD: 'secret' } })
    expect(readSession()?.idToken).toBe('id.tok.en')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows the friendly error on a rejection and stays on the form', async () => {
    mockFetch(cognitoError('NotAuthorizedException'))
    render(<LoginPage />)
    type('Email', 'a@b.c')
    type('Password', 'wrong')
    await submit('Sign in')
    expect(screen.getByRole('alert')).toHaveTextContent('Incorrect email or password.')
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeDisabled()
  })

  it('shows a generic line for a non-auth failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.reject(new Error('x')) })
    )
    render(<LoginPage />)
    type('Email', 'a@b.c')
    type('Password', 'pw')
    await submit('Sign in')
    // An ok response with an unreadable body: no tokens, so SessionNotPersisted.
    expect(screen.getByRole('alert')).toHaveTextContent("isn't keeping you signed in")
  })

  it('disables the button while the request is in flight', async () => {
    let resolve!: (r: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(r => (resolve = r)))
    )
    render(<LoginPage />)
    type('Email', 'a@b.c')
    type('Password', 'pw')
    await act(async () => {
      fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }))
    })
    expect(screen.getByRole('button', { name: 'Signing in…' })).toBeDisabled()
    await act(async () => {
      resolve(cognitoError('NotAuthorizedException'))
    })
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeDisabled()
  })

  it('renders the notice it was given', () => {
    render(<LoginPage notice="Your session has ended" />)
    expect(screen.getByRole('status')).toHaveTextContent('Your session has ended')
  })
})

describe('new password required', () => {
  it('moves to the new-password step and completes the challenge', async () => {
    const spy = mockFetch(
      jsonResponse({ ChallengeName: 'NEW_PASSWORD_REQUIRED', Session: 'sess-7' }),
      jsonResponse({ AuthenticationResult: { IdToken: 'id.tok.en' } })
    )
    render(<LoginPage />)
    type('Email', 'a@b.c')
    type('Password', 'Temp1234')
    await submit('Sign in')

    expect(screen.getByRole('status')).toHaveTextContent('Choose a new password')
    expect(screen.getByText(PASSWORD_REQUIREMENTS)).toBeInTheDocument()
    type('New password', 'Better1234')
    await submit('Set a new password')

    expect(headersOf(spy, 1)['x-amz-target']).toBe(
      'AWSCognitoIdentityProviderService.RespondToAuthChallenge'
    )
    expect(bodyOf(spy, 1)).toMatchObject({
      Session: 'sess-7',
      ChallengeResponses: { USERNAME: 'a@b.c', NEW_PASSWORD: 'Better1234' }
    })
    expect(readSession()?.idToken).toBe('id.tok.en')
  })

  it('reports a rejected new password', async () => {
    mockFetch(
      jsonResponse({ ChallengeName: 'NEW_PASSWORD_REQUIRED', Session: 'sess-7' }),
      cognitoError('InvalidPasswordException')
    )
    render(<LoginPage />)
    type('Email', 'a@b.c')
    type('Password', 'Temp1234')
    await submit('Sign in')
    type('New password', 'weak')
    await submit('Set a new password')
    expect(screen.getByRole('alert')).toHaveTextContent("doesn't meet the requirements")
    expect(screen.getByRole('form', { name: 'Set a new password' })).toBeInTheDocument()
  })
})

describe('forgot password', () => {
  it('walks forgot -> reset -> back to sign in with the right calls', async () => {
    const spy = mockFetch(jsonResponse({}), jsonResponse({}))
    render(<LoginPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    expect(screen.getByRole('form', { name: 'Reset your password' })).toBeInTheDocument()

    type('Email', 'a@b.c')
    await submit('Reset your password')
    expect(headersOf(spy, 0)['x-amz-target']).toBe(
      'AWSCognitoIdentityProviderService.ForgotPassword'
    )
    expect(screen.getByRole('status')).toHaveTextContent('reset code is on its way')

    type('Reset code', ' 123456 ')
    type('New password', 'Fresh1234')
    await submit('Choose a new password')
    expect(bodyOf(spy, 1)).toEqual({
      ClientId: 'client-1',
      Username: 'a@b.c',
      ConfirmationCode: '123456',
      Password: 'Fresh1234'
    })
    expect(screen.getByRole('status')).toHaveTextContent('Password updated')
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    // the email survives, the secrets do not
    expect(screen.getByLabelText('Email')).toHaveValue('a@b.c')
    expect(screen.getByLabelText('Password')).toHaveValue('')
  })

  it('reports a bad code and stays on the reset step', async () => {
    mockFetch(jsonResponse({}), cognitoError('CodeMismatchException'))
    render(<LoginPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    type('Email', 'a@b.c')
    await submit('Reset your password')
    type('Reset code', '000000')
    type('New password', 'Fresh1234')
    await submit('Choose a new password')
    expect(screen.getByRole('alert')).toHaveTextContent("isn't right")
    expect(screen.getByRole('form', { name: 'Choose a new password' })).toBeInTheDocument()
  })

  it('"Back to sign in" clears notices from both the forgot and reset steps', async () => {
    mockFetch(cognitoError('LimitExceededException'), jsonResponse({}))
    render(<LoginPage notice="hello" />)
    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    type('Email', 'a@b.c')
    await submit('Reset your password')
    expect(screen.getByRole('alert')).toHaveTextContent('Too many attempts')
    fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    await submit('Reset your password')
    expect(screen.getByRole('form', { name: 'Choose a new password' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
