/**
 * The About tab: the six AI-agent-building principles and attribution.
 *
 * A straight TSX port of the old `components/AboutTab.jsx` — same copy, same
 * links, same landmarks. The page title/hero comes from AppShell's PageHero, so
 * this page renders only its content sections, built from the package's Card parts.
 */

import { Card, CardBody, CardDescription, CardHeader } from '@readysetcloud/ui'

interface Principle {
  id: number
  title: string
  description: string
}

const aiPrinciples: Principle[] = [
  {
    id: 1,
    title: 'System prompts are the key to success',
    description:
      'Make them clear, specific, and consistent. Treat the prompt as a contract, not a trick. Follow the RISEN framework (role, input, steps, expectation, narrowing).'
  },
  {
    id: 2,
    title: 'Minimize data context',
    description:
      "Provide just enough information for the LLM to decide if it needs tools. Don't overload with raw data that risks leakage, cost, or dilution."
  },
  {
    id: 3,
    title: 'Tools are APIs',
    description:
      'Tools must be deterministic, idempotent, and unambiguous. Define all properties strongly and design workflows that "do more."'
  },
  {
    id: 4,
    title: 'Validate and revise output',
    description:
      'Run generated content through policy, regulatory, and business rule checks. Regenerate if it fails.'
  },
  {
    id: 5,
    title: 'Use meta-agents',
    description:
      'Reinforce the primary LLM with specialized agents for error containment, fact-checking, and quality enforcement.'
  },
  {
    id: 6,
    title: 'Instrument everything',
    description:
      'Track tokens, prompt adherence, reasoning traces, and tool usage. Observability turns black-box behavior into accountable, auditable workflows.'
  }
]

const creators = [
  { name: 'Andres Moreno', linkedinUrl: 'https://www.linkedin.com/in/andmoredev/' },
  { name: 'Allen Helton', linkedinUrl: 'https://www.linkedin.com/in/allenheltondev/' }
]

const resources = {
  youtube: 'https://youtube.com/@nullchecktv',
  github: 'https://github.com/allenheltondev/llm-eval-harness'
}

export default function AboutPage() {
  return (
    <div className="space-y-6 sm:space-y-8 max-w-5xl mx-auto" data-testid="about-page">
      <section aria-labelledby="principles-heading">
        <Card>
          <CardHeader>
            <h2 id="principles-heading" className="card-title">
              6 Principles of AI Agent Building
            </h2>
            <CardDescription>
              These principles guide effective AI agent development and prompt engineering for
              reliable, scalable systems.
            </CardDescription>
          </CardHeader>
          <CardBody>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 sm:gap-4">
              {aiPrinciples.map(principle => (
                <article
                  key={principle.id}
                  className="flex items-start space-x-3 p-3 bg-muted rounded-lg"
                  aria-labelledby={`principle-${principle.id}-title`}
                >
                  <div
                    className="flex-shrink-0 w-6 h-6 bg-primary-100 text-primary-700 rounded-full flex items-center justify-center text-xs font-semibold"
                    aria-label={`Principle ${principle.id}`}
                    role="img"
                  >
                    {principle.id}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h3
                      id={`principle-${principle.id}-title`}
                      className="font-semibold text-foreground mb-1 text-sm leading-tight"
                    >
                      {principle.title}
                    </h3>
                    <p className="text-xs text-muted-foreground leading-snug">
                      {principle.description}
                    </p>
                  </div>
                </article>
              ))}
            </div>
          </CardBody>
        </Card>
      </section>

      <section aria-labelledby="credits-heading">
        <h2 id="credits-heading" className="sr-only">
          Credits
        </h2>
        <Card>
          <CardBody className="text-sm text-muted-foreground w-full flex flex-col md:flex-row md:items-center md:justify-between gap-2">
            <p>
              Created by{' '}
              <a
                href={creators[0].linkedinUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary-600 hover:text-primary-700 font-medium"
                aria-label={`Visit ${creators[0].name}'s LinkedIn profile (opens in new tab)`}
              >
                {creators[0].name}
              </a>{' '}
              and{' '}
              <a
                href={creators[1].linkedinUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary-600 hover:text-primary-700 font-medium"
                aria-label={`Visit ${creators[1].name}'s LinkedIn profile (opens in new tab)`}
              >
                {creators[1].name}
              </a>
            </p>
            <p>
              Learn more:{' '}
              <a
                href={resources.youtube}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary-600 hover:text-primary-700 font-medium"
                aria-label="Visit Null Check TV YouTube Channel (opens in new tab)"
              >
                YouTube
              </a>{' '}
              | Source:{' '}
              <a
                href={resources.github}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary-600 hover:text-primary-700 font-medium"
                aria-label="Visit GitHub Repository (opens in new tab)"
              >
                GitHub
              </a>
            </p>
          </CardBody>
        </Card>
      </section>
    </div>
  )
}
