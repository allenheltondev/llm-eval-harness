/**
 * Create/edit form for a saved MCP server: name, URL and auth headers.
 *
 * Header values are write-only on the server — the API returns only their
 * names — so editing is a *patch* (`PUT` `headers`):
 *
 * - a saved header shows its name (fixed) and an empty value field whose
 *   placeholder says it is unchanged; left blank, it is omitted from the
 *   patch and the server keeps it;
 * - typing a value there replaces it;
 * - removing the row sends `null`, which deletes it;
 * - a new row is sent as name -> value.
 *
 * To rename a saved header, remove it and add a new row.
 */

import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  Input
} from '@readysetcloud/ui'
import { useNotify } from '../../components/notify'
import { useMcpServerStore } from '../../stores'
import type { McpServer, McpServerUpdate } from '../../api'

/** Server-side limits (`mcp/schemas.py`). */
const MAX_NAME_LENGTH = 64
const MAX_HEADERS = 10

export interface HeaderRow {
  /** Stable React key; rows are added and removed freely. */
  key: number
  name: string
  value: string
  /** A header already stored on the server (name fixed, value unknown). */
  saved: boolean
}

export interface HeaderPatchResult {
  headers: Record<string, string | null>
  error: string | null
}

/**
 * Pure: the `headers` patch for an edit, or for a create (no saved rows, no
 * removals). Rows with neither a name nor a value are ignored.
 */
export function buildHeaderPatch(rows: HeaderRow[], removed: string[]): HeaderPatchResult {
  const headers: Record<string, string | null> = {}
  const seen = new Set<string>()

  for (const row of rows) {
    const name = row.name.trim()
    if (!row.saved && name === '' && row.value === '') continue
    if (name === '') return { headers: {}, error: 'Every header needs a name.' }
    const lowered = name.toLowerCase()
    if (seen.has(lowered)) {
      return { headers: {}, error: `Header "${name}" is listed twice.` }
    }
    seen.add(lowered)
    if (row.saved) {
      if (row.value !== '') headers[name] = row.value
      continue
    }
    if (row.value === '') return { headers: {}, error: `Header "${name}" needs a value.` }
    headers[name] = row.value
  }

  if (seen.size > MAX_HEADERS) {
    return { headers: {}, error: `At most ${MAX_HEADERS} headers.` }
  }

  for (const name of removed) {
    // Re-added under the same name: the new value replaces it anyway, and the
    // server rejects a patch naming one header twice.
    if (!seen.has(name.toLowerCase())) headers[name] = null
  }

  return { headers, error: null }
}

let nextKey = 0
function newRow(name = '', saved = false): HeaderRow {
  nextKey += 1
  return { key: nextKey, name, value: '', saved }
}

