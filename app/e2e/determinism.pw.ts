import { expect, test } from '@playwright/test'
import { gotoTab } from './helpers'

const MODEL_ID = 'fake.model-v1-det'
const USER_PROMPT = 'Say hello in exactly one sentence.'

/**
 * Picks the model and prompt in the Evals tab itself, launches a 3-run
 * determinism evaluation against the fake model + fake judge, and checks the
 * progress, the finished result, that History gained the 3 underlying runs (a
 * model id unique to this spec keeps the count exact), and that the Workbench
 * shows the same model and prompt -- the launcher edits the shared run config.
 */
test('runs a determinism evaluation to completion and records its runs', async ({ page }) => {
  await page.goto('/')
  await gotoTab(page, 'Evals')

  // `/models` has no providers in this environment, so the picker offers its
  // manual-entry fallback -- the same one the Workbench shows.
  const launcher = page.getByTestId('evaluated-run-config')
  await launcher.getByLabel('Enter model id manually').fill(MODEL_ID)
  await launcher.getByLabel('User prompt').fill(USER_PROMPT)

  await page.getByLabel(/Number of runs/).fill('3')
  // Grader model defaults to a non-empty id (`amazon.nova-pro-v1:0`) from
  // settings, so the launcher is ready without needing the (unreachable)
  // model catalog.

  await page.getByRole('button', { name: 'Start evaluation' }).click()

  // The fake model + fake judge run entirely in-process, so a 3-run
  // determinism evaluation can finish before the UI's next paint — the live
  // progress panel and the finished result view are therefore both valid
  // observations of "the evaluation reached 3/3", not just the former.
  const progress = page.getByTestId('eval-progress')
  const result = page.getByTestId('eval-result')
  await expect(progress.or(result)).toBeVisible({ timeout: 20000 })

  if (await progress.isVisible()) {
    await expect(page.getByTestId('eval-progress-total')).toHaveText('3')
    await expect(page.getByTestId('eval-progress-completed')).toHaveText('3', { timeout: 20000 })
  }

  await expect(result).toBeVisible({ timeout: 20000 })
  await expect(page.getByTestId('eval-grade')).not.toHaveText('—')
  await expect(page.getByTestId('eval-score')).toContainText('/ 100')
  await expect(page.getByTestId('eval-metrics-grid')).toBeVisible()

  await gotoTab(page, 'History')
  await page.getByTestId('history-filter-model').fill(MODEL_ID)
  await expect(page.getByTestId('history-row').filter({ hasText: MODEL_ID })).toHaveCount(3)

  await gotoTab(page, 'Workbench')
  await expect(page.getByLabel('Enter model id manually')).toHaveValue(MODEL_ID)
  await expect(page.getByLabel('User prompt')).toHaveValue(USER_PROMPT)
})
