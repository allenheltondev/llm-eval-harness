/**
 * ToolPicker against the real `runConfigStore` with `GET /tools` and the
 * MCP server list stubbed: built-in toolsets are a single choice, MCP
 * servers a multi-select capped at five, and a deleted server's id neither
 * shows nor counts.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { McpServer, ToolsResponse } from '../../api'

const toolsMock = vi.fn<() => Promise<ToolsResponse>>()

vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, api: { ...actual.api, tools: toolsMock } }
})

const { default: ToolPicker, liveMcpSelection, toolsSummary } = await import('../ToolPicker')
const {
  DEFAULT_RUN_CONFIG,
  INITIAL_MCP_SERVER_STATE,
  MAX_RUN_MCP_SERVERS,
  knownMcpServerIds,
  toRunRequest,
  useMcpServerStore,
  useRunConfigStore
} = await import('../../stores')

function server(id: string, name = `Server ${id}`): McpServer {
  return {
    id,
    name,
    url: `https://${id}.example.com/mcp`,
    header_names: [],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z'
  }
}

const SIX_SERVERS = ['a', 'b', 'c', 'd', 'e', 'f'].map(id => server(id))
const loadServers = vi.fn().mockResolvedValue(undefined)

async function renderSettled() {
  const result = render(<ToolPicker />)
  await screen.findByRole('checkbox', { name: 'fraud_detection' })
  return result
}

beforeEach(() => {
  toolsMock.mockReset()
  toolsMock.mockResolvedValue({
    toolsets: [
      { name: 'fraud_detection', tools: ['lookupAccount', 'flagTransaction'] },
      { name: 'weather', tools: [] }
    ]
  })
  loadServers.mockClear()
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
  useMcpServerStore.setState({
    ...INITIAL_MCP_SERVER_STATE,
    servers: SIX_SERVERS,
    loaded: true,
    loadServers
  })
})

describe('ToolPicker built-in toolsets', () => {
  it('is one "Tools" group listing each toolset with its tools as a description', async () => {
    await renderSettled()

    expect(screen.getByRole('group', { name: 'Tools' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'fraud_detection' })).toHaveAccessibleDescription(
      'lookupAccount, flagTransaction'
    )
    expect(screen.getByRole('checkbox', { name: 'weather' })).toHaveAccessibleDescription(
      'No tools in this toolset'
    )
    expect(loadServers).toHaveBeenCalledTimes(1)
  })

  it('is a single choice: ticking one unticks the other, and unticking clears it', async () => {
    await renderSettled()
    const fraud = screen.getByRole('checkbox', { name: 'fraud_detection' })
    const weather = screen.getByRole('checkbox', { name: 'weather' })

    fireEvent.click(fraud)
    expect(useRunConfigStore.getState().toolset).toBe('fraud_detection')
    expect(fraud).toBeChecked()

    fireEvent.click(weather)
    expect(useRunConfigStore.getState().toolset).toBe('weather')
    expect(weather).toBeChecked()
    expect(fraud).not.toBeChecked()

    fireEvent.click(weather)
    expect(useRunConfigStore.getState().toolset).toBeNull()
    expect(weather).not.toBeChecked()
  })

  it('says so when the server has no built-in toolsets', async () => {
    toolsMock.mockResolvedValue({ toolsets: [] })
    render(<ToolPicker />)

    expect(await screen.findByText('No built-in toolsets.')).toBeInTheDocument()
  })

  it('reports a GET /tools failure, with a generic message for a non-Error', async () => {
    toolsMock.mockRejectedValue('boom')
    render(<ToolPicker />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load toolsets: Could not load toolsets'
    )
  })

  it('ignores a toolset fetch that settles after unmount', async () => {
    let resolveTools!: (value: ToolsResponse) => void
    let rejectTools!: (reason: unknown) => void
    toolsMock
      .mockImplementationOnce(() => new Promise(resolve => (resolveTools = resolve)))
      .mockImplementationOnce(() => new Promise((_, reject) => (rejectTools = reject)))

    render(<ToolPicker />).unmount()
    render(<ToolPicker />).unmount()
    await act(async () => {
      resolveTools({ toolsets: [{ name: 'late', tools: [] }] })
      rejectTools(new Error('late failure'))
    })

    expect(screen.queryByRole('checkbox', { name: 'late' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('ToolPicker MCP servers', () => {
  it('lists every saved server with its URL and ticks several', async () => {
    await renderSettled()

    expect(screen.getByRole('checkbox', { name: 'Server a' })).toHaveAccessibleDescription(
      'https://a.example.com/mcp'
    )
    fireEvent.click(screen.getByRole('checkbox', { name: 'Server a' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Server c' }))
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['a', 'c'])
    expect(screen.getByText('(2/5)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Server a' }))
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['c'])
  })

  it('works alongside a built-in toolset', async () => {
    await renderSettled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'fraud_detection' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Server b' }))

    const request = toRunRequest(useRunConfigStore.getState(), knownMcpServerIds())
    expect(request.toolset).toBe('fraud_detection')
    expect(request.mcp_servers).toEqual(['b'])
  })

  it(`disables the rest once ${MAX_RUN_MCP_SERVERS} are ticked, and frees them when one is unticked`, async () => {
    await renderSettled()

    for (const id of ['a', 'b', 'c', 'd', 'e']) {
      fireEvent.click(screen.getByRole('checkbox', { name: `Server ${id}` }))
    }

    const sixth = screen.getByRole('checkbox', { name: 'Server f' })
    expect(sixth).toBeDisabled()
    expect(sixth).toHaveAttribute('title', 'At most 5 MCP servers per run')
    expect(screen.getByText('A run can use at most 5 MCP servers.')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Server a' })).toBeEnabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Server a' }))
    expect(sixth).toBeEnabled()
    fireEvent.click(sixth)
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['b', 'c', 'd', 'e', 'f'])
  })

  it('does not show or count a selected id whose server was deleted, and prunes it on change', async () => {
    useRunConfigStore.setState({ mcp_servers: ['gone-1', 'gone-2', 'gone-3', 'gone-4', 'a'] })
    await renderSettled()

    expect(screen.getByText('(1/5)')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Server a' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Server f' })).toBeEnabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Server b' }))
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['a', 'b'])
  })

  it('has an empty state pointing at the Tools page when none are saved', async () => {
    useMcpServerStore.setState({ servers: [] })
    await renderSettled()

    const empty = screen.getByTestId('tools-mcp-empty')
    expect(empty).toHaveTextContent('No MCP servers saved yet.')
    expect(within(empty).getByRole('link', { name: 'Add one on the Tools page' })).toHaveAttribute(
      'href',
      '#/tools'
    )
    expect(screen.getByRole('link', { name: 'Manage tools' })).toHaveAttribute('href', '#/tools')
  })

  it('shows loading, then a load failure', async () => {
    useMcpServerStore.setState({ servers: [], loaded: false, loading: true })
    await renderSettled()
    expect(screen.getByText('Loading MCP servers…')).toBeInTheDocument()

    act(() =>
      useMcpServerStore.setState({
        loading: false,
        error: { code: 'upstream_error', message: 'table missing' }
      })
    )
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Could not load MCP servers: table missing'
      )
    )
  })
})

describe('helpers', () => {
  it('liveMcpSelection keeps everything until the list has loaded', () => {
    expect(liveMcpSelection(['x', 'a'], [server('a')], false)).toEqual(['x', 'a'])
    expect(liveMcpSelection(['x', 'a'], [server('a')], true)).toEqual(['a'])
  })

  it('toolsSummary names the toolset and servers, or says none', () => {
    expect(toolsSummary(null, [], [])).toBe('none')
    expect(toolsSummary('fraud_detection', ['a', 'zz'], [server('a', 'GitHub MCP')])).toBe(
      'fraud_detection, GitHub MCP, zz'
    )
  })
})
