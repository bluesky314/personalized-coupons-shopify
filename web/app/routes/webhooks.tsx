import type { ActionFunctionArgs } from "@remix-run/node";
import { authenticate } from "../shopify.server";
import { fetchCustomerScoringData } from "../services/shopify-data-fetcher";
import { scoreCustomer } from "../services/scoring-client";
import { writeScoringMetafield } from "../services/metafield-writer";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();

export const action = async ({ request }: ActionFunctionArgs) => {
  const { topic, shop, admin, payload } = await authenticate.webhook(request);

  switch (topic) {
    case "ORDERS_CREATE": {
      const customerId = payload.customer?.admin_graphql_api_id;
      if (!customerId || !admin) break;

      try {
        // 1. Fetch customer order history from Shopify
        const customerData = await fetchCustomerScoringData(admin, customerId);

        // 2. Add current order context
        customerData.order_total = parseFloat(payload.total_price || "0");
        customerData.cart_item_count = payload.line_items?.length || 0;

        // 3. Score via our Python microservice (runs LightGBM model)
        const scoringResult = await scoreCustomer(customerData);

        // 4. Write results to customer metafield
        await writeScoringMetafield(admin, customerId, scoringResult);

        console.log(`Scored customer ${customerId}: ${scoringResult.tier} (${scoringResult.score})`);
      } catch (error) {
        console.error(`Scoring failed for ${customerId}:`, error);
      }
      break;
    }

    case "CUSTOMERS_CREATE": {
      const customerId = payload.admin_graphql_api_id;
      if (!customerId || !admin) break;

      try {
        // New customer — will get FULL_COUPON (first-timer hard gate)
        const customerData = await fetchCustomerScoringData(admin, customerId);
        const scoringResult = await scoreCustomer(customerData);
        await writeScoringMetafield(admin, customerId, scoringResult);
        console.log(`Scored new customer ${customerId}: ${scoringResult.tier}`);
      } catch (error) {
        console.error(`Scoring failed for new customer ${customerId}:`, error);
      }
      break;
    }

    case "APP_UNINSTALLED":
      // Clean up app data for this shop
      console.log(`App uninstalled from ${shop}`);
      break;

    case "CUSTOMERS_DATA_REQUEST": {
      // GDPR: Customer requests their data.
      // We store scoring data in Shopify metafields (not our DB beyond logs),
      // so there is minimal data to return.
      console.log(
        `GDPR data request for customer in shop ${shop}, payload:`,
        JSON.stringify(payload),
      );
      break;
    }

    case "CUSTOMERS_REDACT": {
      // GDPR: Delete all stored data for a specific customer.
      const redactCustomerId = payload.customer?.id
        ? `gid://shopify/Customer/${payload.customer.id}`
        : null;
      if (redactCustomerId && shop) {
        const deleted = await prisma.scoringLog.deleteMany({
          where: { shop, customerId: redactCustomerId },
        });
        console.log(
          `GDPR customer redact: deleted ${deleted.count} ScoringLog records for ${redactCustomerId} in ${shop}`,
        );
      }
      break;
    }

    case "SHOP_REDACT": {
      // GDPR: Shop has been inactive for 48h after uninstall — purge everything.
      if (shop) {
        const deletedLogs = await prisma.scoringLog.deleteMany({
          where: { shop },
        });
        const deletedSettings = await prisma.shopSettings.deleteMany({
          where: { shop },
        });
        console.log(
          `GDPR shop redact for ${shop}: deleted ${deletedLogs.count} ScoringLog + ${deletedSettings.count} ShopSettings records`,
        );
      }
      break;
    }
  }

  return new Response(null, { status: 200 });
};
