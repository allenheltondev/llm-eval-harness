/** EffectsPanel and WarningsList: what the numbers cannot tell you, said plainly. */

import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type { ComparisonEffects, ComparisonWarning } from '../../../api'
import { EffectsPanel, WarningsList } from '../ComparisonInsights'

const effects: ComparisonEffects = {
  axes: {
    model: [
      { value: 'nova', mean_pass_rate: 0.65, arms: 2 },
      { value: 'sonnet', mean_pass_rate: 0.75, arms: 2 }
    ],
    prompt: [
      { value: 'terse', mean_pass_rate: 0.5, arms: 2 },
      { value: 'full', mean_pass_rate: 0.9, arms: 2 }
    ]
  },
  spread: { model: 0.1, prompt: 0.4 },
  dominant: 'prompt'
}

describe('EffectsPanel', () => {
  it('says which axis matters more, and the other one', () => {
    render(<EffectsPanel effects={effects} />)

    expect(screen.getByTestId('effects-conclusion')).toHaveTextContent(
      'The prompt moves the pass rate more than the model does.'
    )
  })

  it('names the other axis correctly when the model dominates', () => {
    render(<EffectsPanel effects={{ ...effects, dominant: 'model' }} />)

    expect(screen.getByTestId('effects-conclusion')).toHaveTextContent(
      'The model moves the pass rate more than the prompt does.'
    )
  })

  it('says neither matters more when neither clearly does', () => {
    render(<EffectsPanel effects={{ ...effects, dominant: null }} />)

    expect(screen.getByTestId('effects-conclusion')).toHaveTextContent(
      'Neither axis clearly moves the pass rate more than the other.'
    )
  })

  it('shows each value with its mean pass rate, and each axis with its spread', () => {
    render(<EffectsPanel effects={effects} />)

    const prompt = screen.getByTestId('effect-prompt')
    expect(prompt).toHaveTextContent('spread +40 pts')
    expect(within(prompt).getByText('full').parentElement).toHaveTextContent('90%')
    expect(within(prompt).getByText('terse').parentElement).toHaveTextContent('50%')
    expect(screen.getByTestId('effect-model')).toHaveTextContent('spread +10 pts')
  })

  it('sizes each bar to its mean pass rate', () => {
    const { container } = render(<EffectsPanel effects={effects} />)

    const widths = Array.from(container.querySelectorAll<HTMLElement>('[style*="width"]')).map(
      bar => bar.style.width
    )
    expect(widths).toEqual(['65%', '75%', '50%', '90%'])
  })

  it('warns that interactions are not separated out', () => {
    render(<EffectsPanel effects={effects} />)

    expect(screen.getByTestId('effects-panel')).toHaveTextContent(
      'It does not separate a prompt that helps only one model.'
    )
  })

  it('copes with a single axis', () => {
    render(
      <EffectsPanel
        effects={{
          axes: { model: effects.axes.model },
          spread: { model: 0.1 },
          dominant: 'model'
        }}
      />
    )

    expect(screen.getByTestId('effects-conclusion')).toHaveTextContent(
      'The model moves the pass rate more than the other axis does.'
    )
  })
})

describe('WarningsList', () => {
  const warnings: ComparisonWarning[] = [
    { code: 'single_repeat', message: 'Each case ran once.', arms: ['primary', 'fallback'] },
    { code: 'small_suite', message: 'The suite has fewer than 20 cases.', arms: [] }
  ]

  it('lists every warning with the arms it concerns', () => {
    render(<WarningsList warnings={warnings} />)

    expect(screen.getByTestId('warning-single_repeat')).toHaveTextContent(
      'Each case ran once. (primary, fallback)'
    )
    expect(screen.getByTestId('warning-small_suite')).toHaveTextContent(
      'The suite has fewer than 20 cases.'
    )
  })

  it('names no arms for a warning about the whole comparison', () => {
    render(<WarningsList warnings={warnings} />)

    expect(screen.getByTestId('warning-small_suite')).not.toHaveTextContent('(')
  })

  it('renders nothing when there is nothing to warn about', () => {
    const { container } = render(<WarningsList warnings={[]} />)

    expect(container).toBeEmptyDOMElement()
  })
})
