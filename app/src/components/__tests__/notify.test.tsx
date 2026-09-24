import { act, fireEvent, render, screen } from '@testing-library/react'
import { ToastProvider } from '@readysetcloud/ui'
import { describe, expect, it } from 'vitest'
import { useNotify } from '../notify'

function Notifier() {
  const notify = useNotify()
  return (
    <button type="button" onClick={() => notify('Run deleted', { variant: 'success' })}>
      notify
    </button>
  )
}

describe('useNotify', () => {
  it('shows a design-system toast inside a ToastProvider', () => {
    render(
      <ToastProvider>
        <Notifier />
      </ToastProvider>
    )

    act(() => fireEvent.click(screen.getByRole('button', { name: 'notify' })))

    expect(screen.getByRole('region', { name: 'Notifications' })).toHaveTextContent('Run deleted')
  })

  it('does nothing, rather than throwing, without one', () => {
    render(<Notifier />)

    expect(() => fireEvent.click(screen.getByRole('button', { name: 'notify' }))).not.toThrow()
    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
  })
})
