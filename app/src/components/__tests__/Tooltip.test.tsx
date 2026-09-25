/**
 * Tooltip: shows its content on hover or keyboard focus, hides it on
 * mouse-leave / blur / Escape, describes its trigger via aria-describedby, and
 * never renders a bubble at all when no `content` was given.
 */

import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import Tooltip from '../Tooltip'

describe('Tooltip', () => {
  it('shows the content on hover and hides it again on mouse-leave', () => {
    render(
      <Tooltip content="Helpful hint">
        <button>Target</button>
      </Tooltip>
    )

    expect(screen.queryByText('Helpful hint')).not.toBeInTheDocument()

    fireEvent.mouseEnter(screen.getByText('Target').parentElement!)
    expect(screen.getByText('Helpful hint')).toBeInTheDocument()

    fireEvent.mouseLeave(screen.getByText('Target').parentElement!)
    expect(screen.queryByText('Helpful hint')).not.toBeInTheDocument()
  })

  it('never renders a bubble when content is omitted, even while hovered', () => {
    render(
      <Tooltip>
        <button>Target</button>
      </Tooltip>
    )

    fireEvent.mouseEnter(screen.getByText('Target').parentElement!)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('defaults to the top position and honors an explicit position', () => {
    const { rerender, container } = render(
      <Tooltip content="hi">
        <button>Target</button>
      </Tooltip>
    )
    fireEvent.mouseEnter(screen.getByText('Target').parentElement!)
    expect(container.querySelector('.bottom-full')).toBeInTheDocument()

    rerender(
      <Tooltip content="hi" position="right">
        <button>Target</button>
      </Tooltip>
    )
    fireEvent.mouseEnter(screen.getByText('Target').parentElement!)
    expect(container.querySelector('.left-full')).toBeInTheDocument()
  })

  it('opens on keyboard focus, links the trigger via aria-describedby, and closes on blur', () => {
    render(
      <Tooltip content="Helpful hint">
        <button>Target</button>
      </Tooltip>
    )
    const trigger = screen.getByRole('button', { name: 'Target' })
    expect(trigger).not.toHaveAttribute('aria-describedby')

    fireEvent.focus(trigger)
    const bubble = screen.getByRole('tooltip')
    expect(bubble).toHaveTextContent('Helpful hint')
    expect(trigger).toHaveAccessibleDescription('Helpful hint')

    fireEvent.blur(trigger)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(trigger).not.toHaveAttribute('aria-describedby')
  })

  it('closes on Escape', () => {
    render(
      <Tooltip content="Helpful hint">
        <button>Target</button>
      </Tooltip>
    )
    const trigger = screen.getByRole('button', { name: 'Target' })
    fireEvent.focus(trigger)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()

    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
