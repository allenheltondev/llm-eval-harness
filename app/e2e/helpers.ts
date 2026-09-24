import type { Page } from '@playwright/test'

/**
 * Fills the Workbench's model id and user prompt.
 *
 * `/api/v1/models` always fails in this E2E environment (no AWS credentials),
 * so `ModelPanel` renders its "Enter model id manually" fallback instead of
 * the catalog dropdown — this waits for that error state rather than racing
 * the initial `loadModels()` call.
 */
export async function configureWorkbench(
  page: Page,
  { modelId, userPrompt }: { modelId: string; userPrompt: string }
): Promise<void> {
  await page.getByLabel('Enter model id manually').fill(modelId)
  await page.getByLabel('User prompt').fill(userPrompt)
}

/** Switches section through the AppNav rail's links. */
export async function gotoTab(
  page: Page,
  name: 'Workbench' | 'Evals' | 'History' | 'Guardrails' | 'About'
): Promise<void> {
  await page
    .getByRole('navigation', { name: 'Primary navigation' })
    .getByRole('link', { name })
    .click()
}
