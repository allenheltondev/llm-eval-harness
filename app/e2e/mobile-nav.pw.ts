import { expect, test } from '@playwright/test'

/**
 * Below 641px AppNav is a collapsible top bar. Following one of its links
 * must land on the page chosen with the menu closed -- AppNav does not close
 * it itself, and the shell remounts the nav to do so, which must not stop the
 * browser following the link.
 */
test.use({ viewport: { width: 390, height: 844 } })

test('the mobile menu closes once a link is followed', async ({ page }) => {
  await page.goto('/#/workbench')

  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  const nav = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(nav).toBeHidden()

  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await nav.getByRole('link', { name: 'History' }).click()

  await expect(page).toHaveURL(/#\/history$/)
  await expect(page.getByRole('main', { name: 'History' })).toBeVisible()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(nav).toBeHidden()

  // The page already shown: no route change, and still closes.
  await toggle.click()
  await nav.getByRole('link', { name: 'History' }).click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(nav).toBeHidden()
})
