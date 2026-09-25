/**
 * The application shell: the Ready, Set, Cloud `AppNav` as a side rail
 * beside one page per section.
 *
 * The active section (and, on Evals and History, the open evaluation or run)
 * is the URL fragment — see `routing.ts` — so a link the CLI prints, or one
 * copied from the address bar, opens the same thing after a reload. The rail's
 * links are plain `#/...` anchors: a hash change is not a page load, so no
 * router link component is needed. Every page rebuilds itself from its store
 * on mount, so no other state needs to live in the URL.
 */

import { useCallback, useState, type MouseEvent, type ReactNode } from 'react'
import {
  AppNav,
  Container,
  PageHero,
  PageHeroSubtitle,
  PageHeroTitle,
  readySetCloudServices,
  type AppNavItem,
  type AppTheme
} from '@readysetcloud/ui'
import { displayName, useSession } from './auth'
import AboutPage from './features/about/AboutPage'
import EvalsPage from './features/evals/EvalsPage'
import GuardrailsPage from './features/guardrails/GuardrailsPage'
import HistoryPage from './features/history/HistoryPage'
import WorkbenchPage from './features/workbench/WorkbenchPage'
import { parseRoute, routeHash, useHashRoute, type Route, type TabId } from './routing'

export type { TabId } from './routing'

/**
 * 20px outline icons, stroked in `currentColor` so the rail's states color
 * them. The inline `fill: none` matters: the design system fills nav icons
 * (`.app-nav-link-icon svg { fill: currentColor }`), which a `fill` attribute
 * cannot override, and a filled outline icon is a solid blob.
 */
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      style={{ fill: 'none' }}
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

interface TabDef {
  id: TabId
  label: string
  /** The rail heading this section sits under; none for the standalone one. */
  section?: string
  /** One line under the page title. */
  description: string
  icon: ReactNode
}

export const TABS: TabDef[] = [
  {
    id: 'workbench',
    label: 'Workbench',
    section: 'Run',
    description:
      'Run a prompt against a model and watch the output, tool calls and metrics arrive.',
    icon: (
      <Icon>
        <path d="M4 17l6-6-6-6" />
        <path d="M12 19h8" />
      </Icon>
    )
  },
  {
    id: 'evals',
    label: 'Evals',
    section: 'Run',
    description: 'Launch evaluations and see how every case scored.',
    icon: (
      <Icon>
        <path d="M9 11l3 3 8-8" />
        <path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9" />
      </Icon>
    )
  },
  {
    id: 'history',
    label: 'History',
    section: 'Review',
    description:
      'Every run, filterable, with its full detail, a side-by-side compare and an export.',
    icon: (
      <Icon>
        <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
        <path d="M3 3v5h5" />
        <path d="M12 7v5l3 3" />
      </Icon>
    )
  },
  {
    id: 'guardrails',
    label: 'Guardrails',
    section: 'Manage',
    description: 'Create and version Bedrock guardrails, and see what they catch.',
    icon: (
      <Icon>
        <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" />
      </Icon>
    )
  },
  {
    id: 'about',
    label: 'About',
    description: 'What Nimbus is for, and the principles it is built on.',
    icon: (
      <Icon>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 16v-4" />
        <path d="M12 8h.01" />
      </Icon>
    )
  }
]

/** This app's id in the Ready, Set, Cloud service registry (not listed there yet). */
export const SERVICE_ID = 'nimbus'

const THEME_KEY = 'nimbus.theme'

function storedTheme(): AppTheme {
  try {
    const value = localStorage.getItem(THEME_KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

function TabPage({ route, navigate }: { route: Route; navigate: (next: Route) => void }) {
  switch (route.tab) {
    case 'workbench':
      return <WorkbenchPage />
    case 'evals':
      return (
        <EvalsPage
          evaluationId={route.evaluationId ?? null}
          onSelectEvaluation={id => navigate({ tab: 'evals', evaluationId: id ?? undefined })}
        />
      )
    case 'history':
      return (
        <HistoryPage
          runId={route.runId ?? null}
          onSelectRun={id => navigate({ tab: 'history', runId: id ?? undefined })}
        />
      )
    case 'guardrails':
      return <GuardrailsPage />
    case 'about':
      return <AboutPage />
  }
}

export default function AppShell() {
  const [route, navigate] = useHashRoute()
  // Local runs have no sign-in at all; behind AuthGate this is the signed-in user.
  const { required: authRequired, signedIn, user, signOut } = useSession()
  const [theme, setTheme] = useState<AppTheme>(storedTheme)

  const changeTheme = useCallback((next: AppTheme) => {
    setTheme(next)
    try {
      localStorage.setItem(THEME_KEY, next)
    } catch {
      // Private mode: the choice lasts for this page only.
    }
  }, [])

  const navItems: AppNavItem[] = TABS.map(tab => ({
    id: tab.id,
    label: tab.label,
    href: routeHash({ tab: tab.id }),
    active: tab.id === route.tab,
    icon: tab.icon,
    section: tab.section
  }))

  const activeTab = TABS.find(tab => tab.id === route.tab) ?? TABS[0]
  const authState = !authRequired ? 'none' : signedIn ? 'authenticated' : 'anonymous'

  // AppNav keeps its mobile menu's open state to itself and does not close it
  // when a link is followed, so on a narrow screen the menu would stay open
  // over the page just chosen. Remounting it on every route change closes it.
  // A tap on the link for the page already shown changes no route (nor, from
  // a bare `/`, does Workbench's); that bumps `navEpoch` instead. Only then:
  // remounting on every click would detach the link before the browser
  // follows it.
  const [navEpoch, setNavEpoch] = useState(0)
  const currentHash = routeHash(route)
  const closeMenuOnSamePage = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      const href = (event.target as Element).closest('a[href^="#"]')?.getAttribute('href')
      if (href && routeHash(parseRoute(href)) === currentHash) setNavEpoch(n => n + 1)
    },
    [currentHash]
  )

  return (
    <div className="min-h-screen bg-background text-foreground min-[641px]:flex">
      <div
        className="min-[641px]:sticky min-[641px]:top-0 min-[641px]:h-screen"
        onClick={closeMenuOnSamePage}
      >
        <AppNav
          key={`${currentHash}:${navEpoch}`}
          appName="Nimbus"
          layout="side"
          homeHref={routeHash({ tab: 'workbench' })}
          navItems={navItems}
          services={readySetCloudServices}
          currentServiceId={SERVICE_ID}
          authState={authState}
          user={
            authState === 'authenticated'
              ? {
                  name: displayName(user),
                  email: typeof user.email === 'string' ? user.email : undefined
                }
              : undefined
          }
          onSignOut={() => void signOut()}
          theme={theme}
          onThemeChange={changeTheme}
        />
      </div>

      <main
        id={`section-${route.tab}`}
        aria-label={activeTab.label}
        className="min-w-0 flex-1 py-6"
      >
        <Container className="space-y-6">
          <PageHero>
            <PageHeroTitle>{activeTab.label}</PageHeroTitle>
            <PageHeroSubtitle>{activeTab.description}</PageHeroSubtitle>
          </PageHero>
          <TabPage route={route} navigate={navigate} />
        </Container>
      </main>
    </div>
  )
}
