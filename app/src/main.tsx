import React from 'react'
import ReactDOM from 'react-dom/client'
import { ToastProvider } from '@readysetcloud/ui'
import AppShell from './AppShell'
import { AuthGate } from './auth'
import ErrorBoundary from './components/ErrorBoundary'
import '@readysetcloud/ui/styles.css'
import '@readysetcloud/ui/fonts.css'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <ToastProvider>
        <AuthGate>
          <AppShell />
        </AuthGate>
      </ToastProvider>
    </ErrorBoundary>
  </React.StrictMode>
)
