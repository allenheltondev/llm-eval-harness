/**
 * System + user prompt editors, written straight into `runConfigStore`.
 */

import { Card, CardBody, CardHeader, TextArea } from '@readysetcloud/ui'
import { useRunConfigStore } from '../../stores'

export default function PromptPanel() {
  const systemPrompt = useRunConfigStore(state => state.system_prompt)
  const userPrompt = useRunConfigStore(state => state.user_prompt)
  const setSystemPrompt = useRunConfigStore(state => state.setSystemPrompt)
  const setUserPrompt = useRunConfigStore(state => state.setUserPrompt)

  return (
    <Card role="region" aria-labelledby="prompt-panel-heading">
      <CardHeader>
        <h2 id="prompt-panel-heading" className="card-title">
          Prompts
        </h2>
      </CardHeader>
      <CardBody className="space-y-4">
        <TextArea
          label="System prompt"
          className="font-mono text-sm"
          rows={6}
          placeholder="You are a helpful assistant…"
          value={systemPrompt}
          onChange={event => setSystemPrompt(event.target.value)}
        />
        <TextArea
          label="User prompt"
          className="font-mono text-sm"
          rows={5}
          placeholder="Ask the model something…"
          value={userPrompt}
          onChange={event => setUserPrompt(event.target.value)}
        />
      </CardBody>
    </Card>
  )
}