export default function McpServerEditor({
  server,
  onClose
}: {
  /** The server to edit, or `null` to create one. */
  server: McpServer | null
  onClose: () => void
}) {
  const [name, setName] = useState(server?.name ?? '')
  const [url, setUrl] = useState(server?.url ?? '')
  const [rows, setRows] = useState<HeaderRow[]>(() =>
    (server?.header_names ?? []).map(headerName => newRow(headerName, true))
  )
  const [removed, setRemoved] = useState<string[]>([])
  const [nameError, setNameError] = useState<string | null>(null)
  const [urlError, setUrlError] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const notify = useNotify()

  const createServer = useMcpServerStore(state => state.createServer)
  const updateServer = useMcpServerStore(state => state.updateServer)
  const clearSaveError = useMcpServerStore(state => state.clearSaveError)
  const saving = useMcpServerStore(state => state.saving)
  const saveError = useMcpServerStore(state => state.saveError)

  // A failure left over from an earlier form is not this form's.
  const clearedRef = useRef(false)
  useEffect(() => {
    if (clearedRef.current) return
    clearedRef.current = true
    clearSaveError()
  }, [clearSaveError])

  function updateRow(key: number, patch: Partial<HeaderRow>) {
    setRows(prev => prev.map(row => (row.key === key ? { ...row, ...patch } : row)))
  }

  function removeRow(row: HeaderRow) {
    setRows(prev => prev.filter(entry => entry.key !== row.key))
    if (row.saved) setRemoved(prev => [...prev, row.name])
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setNameError(null)
    setUrlError(null)
    setFormError(null)

    const trimmedName = name.trim()
    const trimmedUrl = url.trim()
    let invalid = false
    if (trimmedName === '') {
      setNameError('Name is required.')
      invalid = true
    }
    if (trimmedUrl === '') {
      setUrlError('URL is required.')
      invalid = true
    } else if (!/^https?:\/\/\S+$/i.test(trimmedUrl)) {
      setUrlError('Enter an http:// or https:// URL.')
      invalid = true
    }
    const patch = buildHeaderPatch(rows, removed)
    if (patch.error) {
      setFormError(patch.error)
      invalid = true
    }
    if (invalid) return

    let result: McpServer | null
    if (server) {
      const body: McpServerUpdate = {}
      if (trimmedName !== server.name) body.name = trimmedName
      if (trimmedUrl !== server.url) body.url = trimmedUrl
      if (Object.keys(patch.headers).length > 0) body.headers = patch.headers
      result = await updateServer(server.id, body)
    } else {
      const headers = patch.headers as Record<string, string>
      result = await createServer({
        name: trimmedName,
        url: trimmedUrl,
        ...(Object.keys(headers).length > 0 ? { headers } : {})
      })
    }

    if (result) {
      notify(server ? 'MCP server saved' : 'MCP server added', { variant: 'success' })
      onClose()
    }
  }

  return (
    <form
      className="max-w-3xl mx-auto space-y-4"
      data-testid="mcp-server-editor"
      noValidate
      onSubmit={event => void handleSubmit(event)}
    >
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">
          {server ? 'Edit MCP server' : 'Add MCP server'}
        </h2>
        <Button variant="secondary" onClick={onClose}>
          Back
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Server</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          <Input
            label="Name"
            hint="Shown in the tool picker; tool names in a run's transcript are prefixed with it."
            aria-required="true"
            type="text"
            maxLength={MAX_NAME_LENGTH}
            value={name}
            error={nameError ?? undefined}
            onChange={event => setName(event.target.value)}
          />
          <Input
            label="URL"
            hint="The server's streamable-HTTP endpoint. Deployed stacks only reach public https URLs."
            aria-required="true"
            type="url"
            className="font-mono"
            placeholder="https://mcp.example.com/mcp"
            value={url}
            error={urlError ?? undefined}
            onChange={event => setUrl(event.target.value)}
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Headers</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3" data-testid="mcp-header-rows">
          <Alert variant="info" data-testid="mcp-header-note">
            Sent with every request to the server, e.g. <code>Authorization</code>. Values are
            stored encrypted and are never shown again — only header names are.
          </Alert>

          {rows.map((row, index) => (
            <div
              key={row.key}
              className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2 sm:items-center"
              data-testid={`mcp-header-row-${index}`}
            >
              <Field label={<span className="sr-only">Header {index + 1} name</span>}>
                {props => (
                  <input
                    {...props}
                    type="text"
                    className="input font-mono"
                    placeholder="Header name"
                    value={row.name}
                    readOnly={row.saved}
                    title={
                      row.saved
                        ? 'To rename a saved header, remove it and add a new one'
                        : undefined
                    }
                    onChange={event => updateRow(row.key, { name: event.target.value })}
                  />
                )}
              </Field>
              <Field label={<span className="sr-only">Header {index + 1} value</span>}>
                {props => (
                  <input
                    {...props}
                    type="password"
                    autoComplete="off"
                    className="input font-mono"
                    placeholder={row.saved ? 'unchanged (saved)' : 'Value'}
                    value={row.value}
                    onChange={event => updateRow(row.key, { value: event.target.value })}
                  />
                )}
              </Field>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Remove header ${row.name.trim() || index + 1}`}
                onClick={() => removeRow(row)}
              >
                Remove
              </Button>
            </div>
          ))}

          {server && rows.some(row => row.saved) && (
            <p className="field-hint">Leave a saved header's value blank to keep it.</p>
          )}

          <Button
            variant="secondary"
            disabled={rows.length >= MAX_HEADERS}
            onClick={() => setRows(prev => [...prev, newRow()])}
          >
            Add header
          </Button>
        </CardBody>
      </Card>

      {formError && (
        <Alert variant="error" data-testid="mcp-form-error">
          {formError}
        </Alert>
      )}

      {saveError && (
        <Alert variant="error" data-testid="mcp-save-error">
          Could not save: {saveError.message}
        </Alert>
      )}

      <div className="flex gap-2">
        <Button type="submit" loading={saving} loadingLabel="Saving…">
          Save
        </Button>
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
