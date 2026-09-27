import { expect, test } from '@playwright/test'
import { gotoTab } from './helpers'

/**
 * Saved MCP servers against the real server (local SQLite store): add one on
 * the Tools tab with a secret header, see it listed by header *name* only,
 * pick it in the Workbench's tool picker, then delete it.
 *
 * Nothing here connects to the MCP server — there is none at this URL, and a
 * local stack accepts any http(s) URL — so the connection test and runs with
 * it are left to the unit tests.
 */
test('saves an MCP server, offers it in the Workbench, and deletes it', async ({ page }) => {
  const name = `E2E MCP ${Date.now()}`
  const secret = 'Bearer e2e-secret-value'

  await page.goto('/')
  await gotoTab(page, 'Tools')
  await expect(page.getByRole('heading', { name: 'MCP servers', level: 2 })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Built-in toolsets', level: 2 })).toBeVisible()

  await page.getByRole('button', { name: 'Add MCP server' }).first().click()
  await page.getByLabel('Name', { exact: true }).fill(name)
  await page.getByLabel('URL', { exact: true }).fill('http://localhost:65530/mcp')
  await page.getByRole('button', { name: 'Add header' }).click()
  await page.getByLabel('Header 1 name').fill('Authorization')
  await page.getByLabel('Header 1 value').fill(secret)
  await page.getByRole('button', { name: 'Save' }).click()

  const row = page.locator('li', { hasText: name })
  await expect(row).toBeVisible()
  await expect(row.getByText('http://localhost:65530/mcp')).toBeVisible()
  await expect(row.getByText('Authorization ••••')).toBeVisible()
  // The value is write-only: it is never rendered back.
  await expect(page.getByText(secret)).toHaveCount(0)

  // Editing shows the saved header by name, its value left blank.
  await page.getByRole('button', { name: `Edit ${name}` }).click()
  await expect(page.getByLabel('Header 1 name')).toHaveValue('Authorization')
  await expect(page.getByLabel('Header 1 value')).toHaveValue('')
  await expect(page.getByLabel('Header 1 value')).toHaveAttribute(
    'placeholder',
    'unchanged (saved)'
  )
  await page.getByRole('button', { name: 'Back' }).click()

  // The Workbench offers it as a tool.
  await gotoTab(page, 'Workbench')
  const checkbox = page.getByRole('checkbox', { name })
  await expect(checkbox).toBeVisible()
  await checkbox.check()
  await expect(checkbox).toBeChecked()

  // Delete it (after a confirm) and it is gone from the Workbench too.
  await gotoTab(page, 'Tools')
  await page.getByRole('button', { name: `Delete ${name}` }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.locator('li', { hasText: name })).toHaveCount(0)

  await gotoTab(page, 'Workbench')
  await expect(page.getByRole('group', { name: 'Tools' })).toBeVisible()
  await expect(page.getByRole('checkbox', { name })).toHaveCount(0)
})
