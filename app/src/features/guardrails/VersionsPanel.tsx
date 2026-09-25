/**
 * Version history for one guardrail, plus publishing the current DRAFT.
 *
 * Bedrock's model is DRAFT-plus-versions: `loadVersions` returns the DRAFT row
 * alongside every numbered version, and `publishVersion` freezes the DRAFT
 * into a new numbered version (with an optional description).
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardDescription,
  CardFooter,
  CardHeader,
  EmptyState,
  Input,
  Loading
} from '@readysetcloud/ui'
import { useGuardrailStore } from '../../stores'
import type { GuardrailVersionSummary } from '../../api'

/**
 * A stable empty-array fallback. Building `[]` inline inside the selector
 * would return a new array identity on every render and fail zustand v5's
 * `useSyncExternalStore` snapshot check (see `guardrailStore.readyGuardrails`).
 */
const EMPTY_VERSIONS: GuardrailVersionSummary[] = []

export default function VersionsPanel({
  guardrailId,
  guardrailName,
  onClose
}: {
  guardrailId: string
  guardrailName: string
  onClose: () => void
}) {
  const [publishing, setPublishing] = useState(false)
  const [description, setDescription] = useState('')
  const [loading, setLoading] = useState(true)

  const versions = useGuardrailStore(state => state.versions[guardrailId]) ?? EMPTY_VERSIONS
  const loadVersions = useGuardrailStore(state => state.loadVersions)
  const publishVersion = useGuardrailStore(state => state.publishVersion)
  const saving = useGuardrailStore(state => state.saving)
  const saveError = useGuardrailStore(state => state.saveError)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void loadVersions(guardrailId).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [guardrailId, loadVersions])

  async function handlePublish() {
    const trimmed = description.trim()
    const result = await publishVersion(guardrailId, trimmed === '' ? undefined : trimmed)
    if (result) {
      setPublishing(false)
      setDescription('')
    }
  }

  return (
    <Card className="max-w-3xl mx-auto" data-testid="versions-panel">
      <CardHeader className="flex items-center justify-between gap-3">
        <div>
          <h2 className="card-title">Versions</h2>
          <CardDescription>{guardrailName}</CardDescription>
        </div>
        <Button variant="secondary" onClick={onClose}>
          Back
        </Button>
      </CardHeader>

      <CardBody>
        {loading && <Loading text="Loading versions…" />}

        {!loading && versions.length === 0 && (
          <div data-testid="versions-empty">
            <EmptyState title="No versions yet" />
          </div>
        )}

        {!loading && versions.length > 0 && (
          <ul className="divide-y divide-border" data-testid="versions-list">
            {versions.map(version => (
              <li key={version.version} className="py-2 flex items-center justify-between">
                <div>
                  <span className="text-sm font-medium text-foreground">
                    {version.version === 'DRAFT' ? 'DRAFT' : `Version ${version.version}`}
                  </span>
                  {version.description && (
                    <p className="text-xs text-muted-foreground mt-0.5">{version.description}</p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardBody>

      <CardFooter>
        {publishing ? (
          <div className="space-y-2">
            <Input
              label="Version description (optional)"
              type="text"
              value={description}
              onChange={event => setDescription(event.target.value)}
              placeholder="What changed in this version?"
            />
            <div className="flex gap-2">
              <Button
                loading={saving}
                loadingLabel="Publishing…"
                onClick={() => void handlePublish()}
              >
                Confirm publish
              </Button>
              <Button
                variant="secondary"
                disabled={saving}
                onClick={() => {
                  setPublishing(false)
                  setDescription('')
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <Button onClick={() => setPublishing(true)}>Publish current DRAFT</Button>
        )}

        {saveError && (
          <Alert variant="error" className="mt-2">
            Could not publish: {saveError.message}
          </Alert>
        )}
      </CardFooter>
    </Card>
  )
}
