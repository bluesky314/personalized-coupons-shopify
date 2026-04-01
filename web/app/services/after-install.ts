// Runs one-time setup tasks after the app is installed on a shop.

import { AdminApiContext } from "@shopify/shopify-app-remix/server";
import { registerMetafieldDefinitions, createAutomaticDiscount } from "./discount-setup";

export async function runAfterInstall(
  admin: AdminApiContext,
  shop: string,
): Promise<{ discountId: string }> {
  console.log(`Running after-install setup for ${shop}...`);

  // 1. Register metafield definitions so they appear in Shopify admin
  await registerMetafieldDefinitions(admin);

  // 2. Create the automatic discount linked to our Shopify Function
  const discountId = await createAutomaticDiscount(admin);

  // 3. Default shop settings are handled by Prisma upsert in the settings route
  //    (ShopSettings has sensible defaults in the schema)

  console.log(`After-install setup complete for ${shop}`);
  return { discountId };
}
