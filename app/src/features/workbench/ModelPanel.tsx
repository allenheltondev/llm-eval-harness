/**
 * Model picker card for the Workbench: the shared `ModelPicker` under a
 * "Model" heading, plus the catalog-cache badge.
 */

import { Badge, Card, CardBody, CardHeader } from '@readysetcloud/ui'
import { useModelStore } from '../../stores'
import ModelPicker from '../../components/ModelPicker'

export default function ModelPanel() {
  const cached = useModelStore(state => state.modelsCached)

  return (
    <Card role="region" aria-labelledby="model-panel-heading">
      <CardHeader className="flex items-center justify-between">
        <h2 id="model-panel-heading" className="card-title">
          Model
        </h2>
        {cached && (
          <Badge variant="neutral" title="Served from the server's catalog cache">
            cached
          </Badge>
        )}
      </CardHeader>
      <CardBody>
        {/* The card heading already reads "Model", so the picker's own label
            is screen-reader only. */}
        <ModelPicker label={<span className="sr-only">Model</span>} />
      </CardBody>
    </Card>
  )
}
