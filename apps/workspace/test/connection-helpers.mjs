// Public connection actions live in the row's overflow menu.
export const connectionRow = name => `.connection-row:has(button[aria-label=${JSON.stringify(`Actions for ${name}`)}])`;
export async function connectionAction(page, name, action) {
  await page.locator(`button[aria-label=${JSON.stringify(`Actions for ${name}`)}]`).click();
  const label = action === 'Forget password' ? `Forget the saved password of ${name}`
    : action === 'Delete' ? `Delete ${name}…` : `${action} ${name}`;
  await page.locator(`[role="menu"] button[aria-label=${JSON.stringify(label)}]`).click();
}
