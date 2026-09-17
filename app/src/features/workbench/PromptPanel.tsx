/**
 * System + user prompt editors, written straight into `runConfigStore`.
 */

import { useRunConfigStore } from '../../stores'

export default function PromptPanel() {
  const systemPrompt = useRunConfigStore(state => state.system_prompt)
  const userPrompt = useRunConfigStore(state => state.user_prompt)
  const setSystemPrompt = useRunConfigStore(state => state.setSystemPrompt)
  const setUserPrompt = useRunConfigStore(state => state.setUserPrompt)

  return (
    <section className="card" aria-labelledby="prompt-panel-heading">
      <h2 id="prompt-panel-heading" className="text-base font-semibold text-gray-900 mb-3">
        Prompts
      </h2>

      <div className="mb-4">
        <label htmlFor="system-prompt" className="block text-xs font-medium text-gray-700 mb-1">
          System prompt
        </label>
        <textarea
          id="system-prompt"
          className="input-field font-mono text-sm"
          rows={6}
          placeholder="You are a helpful assistant…"
          value={systemPrompt}
          onChange={event => setSystemPrompt(event.target.value)}
        />
      </div>

      <div>
        <label htmlFor="user-prompt" className="block text-xs font-medium text-gray-700 mb-1">
          User prompt
        </label>
        <textarea
          id="user-prompt"
          className="input-field font-mono text-sm"
          rows={5}
          placeholder="Ask the model something…"
          value={userPrompt}
          onChange={event => setUserPrompt(event.target.value)}
        />
      </div>
    </section>
  )
}
