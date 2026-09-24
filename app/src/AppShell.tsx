/**
 * The application shell: a sticky header (title, tab bar) over one page per
 * tab.
 *
 * The active tab (and, on Evals and History, the open evaluation or run) is
 * the URL fragment — see `routing.ts` — so a link the CLI prints, or one
 * copied from the address bar, opens the same thing after a reload. Every tab
 * rebuilds itself from its store on mount, so no other state needs to live
 * in the URL.
 */

import { useAuth } from './auth/react'
import AboutPage from './features/about/AboutPage'
import EvalsPage from './features/evals/EvalsPage'
import GuardrailsPage from './features/guardrails/GuardrailsPage'
import HistoryPage from './features/history/HistoryPage'
import WorkbenchPage from './features/workbench/WorkbenchPage'
import { useHashRoute, type Route, type TabId } from './routing'

export type { TabId } from './routing'

interface TabDef {
  id: TabId
  label: string
}

export const TABS: TabDef[] = [
  { id: 'workbench', label: 'Workbench' },
  { id: 'evals', label: 'Evals' },
  { id: 'history', label: 'History' },
  { id: 'guardrails', label: 'Guardrails' },
  { id: 'about', label: 'About' }
]

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
  const activeTab = route.tab
  // Outside an AuthProvider (every local run) `required` is false and no
  // sign-out control renders; behind AuthGate it is the signed-in user's.
  const { required: authRequired, signedIn, user, signOut } = useAuth()

  return (
    <div className="min-h-screen bg-gradient-to-br from-primary-50 to-secondary-100">
      <header className="sticky top-0 z-40 border-b border-secondary-200 bg-gradient-to-br from-primary-50 to-secondary-100 shadow-sm">
        <div className="container mx-auto px-4 sm:px-6 lg:px-8 py-3">
          <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
            <div className="text-center sm:text-left">
              <h1 className="text-xl md:text-2xl font-bold text-primary-700 leading-tight">
                Nimbus
              </h1>
              <p className="text-xs md:text-sm text-secondary-700">
                Building enterprise-grade AI agents before it was cool
              </p>
            </div>

            <div className="flex items-center gap-2">
              <nav
                role="tablist"
                aria-label="Sections"
                className="flex flex-wrap justify-center rounded-lg border border-gray-200 bg-surface p-1 shadow-sm"
              >
                {TABS.map(tab => {
                  const selected = tab.id === activeTab
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      role="tab"
                      id={`tab-${tab.id}`}
                      aria-selected={selected}
                      aria-controls={`tabpanel-${tab.id}`}
                      className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors duration-200 ${
                        selected
                          ? 'bg-primary-600 text-white'
                          : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'
                      }`}
                      onClick={() => navigate({ tab: tab.id })}
                    >
                      {tab.label}
                    </button>
                  )
                })}
              </nav>

              {authRequired && signedIn && (
                <button
                  type="button"
                  onClick={() => void signOut()}
                  title={typeof user.email === 'string' ? `Signed in as ${user.email}` : 'Sign out'}
                  className="rounded-md border border-gray-200 bg-surface px-3 py-1.5 text-sm font-medium text-gray-600 shadow-sm hover:bg-gray-50 hover:text-gray-900"
                >
                  Sign out
                </button>
              )}
            </div>
          </div>
        </div>
      </header>

      <main
        role="tabpanel"
        id={`tabpanel-${activeTab}`}
        aria-labelledby={`tab-${activeTab}`}
        className="container mx-auto px-4 sm:px-6 lg:px-8 py-6"
      >
        <TabPage route={route} navigate={navigate} />
      </main>
    </div>
  )
}
