/** mcpServerStore: list caching, CRUD, connection tests, `knownMcpServerIds`. */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { McpServer } from '../../api'

const listMock = vi.fn()
const createMock = vi.fn()
const updateMock = vi.fn()
const removeMock = vi.fn()
const testMock = vi.fn()

vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      mcpServers: {
        ...actual.api.mcpServers,
        list: listMock,
        create: createMock,
        update: updateMock,
        remove: removeMock,
        test: testMock
      }
    }
  }
})

const { ApiError, StreamAbortedError } = await import('../../api')
const { useMcpServerStore, knownMcpServerIds, INITIAL_MCP_SERVER_STATE } =
  await import('../mcpServerStore')

function server(id: string, overrides: Partial<McpServer> = {}): McpServer {
  return {
    id,
    name: `Server ${id}`,
    url: `https://${id}.example.com/mcp`,
    header_names: [],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides
  }
}

beforeEach(() => {
  for (const mock of [listMock, createMock, updateMock, removeMock, testMock]) mock.mockReset()
  useMcpServerStore.getState().clear()
})

describe('loadServers', () => {
  it('loads once, then serves the cached list unless forced', async () => {
    listMock.mockResolvedValue({ servers: [server('a')] })

    await useMcpServerStore.getState().loadServers()
    await useMcpServerStore.getState().loadServers()
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(useMcpServerStore.getState()).toMatchObject({
      servers: [server('a')],
      loaded: true,
      loading: false,
      error: null
    })

    await useMcpServerStore.getState().loadServers(true)
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('shares one in-flight request between concurrent callers', async () => {
    listMock.mockResolvedValue({ servers: [] })

    const first = useMcpServerStore.getState().loadServers()
    const second = useMcpServerStore.getState().loadServers()
    expect(second).toBe(first)
    expect(useMcpServerStore.getState().loading).toBe(true)
    await first

    expect(listMock).toHaveBeenCalledTimes(1)
  })

  it('records a failure as a StoreError', async () => {
    listMock.mockRejectedValue(new ApiError('nope', { code: 'upstream_error', status: 502 }))

    await useMcpServerStore.getState().loadServers()

    expect(useMcpServerStore.getState()).toMatchObject({
      loading: false,
      loaded: false,
      error: { code: 'upstream_error', message: 'nope' }
    })
  })

  it('treats an abort as no error', async () => {
    listMock.mockRejectedValue(new StreamAbortedError())

    await useMcpServerStore.getState().loadServers()

    expect(useMcpServerStore.getState()).toMatchObject({ loading: false, error: null })
  })
})

describe('create / update / remove', () => {
  it('createServer appends the new server', async () => {
    useMcpServerStore.setState({ servers: [server('a')], loaded: true })
    createMock.mockResolvedValue(server('b', { header_names: ['Authorization'] }))

    const created = await useMcpServerStore.getState().createServer({
      name: 'Server b',
      url: 'https://b.example.com/mcp',
      headers: { Authorization: 'Bearer secret' }
    })

    expect(created?.id).toBe('b')
    expect(createMock).toHaveBeenCalledWith({
      name: 'Server b',
      url: 'https://b.example.com/mcp',
      headers: { Authorization: 'Bearer secret' }
    })
    expect(useMcpServerStore.getState().servers.map(row => row.id)).toEqual(['a', 'b'])
    expect(useMcpServerStore.getState().saving).toBe(false)
  })

  it('createServer records a save error and returns null', async () => {
    createMock.mockRejectedValue(
      new ApiError('MCP server URLs must use https', { code: 'invalid_mcp_url', status: 400 })
    )

    const created = await useMcpServerStore.getState().createServer({ name: 'x', url: 'http://x' })

    expect(created).toBeNull()
    expect(useMcpServerStore.getState().saveError).toEqual({
      code: 'invalid_mcp_url',
      message: 'MCP server URLs must use https'
    })

    useMcpServerStore.getState().clearSaveError()
    expect(useMcpServerStore.getState().saveError).toBeNull()
  })

  it('updateServer replaces the row and drops its stale test result', async () => {
    useMcpServerStore.setState({
      servers: [server('a'), server('b')],
      testResults: {
        a: { ok: true, tools: [], error: null },
        b: { ok: true, tools: [], error: null }
      }
    })
    updateMock.mockResolvedValue(server('a', { name: 'Renamed' }))

    const updated = await useMcpServerStore
      .getState()
      .updateServer('a', { name: 'Renamed', headers: { Authorization: null } })

    expect(updated?.name).toBe('Renamed')
    expect(updateMock).toHaveBeenCalledWith('a', {
      name: 'Renamed',
      headers: { Authorization: null }
    })
    const state = useMcpServerStore.getState()
    expect(state.servers.map(row => row.name)).toEqual(['Renamed', 'Server b'])
    expect(Object.keys(state.testResults)).toEqual(['b'])
  })

  it('updateServer records a failure', async () => {
    updateMock.mockRejectedValue(new Error('boom'))

    expect(await useMcpServerStore.getState().updateServer('a', { name: 'x' })).toBeNull()
    expect(useMcpServerStore.getState().saveError).toEqual({
      code: 'unknown_error',
      message: 'boom'
    })
  })

  it('removeServer drops the row', async () => {
    useMcpServerStore.setState({ servers: [server('a'), server('b')] })
    removeMock.mockResolvedValue(undefined)

    expect(await useMcpServerStore.getState().removeServer('a')).toBe(true)
    expect(removeMock).toHaveBeenCalledWith('a')
    expect(useMcpServerStore.getState().servers.map(row => row.id)).toEqual(['b'])
  })

  it('removeServer keeps the row on failure', async () => {
    useMcpServerStore.setState({ servers: [server('a')] })
    removeMock.mockRejectedValue(new ApiError('gone', { code: 'not_found', status: 404 }))

    expect(await useMcpServerStore.getState().removeServer('a')).toBe(false)
    expect(useMcpServerStore.getState().servers).toHaveLength(1)
    expect(useMcpServerStore.getState().saveError?.code).toBe('not_found')
  })
})

describe('testServer', () => {
  it('stores the result and clears the in-flight flag', async () => {
    let resolve!: (value: unknown) => void
    testMock.mockReturnValue(new Promise(r => (resolve = r)))

    const pending = useMcpServerStore.getState().testServer('a')
    expect(useMcpServerStore.getState().testing).toEqual({ a: true })

    resolve({ ok: true, tools: [{ name: 'search', description: 'Find' }], error: null })
    const result = await pending

    expect(result.ok).toBe(true)
    expect(useMcpServerStore.getState().testing).toEqual({})
    expect(useMcpServerStore.getState().testResults.a).toEqual(result)
  })

  it('folds a transport failure into an ok:false result', async () => {
    testMock.mockRejectedValue(new ApiError('MCP server not found', { code: 'unknown_mcp_server' }))

    const result = await useMcpServerStore.getState().testServer('a')

    expect(result).toEqual({ ok: false, tools: [], error: 'MCP server not found' })
    expect(useMcpServerStore.getState().testResults.a).toEqual(result)
  })
})

describe('knownMcpServerIds', () => {
  it('is undefined until the list has loaded', () => {
    expect(knownMcpServerIds()).toBeUndefined()
    expect(knownMcpServerIds({ ...INITIAL_MCP_SERVER_STATE, servers: [server('a')] })).toBe(
      undefined
    )
  })

  it('lists the loaded ids', () => {
    useMcpServerStore.setState({ servers: [server('a'), server('b')], loaded: true })
    expect(knownMcpServerIds()).toEqual(['a', 'b'])
  })
})
