import React, { type ErrorInfo, type ReactNode } from 'react'
import { Button, Card, CardBody, ErrorState } from '@readysetcloud/ui'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
  errorInfo: ErrorInfo | null
  errorId: string | null
}

class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
      errorId: null
    }
  }

  static getDerivedStateFromError(): Partial<ErrorBoundaryState> {
    // Update state so the next render will show the fallback UI
    return { hasError: true }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // Generate unique error ID for tracking
    const errorId = `error_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`

    // Log the error to console for debugging
    console.error('ErrorBoundary caught an error:', error, errorInfo)

    // Log additional context
    console.error('Error ID:', errorId)
    console.error('User Agent:', navigator.userAgent)
    console.error('URL:', window.location.href)
    console.error('Timestamp:', new Date().toISOString())

    this.setState({
      error: error,
      errorInfo: errorInfo,
      errorId: errorId
    })

    // Report error to external service if configured
    this.reportError(error, errorInfo, errorId)
  }

  reportError = (error: Error, errorInfo: ErrorInfo, errorId: string) => {
    // In a production environment, you would send this to an error reporting service
    // For now, we'll just log it locally
    try {
      const errorReport = {
        errorId,
        message: error.message,
        stack: error.stack,
        componentStack: errorInfo.componentStack,
        userAgent: navigator.userAgent,
        url: window.location.href,
        timestamp: new Date().toISOString(),
        props: this.props
      }

      // Store in localStorage for debugging
      const existingReports = JSON.parse(localStorage.getItem('error-reports') || '[]')
      existingReports.unshift(errorReport)
      // Keep only last 10 error reports
      localStorage.setItem('error-reports', JSON.stringify(existingReports.slice(0, 10)))
    } catch (reportingError) {
      console.error('Failed to report error:', reportingError)
    }
  }

  handleReload = () => {
    window.location.reload()
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null, errorInfo: null })
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="min-h-screen bg-background flex items-center justify-center p-4">
          <Card className="max-w-2xl w-full">
            <CardBody className="space-y-6">
              <ErrorState message="The application encountered an unexpected error and needs to be restarted." />

              {/* Error Details (in development) */}
              {import.meta.env.DEV && this.state.error && (
                <div className="p-4 bg-muted rounded-lg border border-border">
                  <h2 className="text-sm font-medium text-foreground mb-2">Error Details:</h2>
                  {this.state.errorId && (
                    <div className="mb-2 text-xs text-muted-foreground">
                      <span className="font-medium">Error ID:</span> {this.state.errorId}
                    </div>
                  )}
                  <div className="text-sm text-foreground font-mono bg-surface p-3 rounded border border-border overflow-auto max-h-32">
                    {this.state.error.toString()}
                  </div>
                  {this.state.errorInfo && (
                    <details className="mt-2">
                      <summary className="text-sm font-medium text-foreground cursor-pointer">
                        Component Stack
                      </summary>
                      <div className="mt-2 text-xs text-muted-foreground font-mono bg-surface p-3 rounded border border-border overflow-auto max-h-32">
                        {this.state.errorInfo.componentStack}
                      </div>
                    </details>
                  )}
                  <details className="mt-2">
                    <summary className="text-sm font-medium text-foreground cursor-pointer">
                      Browser Information
                    </summary>
                    <div className="mt-2 text-xs text-muted-foreground space-y-1">
                      <div>
                        <span className="font-medium">User Agent:</span> {navigator.userAgent}
                      </div>
                      <div>
                        <span className="font-medium">URL:</span> {window.location.href}
                      </div>
                      <div>
                        <span className="font-medium">Timestamp:</span> {new Date().toISOString()}
                      </div>
                    </div>
                  </details>
                </div>
              )}

              {/* Action Buttons */}
              <div className="flex flex-col sm:flex-row gap-3 justify-center">
                <Button onClick={this.handleReset}>Try Again</Button>
                <Button variant="secondary" onClick={this.handleReload}>
                  Reload Page
                </Button>
              </div>

              {/* Help Text */}
              <div className="text-center text-sm text-muted-foreground">
                <p>If this problem persists, try:</p>
                <ul className="mt-2 space-y-1">
                  <li>• Clearing your browser cache and cookies</li>
                  <li>• Checking that the Nimbus API server is reachable</li>
                  <li>• Ensuring you have a stable internet connection</li>
                  <li>• Refreshing the page</li>
                  <li>• Using a different browser</li>
                  <li>• Checking browser console for additional details</li>
                </ul>
                {this.state.errorId && (
                  <p className="mt-3 text-xs">
                    <span className="font-medium">Error ID:</span> {this.state.errorId}
                    <br />
                    <span>Include this ID when reporting the issue</span>
                  </p>
                )}
              </div>
            </CardBody>
          </Card>
        </main>
      )
    }

    return this.props.children
  }
}

export default ErrorBoundary
