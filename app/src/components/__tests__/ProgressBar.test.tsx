/** ProgressBar: width clamps to 0-100, indeterminate pulses, status shows the percentage. */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ProgressBar from '../ProgressBar'

function bar(container: HTMLElement): HTMLElement {
  return container.querySelector('.h-2.rounded-full.transition-all') as HTMLElement
}

describe('ProgressBar', () => {
  it('renders the progress as a clamped width with no status row by default', () => {
    const { container } = render(<ProgressBar progress={42.6} />)
    expect(bar(container).style.width).toBe('42.6%')
    expect(bar(container).className).toContain('bg-primary-600')
    expect(bar(container).className).not.toContain('animate-pulse')
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument()
  })

  it.each([
    [-20, '0%'],
    [0, '0%'],
    [150, '100%']
  ])('clamps %s to %s', (progress, width) => {
    const { container } = render(<ProgressBar progress={progress} />)
    expect(bar(container).style.width).toBe(width)
  })

  it('shows the status text with a rounded percentage', () => {
    render(<ProgressBar progress={66.4} status="Grading" />)
    expect(screen.getByText('Grading')).toBeInTheDocument()
    expect(screen.getByText('66%')).toBeInTheDocument()
  })

  it('exposes a progressbar named by its status with the rounded value', () => {
    render(<ProgressBar progress={66.4} status="Grading" />)
    const progressbar = screen.getByRole('progressbar', { name: 'Grading' })
    expect(progressbar).toHaveAttribute('aria-valuenow', '66')
  })

  it('indeterminate: full width, pulsing, and no percentage next to the status', () => {
    const { container } = render(<ProgressBar indeterminate status="Working" />)
    expect(bar(container).style.width).toBe('100%')
    expect(bar(container).className).toContain('animate-pulse')
    expect(screen.getByText('Working')).toBeInTheDocument()
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument()
  })

  it.each([
    ['success', 'bg-success-600', 'bg-success-100'],
    ['warning', 'bg-warning-600', 'bg-warning-100'],
    ['error', 'bg-error-600', 'bg-error-100']
  ] as const)('applies the %s colour to the bar and its track', (color, fill, track) => {
    const { container } = render(<ProgressBar progress={10} color={color} />)
    expect(bar(container).className).toContain(fill)
    expect(bar(container).parentElement?.className).toContain(track)
  })
})
