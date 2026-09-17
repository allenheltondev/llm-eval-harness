import React from 'react'
import ReactDOM from 'react-dom/client'
import AppShell from './AppShell'
import { AuthGate } from './auth'
import ErrorBoundary from './components/ErrorBoundary'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <AuthGate>
        <AppShell />
      </AuthGate>
    </ErrorBoundary>
  </React.StrictMode>
)
