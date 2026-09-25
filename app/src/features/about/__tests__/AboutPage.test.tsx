/**
 * AboutPage: renders the principles and credits sections (no page title of
 * its own — AppShell's PageHero supplies the h1) with external links that
 * open in a new tab.
 */

import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import AboutPage from '../AboutPage'

describe('AboutPage', () => {
  it('renders the principles section with all six principles and no h1', () => {
    render(<AboutPage />)

    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    const principles = screen.getByRole('region', { name: '6 Principles of AI Agent Building' })
    expect(within(principles).getAllByRole('article')).toHaveLength(6)
    expect(within(principles).getByRole('article', { name: 'Tools are APIs' })).toBeInTheDocument()
  })

  it('renders credits with external links that open in a new tab', () => {
    render(<AboutPage />)

    const credits = screen.getByRole('region', { name: 'Credits' })
    const links = within(credits).getAllByRole('link')
    expect(links).toHaveLength(4)
    for (const link of links) {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
    expect(
      within(credits).getByRole('link', { name: 'Visit GitHub Repository (opens in new tab)' })
    ).toHaveAttribute('href', 'https://github.com/allenheltondev/llm-eval-harness')
  })
})
