import { authenticate } from "~/shopify.server";
import { PLAN_STARTER, PLAN_GROWTH, PLAN_PRO } from "~/shopify.server";

// ---------------------------------------------------------------------------
// Plan definitions with revenue limits for usage-based billing
// ---------------------------------------------------------------------------
export const PLANS = {
  STARTER: {
    name: PLAN_STARTER,
    amount: 49,
    includedRevenue: 5000,
    overageRate: 0.015, // 1.5%
  },
  GROWTH: {
    name: PLAN_GROWTH,
    amount: 199,
    includedRevenue: 25000,
    overageRate: 0.01, // 1.0%
  },
  PRO: {
    name: PLAN_PRO,
    amount: 499,
    includedRevenue: 100000,
    overageRate: 0.007, // 0.7%
  },
} as const;

export type PlanKey = keyof typeof PLANS;
export type PlanName = (typeof PLANS)[PlanKey]["name"];

const ALL_PLAN_NAMES = Object.values(PLANS).map((p) => p.name);

// ---------------------------------------------------------------------------
// Check whether the merchant has an active subscription on any plan
// ---------------------------------------------------------------------------
export async function hasActiveSubscription(
  request: Request,
): Promise<boolean> {
  const { billing } = await authenticate.admin(request);

  for (const planName of ALL_PLAN_NAMES) {
    const { hasActivePayment } = await billing.check({ plans: [planName] });
    if (hasActivePayment) return true;
  }

  return false;
}

// ---------------------------------------------------------------------------
// Return the current plan name (or null if none active)
// ---------------------------------------------------------------------------
export async function getCurrentPlan(
  request: Request,
): Promise<PlanName | null> {
  const { billing } = await authenticate.admin(request);

  for (const planName of ALL_PLAN_NAMES) {
    const { hasActivePayment } = await billing.check({ plans: [planName] });
    if (hasActivePayment) return planName;
  }

  return null;
}

// ---------------------------------------------------------------------------
// Get the full plan config for the current subscription
// ---------------------------------------------------------------------------
export async function getCurrentPlanConfig(request: Request) {
  const currentName = await getCurrentPlan(request);
  if (!currentName) return null;

  return (
    Object.values(PLANS).find((p) => p.name === currentName) ?? null
  );
}

// ---------------------------------------------------------------------------
// Redirect to billing if no active subscription.
// Call from a loader — it throws a Response redirect when billing is needed.
// Pass `plan` to specify which plan to subscribe to (defaults to Starter).
// ---------------------------------------------------------------------------
export async function requireSubscription(
  request: Request,
  plan: PlanName = PLAN_STARTER,
) {
  const { billing } = await authenticate.admin(request);

  const { hasActivePayment } = await billing.check({
    plans: ALL_PLAN_NAMES,
  });

  if (!hasActivePayment) {
    // This throws a redirect Response to the Shopify billing page
    await billing.request({
      plan,
      isTest: process.env.NODE_ENV !== "production",
    });
  }
}
