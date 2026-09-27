/**
 * Tool picker, shared by the Workbench's Run card and the Evals launcher
 * (the same way `ModelPicker` is): one checkbox group labelled "Tools".
 *
 * Two kinds of tools, stored in two `runConfigStore` fields:
 *
 * - Built-in toolsets (`GET /tools`) -> `toolset: string | null`. The server
 *   runs at most ONE built-in toolset, so these checkboxes behave like a
 *   radio group that can also be cleared: ticking one unticks the other.
 * - Saved MCP servers (`mcpServerStore`) -> `mcp_servers: string[]`. Any
 *   number up to `MAX_RUN_MCP_SERVERS`; past that the rest are disabled.
 *
 * A selected id whose server has since been deleted is not shown, is not
 * counted against the limit, and is pruned from the selection the next time
 * the MCP selection changes (and dropped from requests by `toRunRequest`).
 */

import { useEffect, useState } from 'react'
import { Alert } from '@readysetcloud/ui'
import { api } from '../api'
import type { McpServer, Toolset } from '../api'
import { MAX_RUN_MCP_SERVERS, useMcpServerStore, useRunConfigStore } from '../stores'
import { routeHash } from '../routing'

export interface ToolsetsState {
  toolsets: Toolset[]
  loaded: boolean
  error: string | null
}

/**
 * `GET /tools`, fetched once per mount and held locally: the list is static
 * for the life of the server.
 */
export function useToolsets(): ToolsetsState {
  const [state, setState] = useState<ToolsetsState>({ toolsets: [], loaded: false, error: null })

  useEffect(() => {
    let cancelled = false
    api
      .tools()
      .then(response => {
        if (!cancelled) setState({ toolsets: response.toolsets, loaded: true, error: null })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setState({
          toolsets: [],
          loaded: true,
          error: error instanceof Error ? error.message : 'Could not load toolsets'
        })
      })
    return () => {
      cancelled = true
    }
  }, [])

  return state
}

/**
 * The selected MCP ids that still exist. Before the list has loaded nothing
 * can be checked against, so the selection is taken as is.
 */
export function liveMcpSelection(
  selected: string[],
  servers: McpServer[],
  loaded: boolean
): string[] {
  if (!loaded) return selected
  return selected.filter(id => servers.some(server => server.id === id))
}

/**
 * One line naming every tool source a config uses — `"fraud_detection,
 * GitHub MCP"` — or `"none"`. An id with no matching server shows as the id.
 */
export function toolsSummary(
  toolset: string | null,
  mcpServerIds: string[],
  servers: McpServer[]
): string {
  const names = mcpServerIds.map(id => servers.find(server => server.id === id)?.name ?? id)
  const parts = [...(toolset ? [toolset] : []), ...names]
  return parts.length > 0 ? parts.join(', ') : 'none'
}

const CHECKBOX_CLASS = 'mt-0.5 h-4 w-4 shrink-0 rounded border-border text-primary-600'

export interface ToolPickerProps {
  /** Prefix for element ids and test ids, so two pickers never collide. */
  idPrefix?: string
}

export default function ToolPicker({ idPrefix = 'tools' }: ToolPickerProps) {
  const { toolsets, error: toolsError } = useToolsets()

  const toolset = useRunConfigStore(state => state.toolset)
  const selectedMcp = useRunConfigStore(state => state.mcp_servers)
  const setToolset = useRunConfigStore(state => state.setToolset)
  const setMcpServers = useRunConfigStore(state => state.setMcpServers)

  const servers = useMcpServerStore(state => state.servers)
  const serversLoaded = useMcpServerStore(state => state.loaded)
  const serversLoading = useMcpServerStore(state => state.loading)
  const serversError = useMcpServerStore(state => state.error)
  const loadServers = useMcpServerStore(state => state.loadServers)

  useEffect(() => {
    void loadServers()
  }, [loadServers])

  const live = liveMcpSelection(selectedMcp, servers, serversLoaded)
  const atLimit = live.length >= MAX_RUN_MCP_SERVERS

  function toggleMcp(id: string, checked: boolean) {
    setMcpServers(checked ? [...live, id] : live.filter(entry => entry !== id))
  }

  const manageHref = routeHash({ tab: 'tools' })

  return (
    <fieldset className="space-y-3" data-testid={`${idPrefix}-picker`}>
      <legend className="field-label">Tools</legend>

      <div className="space-y-1.5">
        <p className="text-xs font-medium text-muted-foreground">
          Built-in toolset <span className="font-normal">(one at a time)</span>
        </p>
        {toolsets.length === 0 && !toolsError && (
          <p className="text-xs text-muted-foreground">No built-in toolsets.</p>
        )}
        {toolsets.map(entry => {
          const id = `${idPrefix}-toolset-${entry.name}`
          return (
            <div key={entry.name} className="flex items-start gap-2">
              <input
                id={id}
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={toolset === entry.name}
                aria-describedby={`${id}-tools`}
                onChange={event => setToolset(event.target.checked ? entry.name : null)}
              />
              <div className="min-w-0">
                <label htmlFor={id} className="text-sm text-foreground">
                  {entry.name}
                </label>
                <p id={`${id}-tools`} className="text-xs text-muted-foreground">
                  {entry.tools.length > 0 ? entry.tools.join(', ') : 'No tools in this toolset'}
                </p>
              </div>
            </div>
          )
        })}
        {toolsError && <Alert variant="error">Could not load toolsets: {toolsError}</Alert>}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-xs font-medium text-muted-foreground">
            MCP servers{' '}
            <span className="font-normal">
              ({live.length}/{MAX_RUN_MCP_SERVERS})
            </span>
          </p>
          <a href={manageHref} className="text-xs text-primary-600 hover:underline">
            Manage tools
          </a>
        </div>

        {serversLoading && servers.length === 0 && (
          <p className="text-xs text-muted-foreground">Loading MCP servers…</p>
        )}

        {serversLoaded && servers.length === 0 && (
          <p className="text-xs text-muted-foreground" data-testid={`${idPrefix}-mcp-empty`}>
            No MCP servers saved yet.{' '}
            <a href={manageHref} className="text-primary-600 hover:underline">
              Add one on the Tools page
            </a>{' '}
            to let runs call its tools.
          </p>
        )}

        {servers.map(server => {
          const id = `${idPrefix}-mcp-${server.id}`
          const checked = live.includes(server.id)
          const disabled = !checked && atLimit
          return (
            <div key={server.id} className="flex items-start gap-2">
              <input
                id={id}
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={checked}
                disabled={disabled}
                title={disabled ? `At most ${MAX_RUN_MCP_SERVERS} MCP servers per run` : undefined}
                aria-describedby={`${id}-url`}
                onChange={event => toggleMcp(server.id, event.target.checked)}
              />
              <div className={`min-w-0 ${disabled ? 'opacity-50' : ''}`}>
                <label htmlFor={id} className="text-sm text-foreground">
                  {server.name}
                </label>
                <p id={`${id}-url`} className="truncate font-mono text-xs text-muted-foreground">
                  {server.url}
                </p>
              </div>
            </div>
          )
        })}

        {atLimit && (
          <p className="text-xs text-muted-foreground">
            A run can use at most {MAX_RUN_MCP_SERVERS} MCP servers.
          </p>
        )}

        {serversError && (
          <Alert variant="error">Could not load MCP servers: {serversError.message}</Alert>
        )}
      </div>
    </fieldset>
  )
}
