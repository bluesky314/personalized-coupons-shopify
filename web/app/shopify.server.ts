import "@shopify/shopify-app-remix/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  BillingInterval,
  shopifyApp,
} from "@shopify/shopify-app-remix/server";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();

// ---------------------------------------------------------------------------
// Billing plan names — exported so other modules can reference them
// ---------------------------------------------------------------------------
export const PLAN_STARTER = "Starter";
export const PLAN_GROWTH = "Growth";
export const PLAN_PRO = "Pro";

const shopify = shopifyApp({
  apiKey: process.env.SHOPIFY_API_KEY!,
  apiSecretKey: process.env.SHOPIFY_API_SECRET!,
  apiVersion: ApiVersion.January25,
  scopes: process.env.SCOPES?.split(",") || [
    "read_customers",
    "write_customers",
    "read_orders",
    "read_products",
    "write_discounts",
    "read_discounts",
  ],
  appUrl: process.env.SHOPIFY_APP_URL || process.env.HOST || "https://localhost",
  authPathPrefix: "/auth",
  sessionStorage: new PrismaSessionStorage(prisma),
  distribution: AppDistribution.AppStore,
  billing: {
    [PLAN_STARTER]: {
      amount: 49,
      currencyCode: "USD",
      interval: BillingInterval.Every30Days,
      trialDays: 14,
    },
    [PLAN_GROWTH]: {
      amount: 199,
      currencyCode: "USD",
      interval: BillingInterval.Every30Days,
      trialDays: 14,
    },
    [PLAN_PRO]: {
      amount: 499,
      currencyCode: "USD",
      interval: BillingInterval.Every30Days,
      trialDays: 14,
    },
  },
  future: {
    unstable_newEmbeddedAuthStrategy: true,
  },
  ...(process.env.SHOP_CUSTOM_DOMAIN
    ? { customShopDomains: [process.env.SHOP_CUSTOM_DOMAIN] }
    : {}),
});

export default shopify;
export const apiVersion = ApiVersion.January25;
export const addDocumentResponseHeaders = shopify.addDocumentResponseHeaders;
export const authenticate = shopify.authenticate;
export const unauthenticated = shopify.unauthenticated;
export const login = shopify.login;
export const registerWebhooks = shopify.registerWebhooks;
export const sessionStorage = shopify.sessionStorage;
