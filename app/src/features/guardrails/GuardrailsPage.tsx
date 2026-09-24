/**
 * The Guardrails tab: list, create/edit, and version management.
 *
 * A single piece of local view state decides what's on screen — the list, the
 * editor (create or edit), or the versions panel for one guardrail — so only
 * one of them is ever mounted at a time and each can own its own effects.
 */

import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Loading,
  StatusBadge
} from '@readysetcloud/ui'
import { useNotify } from '../../components/notify'
import { statusTone } from '../../components/status'
import { useGuardrailStore, type GuardrailStateData } from '../../stores'
import type { GuardrailLifecycleStatus, GuardrailSummary } from '../../api'
import GuardrailEditor from './GuardrailEditor'
import VersionsPanel from './VersionsPanel'

type View =
  | { mode: 'list' }
  | { mode: 'editor'; guardrailId: string | null }
  | { mode: 'versions'; guardrailId: string; name: string }

const STATUS_LABELS: Record<GuardrailLifecycleStatus, string> = {
  READY: 'Ready',
  CREATING: 'Creating',
  UPDATING: 'Updating',
  VERSIONING: 'Versioning',
  FAILED: 'Failed',
  DELETING: 'Deleting'
}

function LifecycleBadge({ status }: { status: GuardrailLifecycleStatus }) {
  return (
    <StatusBadge tone={statusTone(status)} role={undefined}>
      {STATUS_LABELS[status]}
    </StatusBadge>
  )
}

function formatDate(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

function GuardrailRow({
  guardrail,
  onEdit,
  onVersions
}: {
  guardrail: GuardrailSummary
  onEdit: () => void
  onVersions: () => void
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const removeGuardrail = useGuardrailStore(state => state.removeGuardrail)
  const saving = useGuardrailStore(state => state.saving)
  const notify = useNotify()

  async function handleDelete() {
    const removed = await removeGuardrail(guardrail.id)
    if (removed) {
      setConfirmingDelete(false)
      notify(`Deleted ${guardrail.name}`, { variant: 'success' })
    }
  }

  return (
    <tr className="border-b border-border last:border-0">
      <td className="py-2 pr-4">
        <div className="text-sm font-medium text-foreground">{guardrail.name}</div>
        <div className="text-xs text-muted-foreground font-mono">{guardrail.id}</div>
      </td>
      <td className="py-2 pr-4">
        <LifecycleBadge status={guardrail.status} />
      </td>
      <td className="py-2 pr-4 text-sm text-muted-foreground">{guardrail.version}</td>
      <td className="py-2 pr-4 text-sm text-muted-foreground">{formatDate(guardrail.createdAt)}</td>
      <td className="py-2 pl-2">
        {confirmingDelete ? (
          <div className="flex items-center justify-end gap-2">
            <span className="text-xs text-muted-foreground">Delete?</span>
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
          <div className="flex items-center justify-end gap-1">
            <Button variant="ghost" size="sm" onClick={onEdit}>
              Edit
            </Button>
            <Button variant="ghost" size="sm" onClick={onVersions}>
              Versions
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(true)}>
              Delete
            </Button>
          </div>
        )}
      </td>
    </tr>
  )
}

function GuardrailList({
  guardrails,
  loading,
  loaded,
  error,
  onRetry,
  onNew,
  onEdit,
  onVersions
}: {
  guardrails: GuardrailSummary[]
  loading: GuardrailStateData['loading']
  loaded: GuardrailStateData['loaded']
  error: GuardrailStateData['error']
  onRetry: () => void
  onNew: () => void
  onEdit: (id: string) => void
  onVersions: (guardrail: GuardrailSummary) => void
}) {
  return (
    <Card role="region" aria-labelledby="guardrails-heading">
      <CardHeader className="flex items-center justify-between gap-3">
        <h2 id="guardrails-heading" className="card-title">
          Guardrails
        </h2>
        <Button onClick={onNew}>New guardrail</Button>
      </CardHeader>

      <CardBody>
        {error && (
          <ErrorState
            className="mb-3"
            heading="Could not load guardrails"
            message={error.message}
            action={{ label: 'Retry', onClick: onRetry }}
          />
        )}

        {loading && guardrails.length === 0 && <Loading text="Loading guardrails…" />}

        {loaded && !loading && guardrails.length === 0 && !error && (
          <div data-testid="guardrails-empty">
            <EmptyState title="No guardrails yet" description="Create one to get started." />
          </div>
        )}

        {guardrails.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left" data-testid="guardrails-table">
              <thead>
                <tr className="border-b border-border text-xs font-medium text-muted-foreground uppercase tracking-wide">
                  <th className="py-2 pr-4">Name</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Version</th>
                  <th className="py-2 pr-4">Created</th>
                  <th className="py-2 pl-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {guardrails.map(guardrail => (
                  <GuardrailRow
                    key={guardrail.id}
                    guardrail={guardrail}
                    onEdit={() => onEdit(guardrail.id)}
                    onVersions={() => onVersions(guardrail)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  )
}

export default function GuardrailsPage() {
  const [view, setView] = useState<View>({ mode: 'list' })

  const guardrails = useGuardrailStore(state => state.guardrails)
  const loading = useGuardrailStore(state => state.loading)
  const loaded = useGuardrailStore(state => state.loaded)
  const error = useGuardrailStore(state => state.error)
  const loadGuardrails = useGuardrailStore(state => state.loadGuardrails)

  useEffect(() => {
    void loadGuardrails()
  }, [loadGuardrails])

  if (view.mode === 'editor') {
    return (
      <GuardrailEditor guardrailId={view.guardrailId} onClose={() => setView({ mode: 'list' })} />
    )
  }

  if (view.mode === 'versions') {
    return (
      <VersionsPanel
        guardrailId={view.guardrailId}
        guardrailName={view.name}
        onClose={() => setView({ mode: 'list' })}
      />
    )
  }

  return (
    <div data-testid="guardrails-page">
      <GuardrailList
        guardrails={guardrails}
        loading={loading}
        loaded={loaded}
        error={error}
        onRetry={() => void loadGuardrails()}
        onNew={() => setView({ mode: 'editor', guardrailId: null })}
        onEdit={id => setView({ mode: 'editor', guardrailId: id })}
        onVersions={guardrail =>
          setView({ mode: 'versions', guardrailId: guardrail.id, name: guardrail.name })
        }
      />
    </div>
  )
}
