/**
 * The Tools tab: every tool a run can use.
 *
 * - Saved remote MCP servers (`mcpServerStore`): list, add, edit, delete and
 *   a connection test that lists the server's tools.
 * - The built-in toolsets (`GET /tools`), read-only.
 *
 * As on the Guardrails tab, one piece of local view state decides whether
 * the list or the editor is on screen.
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Loading
} from '@readysetcloud/ui'
import { useNotify } from '../../components/notify'
import { useToolsets } from '../../components/ToolPicker'
import { useMcpServerStore, useRunConfigStore } from '../../stores'
import type { McpServer, McpTestResult, Toolset } from '../../api'
import McpServerEditor from './McpServerEditor'

type View = { mode: 'list' } | { mode: 'editor'; server: McpServer | null }

function TestResult({ result }: { result: McpTestResult }) {
  if (!result.ok) {
    return (
      <Alert variant="error" data-testid="mcp-test-error">
        Could not connect: {result.error ?? 'unknown error'}
      </Alert>
    )
  }
  return (
    <Alert variant="success" data-testid="mcp-test-ok">
      <p>
        Connected — {result.tools.length} {result.tools.length === 1 ? 'tool' : 'tools'}.
      </p>
      {result.tools.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs">
          {result.tools.map(tool => (
            <li key={tool.name}>
              <span className="font-mono">{tool.name}</span>
              {tool.description && <span> — {tool.description}</span>}
            </li>
          ))}
        </ul>
      )}
    </Alert>
  )
}

function McpServerRow({ server, onEdit }: { server: McpServer; onEdit: () => void }) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const removeServer = useMcpServerStore(state => state.removeServer)
  const testServer = useMcpServerStore(state => state.testServer)
  const saving = useMcpServerStore(state => state.saving)
  const testing = useMcpServerStore(state => state.testing[server.id] ?? false)
  const testResult = useMcpServerStore(state => state.testResults[server.id])
  const notify = useNotify()

  async function handleDelete() {
    const removed = await removeServer(server.id)
    if (!removed) return
    setConfirmingDelete(false)
    // Don't leave the deleted id ticked in the Workbench's run config.
    const config = useRunConfigStore.getState()
    if (config.mcp_servers.includes(server.id)) {
      config.setMcpServers(config.mcp_servers.filter(id => id !== server.id))
    }
    notify(`Deleted ${server.name}`, { variant: 'success' })
  }

  return (
    <li
      className="space-y-2 border-b border-border py-3 last:border-0"
      data-testid={`mcp-server-${server.id}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-medium text-foreground">{server.name}</div>
          <div className="break-all font-mono text-xs text-muted-foreground">{server.url}</div>
          {server.header_names.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1" aria-label="Headers">
              {server.header_names.map(headerName => (
                <Badge key={headerName} variant="neutral" className="font-mono">
                  {headerName} ••••
                </Badge>
              ))}
            </div>
          )}
        </div>

        {confirmingDelete ? (
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Delete {server.name}?</span>
            <Button variant="error" size="sm" loading={saving} onClick={() => void handleDelete()}>
              Confirm
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={saving}
              onClick={() => setConfirmingDelete(false)}
            >
              Cancel
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              loading={testing}
              loadingLabel="Testing…"
              aria-label={`Test ${server.name}`}
              onClick={() => void testServer(server.id)}
            >
              Test
            </Button>
            <Button variant="ghost" size="sm" aria-label={`Edit ${server.name}`} onClick={onEdit}>
              Edit
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Delete ${server.name}`}
              onClick={() => setConfirmingDelete(true)}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      {testResult && !testing && <TestResult result={testResult} />}
    </li>
  )
}

function McpServerList({
  onNew,
  onEdit
}: {
  onNew: () => void
  onEdit: (server: McpServer) => void
}) {
  const servers = useMcpServerStore(state => state.servers)
  const loading = useMcpServerStore(state => state.loading)
  const loaded = useMcpServerStore(state => state.loaded)
  const error = useMcpServerStore(state => state.error)
  const loadServers = useMcpServerStore(state => state.loadServers)
  const saveError = useMcpServerStore(state => state.saveError)

  return (
    <Card role="region" aria-labelledby="mcp-servers-heading">
      <CardHeader className="flex items-center justify-between gap-3">
        <h2 id="mcp-servers-heading" className="card-title">
          MCP servers
        </h2>
        <Button onClick={onNew}>Add MCP server</Button>
      </CardHeader>
      <CardBody>
        <p className="mb-3 text-sm text-muted-foreground">
          Remote Model Context Protocol servers whose tools a run can call. Pick them per run in the
          Workbench or an evaluation, up to five at a time.
        </p>

        {error && (
          <ErrorState
            className="mb-3"
            heading="Could not load MCP servers"
            message={error.message}
            action={{ label: 'Retry', onClick: () => void loadServers(true) }}
          />
        )}

        {saveError && (
          <Alert variant="error" className="mb-3" data-testid="mcp-delete-error">
            {saveError.message}
          </Alert>
        )}

        {loading && servers.length === 0 && <Loading text="Loading MCP servers…" />}

        {loaded && !loading && servers.length === 0 && !error && (
          <div data-testid="mcp-servers-empty">
            <EmptyState
              title="No MCP servers yet"
              description="Add a server's URL (and any auth headers) to give runs its tools."
              action={<Button onClick={onNew}>Add MCP server</Button>}
            />
          </div>
        )}

        {servers.length > 0 && (
          <ul data-testid="mcp-servers-list">
            {servers.map(server => (
              <McpServerRow key={server.id} server={server} onEdit={() => onEdit(server)} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}

function BuiltInToolsets({
  toolsets,
  loaded,
  error
}: {
  toolsets: Toolset[]
  loaded: boolean
  error: string | null
}) {
  return (
    <Card role="region" aria-labelledby="builtin-toolsets-heading">
      <CardHeader>
        <h2 id="builtin-toolsets-heading" className="card-title">
          Built-in toolsets
        </h2>
      </CardHeader>
      <CardBody>
        <p className="mb-3 text-sm text-muted-foreground">
          Bundled with Nimbus. A run can use one built-in toolset alongside its MCP servers.
        </p>
        {!loaded && <Loading text="Loading toolsets…" />}
        {error && <Alert variant="error">Could not load toolsets: {error}</Alert>}
        {loaded && !error && toolsets.length === 0 && (
          <p className="text-sm text-muted-foreground">No built-in toolsets.</p>
        )}
        {toolsets.length > 0 && (
          <ul className="space-y-3" data-testid="builtin-toolsets">
            {toolsets.map(toolset => (
              <li key={toolset.name}>
                <div className="font-mono text-sm text-foreground">{toolset.name}</div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {toolset.tools.length > 0 ? (
                    toolset.tools.map(tool => (
                      <Badge key={tool} variant="primary" className="font-mono">
                        {tool}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">No tools in this toolset</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}

export default function ToolsPage() {
  const [view, setView] = useState<View>({ mode: 'list' })
  const loadServers = useMcpServerStore(state => state.loadServers)
  const clearSaveError = useMcpServerStore(state => state.clearSaveError)
  const toolsets = useToolsets()

  useEffect(() => {
    void loadServers()
  }, [loadServers])

  if (view.mode === 'editor') {
    return (
      <McpServerEditor
        server={view.server}
        onClose={() => {
          clearSaveError()
          setView({ mode: 'list' })
        }}
      />
    )
  }

  return (
    <div className="space-y-6" data-testid="tools-page">
      <McpServerList
        onNew={() => setView({ mode: 'editor', server: null })}
        onEdit={server => setView({ mode: 'editor', server })}
      />
      <BuiltInToolsets {...toolsets} />
    </div>
  )
}
