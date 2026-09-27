/**
 * The Tools tab's saved MCP servers: the list, and the CRUD + connection-test
 * wrappers around `api.mcpServers`.
 *
 * The list is also what the Workbench's tool picker offers, and what
 * `knownMcpServerIds` checks a run's `mcp_servers` against so a server
 * deleted since it was ticked is dropped instead of 400-ing the run.
 *
 * Header values are write-only on the server: nothing here ever holds one
 * after the request that set it.
 */

import { create } from 'zustand'
import { api } from '../api'
import type { McpServer, McpServerCreate, McpServerUpdate, McpTestResult } from '../api'
import { isAborted, toStoreError, type StoreError } from './errors'

export interface McpServerStateData {
  servers: McpServer[]
  loading: boolean
  loaded: boolean
  error: StoreError | null

  /** True while a create/update/delete is in flight. */
  saving: boolean
  saveError: StoreError | null

  /** Latest connection test per server id. */
  testResults: Record<string, McpTestResult>
  /** Server ids with a connection test in flight. */
  testing: Record<string, boolean>
}

export interface McpServerActions {
  /** `GET /mcp-servers`. Idempotent unless `force`. */
  loadServers(force?: boolean): Promise<void>
  /** `POST /mcp-servers` -> 201. Returns the new server, or `null` on failure. */
  createServer(body: McpServerCreate): Promise<McpServer | null>
  /** `PUT /mcp-servers/{id}` (`headers` is a patch). */
  updateServer(serverId: string, body: McpServerUpdate): Promise<McpServer | null>
  /** `DELETE /mcp-servers/{id}`. */
  removeServer(serverId: string): Promise<boolean>
  /**
   * `POST /mcp-servers/{id}/test`. A transport failure is folded into an
   * `ok: false` result, so the caller always gets something to show.
   */
  testServer(serverId: string): Promise<McpTestResult>
  clearSaveError(): void
  clear(): void
}

export type McpServerStore = McpServerStateData & McpServerActions

export const INITIAL_MCP_SERVER_STATE: McpServerStateData = {
  servers: [],
  loading: false,
  loaded: false,
  error: null,
  saving: false,
  saveError: null,
  testResults: {},
  testing: {}
}

/** Most saved MCP servers one run may use (server-side `max_length=5`). */
export const MAX_RUN_MCP_SERVERS = 5

let listRequest: Promise<void> | null = null

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record }
  delete next[key]
  return next
}

export const useMcpServerStore = create<McpServerStore>()((set, get) => ({
  ...INITIAL_MCP_SERVER_STATE,

  loadServers: (force = false) => {
    if (!force) {
      if (listRequest) return listRequest
      if (get().loaded) return Promise.resolve()
    }

    set({ loading: true, error: null })
    listRequest = (async () => {
      try {
        const response = await api.mcpServers.list()
        set({ servers: response.servers, loading: false, loaded: true })
      } catch (error) {
        if (isAborted(error)) {
          set({ loading: false })
          return
        }
        set({ loading: false, error: toStoreError(error) })
      } finally {
        listRequest = null
      }
    })()
    return listRequest
  },

  createServer: async body => {
    set({ saving: true, saveError: null })
    try {
      const server = await api.mcpServers.create(body)
      set(state => ({ saving: false, servers: [...state.servers, server] }))
      return server
    } catch (error) {
      set({ saving: false, saveError: toStoreError(error) })
      return null
    }
  },

  updateServer: async (serverId, body) => {
    set({ saving: true, saveError: null })
    try {
      const server = await api.mcpServers.update(serverId, body)
      set(state => ({
        saving: false,
        servers: state.servers.map(row => (row.id === serverId ? server : row)),
        // The URL or headers may have changed: an old test result is stale.
        testResults: without(state.testResults, serverId)
      }))
      return server
    } catch (error) {
      set({ saving: false, saveError: toStoreError(error) })
      return null
    }
  },

  removeServer: async serverId => {
    set({ saving: true, saveError: null })
    try {
      await api.mcpServers.remove(serverId)
    } catch (error) {
      set({ saving: false, saveError: toStoreError(error) })
      return false
    }
    set(state => ({
      saving: false,
      servers: state.servers.filter(row => row.id !== serverId),
      testResults: without(state.testResults, serverId)
    }))
    return true
  },

  testServer: async serverId => {
    set(state => ({ testing: { ...state.testing, [serverId]: true } }))
    let result: McpTestResult
    try {
      result = await api.mcpServers.test(serverId)
    } catch (error) {
      result = { ok: false, tools: [], error: toStoreError(error).message }
    }
    set(state => ({
      testing: without(state.testing, serverId),
      testResults: { ...state.testResults, [serverId]: result }
    }))
    return result
  },

  clearSaveError: () => set({ saveError: null }),

  clear: () => {
    listRequest = null
    set({ ...INITIAL_MCP_SERVER_STATE })
  }
}))

/**
 * The ids a run may reference, or `undefined` while the list has not loaded
 * (so a request built before then keeps whatever was selected rather than
 * dropping everything). Pass the result to `toRunRequest`.
 */
export function knownMcpServerIds(
  state: Pick<McpServerStateData, 'servers' | 'loaded'> = useMcpServerStore.getState()
): string[] | undefined {
  return state.loaded ? state.servers.map(server => server.id) : undefined
}
