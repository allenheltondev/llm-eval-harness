/**
 * ToolsPage, wired to the real `mcpServerStore` with only the API stubbed:
 * the saved-server list, add (with headers), edit's header *patch* (blank
 * keeps, a new value replaces, a removed row sends null), delete, the
 * connection test, and the read-only built-in toolsets.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ToastProvider } from '@readysetcloud/ui'
import type { McpServer, ToolsResponse } from '../../../api'

const toolsMock = vi.fn<() => Promise<ToolsResponse>>()
const listMock = vi.fn()
const createMock = vi.fn()
const updateMock = vi.fn()
const removeMock = vi.fn()
const testMock = vi.fn()

vi.mock('../../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      tools: toolsMock,
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

const { default: ToolsPage } = await import('../ToolsPage')
const { buildHeaderPatch } = await import('../McpServerEditor')
const { ApiError } = await import('../../../api')
const { DEFAULT_RUN_CONFIG, useMcpServerStore, useRunConfigStore } = await import('../../../stores')

const GITHUB: McpServer = {
  id: 'mcp-1',
  name: 'GitHub MCP',
  url: 'https://mcp.github.example/mcp',
  header_names: ['Authorization', 'X-Team'],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z'
}

const DOCS: McpServer = {
  id: 'mcp-2',
  name: 'Docs',
  url: 'http://localhost:9000/mcp',
  header_names: [],
  created_at: '2026-09-02T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z'
}

function renderPage() {
  return render(
    <ToastProvider>
      <ToolsPage />
    </ToastProvider>
  )
}

async function renderLoaded() {
  renderPage()
  await screen.findByTestId('mcp-servers-list')
}

function headerRow(index: number) {
  return within(screen.getByTestId(`mcp-header-row-${index}`))
}

beforeEach(() => {
  for (const mock of [toolsMock, listMock, createMock, updateMock, removeMock, testMock]) {
    mock.mockReset()
  }
  toolsMock.mockResolvedValue({
    toolsets: [
      { name: 'fraud_detection', tools: ['lookupAccount', 'flagTransaction'] },
      { name: 'empty', tools: [] }
    ]
  })
  listMock.mockResolvedValue({ servers: [GITHUB, DOCS] })
  useMcpServerStore.getState().clear()
  useRunConfigStore.setState({ ...DEFAULT_RUN_CONFIG })
})

describe('ToolsPage list', () => {
  it('lists each saved server with its URL and header names, never values', async () => {
    await renderLoaded()

    const row = within(screen.getByTestId('mcp-server-mcp-1'))
    expect(row.getByText('GitHub MCP')).toBeInTheDocument()
    expect(row.getByText('https://mcp.github.example/mcp')).toBeInTheDocument()
    expect(row.getByText('Authorization ••••')).toBeInTheDocument()
    expect(row.getByText('X-Team ••••')).toBeInTheDocument()

    const docs = within(screen.getByTestId('mcp-server-mcp-2'))
    expect(docs.getByText('http://localhost:9000/mcp')).toBeInTheDocument()
    expect(docs.queryByLabelText('Headers')).not.toBeInTheDocument()
    expect(listMock).toHaveBeenCalledTimes(1)
  })

  it('shows the built-in toolsets read-only', async () => {
    await renderLoaded()

    const builtIn = within(await screen.findByTestId('builtin-toolsets'))
    expect(builtIn.getByText('fraud_detection')).toBeInTheDocument()
    expect(builtIn.getByText('lookupAccount')).toBeInTheDocument()
    expect(builtIn.getByText('flagTransaction')).toBeInTheDocument()
    expect(builtIn.getByText('No tools in this toolset')).toBeInTheDocument()
    expect(builtIn.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('reports a built-in toolset failure, and an empty toolset list', async () => {
    toolsMock.mockRejectedValueOnce(new Error('tools down'))
    const { unmount } = renderPage()
    expect(await screen.findByText('Could not load toolsets: tools down')).toBeInTheDocument()
    unmount()

    toolsMock.mockResolvedValueOnce({ toolsets: [] })
    renderPage()
    expect(await screen.findByText('No built-in toolsets.')).toBeInTheDocument()
  })

  it('shows an empty state with an add action when nothing is saved', async () => {
    listMock.mockResolvedValue({ servers: [] })
    renderPage()

    const empty = await screen.findByTestId('mcp-servers-empty')
    expect(empty).toHaveTextContent('No MCP servers yet')
    fireEvent.click(within(empty).getByRole('button', { name: 'Add MCP server' }))
    expect(screen.getByTestId('mcp-server-editor')).toBeInTheDocument()
  })

  it('shows a load failure with a retry that forces a reload', async () => {
    listMock.mockRejectedValueOnce(new ApiError('table missing', { code: 'upstream_error' }))
    renderPage()

    expect(await screen.findByText('table missing')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await screen.findByTestId('mcp-servers-list')
    expect(listMock).toHaveBeenCalledTimes(2)
  })
})

describe('ToolsPage add', () => {
  it('creates a server with its headers, as password fields, and returns to the list', async () => {
    createMock.mockResolvedValue({
      ...DOCS,
      id: 'mcp-3',
      name: 'Linear',
      url: 'https://mcp.linear.example/sse',
      header_names: ['Authorization']
    })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))
    expect(screen.getByTestId('mcp-header-note')).toHaveTextContent(
      'Values are stored encrypted and are never shown again'
    )
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '  Linear ' } })
    fireEvent.change(screen.getByLabelText('URL'), {
      target: { value: 'https://mcp.linear.example/sse' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add header' }))
    fireEvent.change(headerRow(0).getByLabelText('Header 1 name'), {
      target: { value: 'Authorization' }
    })
    const value = headerRow(0).getByLabelText('Header 1 value')
    expect(value).toHaveAttribute('type', 'password')
    fireEvent.change(value, { target: { value: 'Bearer lin_123' } })
    // A blank extra row is ignored.
    fireEvent.click(screen.getByRole('button', { name: 'Add header' }))

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByTestId('mcp-servers-list')
    expect(createMock).toHaveBeenCalledWith({
      name: 'Linear',
      url: 'https://mcp.linear.example/sse',
      headers: { Authorization: 'Bearer lin_123' }
    })
    expect(within(screen.getByTestId('mcp-server-mcp-3')).getByText('Linear')).toBeInTheDocument()
    expect(await screen.findByText('MCP server added')).toBeInTheDocument()
  })

  it('omits headers when none are given', async () => {
    createMock.mockResolvedValue({ ...DOCS, id: 'mcp-9' })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Local' } })
    fireEvent.change(screen.getByLabelText('URL'), {
      target: { value: 'http://localhost:9000/mcp' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(createMock).toHaveBeenCalledWith({ name: 'Local', url: 'http://localhost:9000/mcp' })
    )
  })

  it('validates name, URL and headers before sending anything', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByText('Name is required.')).toBeInTheDocument()
    expect(screen.getByText('URL is required.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'X' } })
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'ftp://nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByText('Enter an http:// or https:// URL.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://ok.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add header' }))
    fireEvent.change(headerRow(0).getByLabelText('Header 1 name'), {
      target: { value: 'Authorization' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByTestId('mcp-form-error')).toHaveTextContent(
      'Header "Authorization" needs a value.'
    )

    expect(createMock).not.toHaveBeenCalled()
  })

  it('shows the server-side rejection (e.g. a non-public URL on a deployed stack)', async () => {
    createMock.mockRejectedValue(
      new ApiError('MCP server URLs must use https', { code: 'invalid_mcp_url', status: 400 })
    )
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Local' } })
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'http://localhost:1/mcp' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByTestId('mcp-save-error')).toHaveTextContent(
      'Could not save: MCP server URLs must use https'
    )
    expect(screen.getByTestId('mcp-server-editor')).toBeInTheDocument()

    // Leaving the form drops the error; it does not follow the user back.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('mcp-delete-error')).not.toBeInTheDocument()
  })
})

describe('ToolsPage edit', () => {
  async function openEditor() {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Edit GitHub MCP' }))
    return screen.getByTestId('mcp-server-editor')
  }

  it('shows saved headers by name with an empty value saying it is unchanged', async () => {
    await openEditor()

    expect(screen.getByLabelText('Name')).toHaveValue('GitHub MCP')
    expect(screen.getByLabelText('URL')).toHaveValue('https://mcp.github.example/mcp')
    const name = headerRow(0).getByLabelText('Header 1 name')
    expect(name).toHaveValue('Authorization')
    expect(name).toHaveAttribute('readonly')
    const value = headerRow(0).getByLabelText('Header 1 value')
    expect(value).toHaveValue('')
    expect(value).toHaveAttribute('placeholder', 'unchanged (saved)')
    expect(value).toHaveAttribute('type', 'password')
  })

  it('sends nothing but what changed: blank keeps, a value replaces, a removed row is null', async () => {
    updateMock.mockResolvedValue({ ...GITHUB, header_names: ['Authorization', 'X-Region'] })
    await openEditor()

    // Authorization: new value. X-Team: removed. X-Region: added.
    fireEvent.change(headerRow(0).getByLabelText('Header 1 value'), {
      target: { value: 'Bearer new' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Remove header X-Team' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add header' }))
    fireEvent.change(headerRow(1).getByLabelText('Header 2 name'), {
      target: { value: 'X-Region' }
    })
    fireEvent.change(headerRow(1).getByLabelText('Header 2 value'), { target: { value: 'eu' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByTestId('mcp-servers-list')
    expect(updateMock).toHaveBeenCalledWith('mcp-1', {
      headers: { Authorization: 'Bearer new', 'X-Team': null, 'X-Region': 'eu' }
    })
    expect(
      within(screen.getByTestId('mcp-server-mcp-1')).getByText('X-Region ••••')
    ).toBeInTheDocument()
  })

  it('leaves headers out entirely when only the name changes', async () => {
    updateMock.mockResolvedValue({ ...GITHUB, name: 'GitHub' })
    await openEditor()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'GitHub' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(updateMock).toHaveBeenCalledWith('mcp-1', { name: 'GitHub' }))
  })

  it('sends a changed URL', async () => {
    updateMock.mockResolvedValue({ ...GITHUB, url: 'https://new.example/mcp' })
    await openEditor()

    fireEvent.change(screen.getByLabelText('URL'), {
      target: { value: 'https://new.example/mcp' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith('mcp-1', { url: 'https://new.example/mcp' })
    )
  })

  it('Back returns to the list without saving', async () => {
    await openEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))

    expect(screen.getByTestId('tools-page')).toBeInTheDocument()
    expect(updateMock).not.toHaveBeenCalled()
  })
})

describe('ToolsPage delete', () => {
  it('asks first, then deletes and unticks it from the run config', async () => {
    removeMock.mockResolvedValue(undefined)
    useRunConfigStore.setState({ mcp_servers: ['mcp-1', 'mcp-2'] })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Delete GitHub MCP' }))
    expect(screen.getByText('Delete GitHub MCP?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(removeMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Delete GitHub MCP' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(screen.queryByTestId('mcp-server-mcp-1')).not.toBeInTheDocument())
    expect(removeMock).toHaveBeenCalledWith('mcp-1')
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['mcp-2'])
    expect(await screen.findByText('Deleted GitHub MCP')).toBeInTheDocument()
  })

  it('leaves an unselected server out of the run config alone', async () => {
    removeMock.mockResolvedValue(undefined)
    useRunConfigStore.setState({ mcp_servers: ['mcp-2'] })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Delete GitHub MCP' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(removeMock).toHaveBeenCalled())
    expect(useRunConfigStore.getState().mcp_servers).toEqual(['mcp-2'])
  })

  it('keeps the row and shows why when the delete fails', async () => {
    removeMock.mockRejectedValue(
      new ApiError('store unavailable', { code: 'mcp_store_unavailable' })
    )
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Delete GitHub MCP' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByTestId('mcp-delete-error')).toHaveTextContent('store unavailable')
    expect(screen.getByTestId('mcp-server-mcp-1')).toBeInTheDocument()
  })
})

describe('ToolsPage test connection', () => {
  it('lists the tools a reachable server offers', async () => {
    testMock.mockResolvedValue({
      ok: true,
      tools: [
        { name: 'search_issues', description: 'Search issues' },
        { name: 'get_repo', description: null }
      ],
      error: null
    })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Test GitHub MCP' }))

    const ok = await screen.findByTestId('mcp-test-ok')
    expect(testMock).toHaveBeenCalledWith('mcp-1')
    expect(ok).toHaveTextContent('Connected — 2 tools.')
    expect(ok).toHaveTextContent('search_issues — Search issues')
    expect(ok).toHaveTextContent('get_repo')
  })

  it('says "1 tool" for a single tool, and nothing more for none', async () => {
    testMock
      .mockResolvedValueOnce({
        ok: true,
        tools: [{ name: 'only', description: null }],
        error: null
      })
      .mockResolvedValueOnce({ ok: true, tools: [], error: null })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Test GitHub MCP' }))
    expect(await screen.findByTestId('mcp-test-ok')).toHaveTextContent('Connected — 1 tool.')

    fireEvent.click(screen.getByRole('button', { name: 'Test Docs' }))
    await waitFor(() => expect(screen.getAllByTestId('mcp-test-ok')).toHaveLength(2))
    expect(screen.getAllByTestId('mcp-test-ok')[1]).toHaveTextContent('Connected — 0 tools.')
  })

  it('shows why an unreachable server failed', async () => {
    testMock.mockResolvedValue({ ok: false, tools: [], error: 'Connection refused' })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Test Docs' }))

    expect(await screen.findByTestId('mcp-test-error')).toHaveTextContent(
      'Could not connect: Connection refused'
    )
  })

  it('falls back to a generic message when the server gives none', async () => {
    testMock.mockResolvedValue({ ok: false, tools: [], error: null })
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Test Docs' }))

    expect(await screen.findByTestId('mcp-test-error')).toHaveTextContent(
      'Could not connect: unknown error'
    )
  })
})

describe('buildHeaderPatch', () => {
  const row = (name: string, value: string, saved = false, key = 0) => ({ key, name, value, saved })

  it('rejects a nameless row with a value, and duplicate names (case-insensitive)', () => {
    expect(buildHeaderPatch([row('', 'x')], []).error).toBe('Every header needs a name.')
    expect(buildHeaderPatch([row('Auth', 'a'), row('auth', 'b')], []).error).toBe(
      'Header "auth" is listed twice.'
    )
  })

  it('caps the number of headers', () => {
    const rows = Array.from({ length: 11 }, (_, i) => row(`H${i}`, 'v', false, i))
    expect(buildHeaderPatch(rows, []).error).toBe('At most 10 headers.')
  })

  it('does not also null a removed header that is re-added under the same name', () => {
    expect(buildHeaderPatch([row('authorization', 'new')], ['Authorization'])).toEqual({
      headers: { authorization: 'new' },
      error: null
    })
  })
})
