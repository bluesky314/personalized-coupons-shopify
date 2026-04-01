// Writes pre-computed scoring data to customer metafields
// so the Shopify Function can read them at checkout time.

import { AdminApiContext } from "@shopify/shopify-app-remix/server";

const METAFIELD_NAMESPACE = "personalized_coupons";
const METAFIELD_KEY = "scoring";

interface StrategyMetafield {
  active: boolean;
  coupon_pct?: number;
  target_rs?: number;
  max_order_value?: number;
  expires_at?: string;
  message?: string;
}

interface ScoringMetafield {
  tier: string;
  score: number;
  nudge_pct: number;
  full_pct: number;
  hard_gate: boolean;
  archetype: string;
  scored_at: string;
  strategies: Record<string, StrategyMetafield>;
}

/** All strategy keys the Shopify Function expects to exist. */
const ALL_STRATEGY_KEYS = [
  "aov_stretch",
  "lapse_reactivation",
  "category_upgrade",
  "loyalty_milestone",
  "frequency_booster",
  "highest_basket",
] as const;

const SET_SCORING_METAFIELD_MUTATION = `
  mutation SetScoringMetafield($input: CustomerInput!) {
    customerUpdate(input: $input) {
      customer { id }
      userErrors { field message }
    }
  }
`;

export async function writeScoringMetafield(
  admin: AdminApiContext,
  customerId: string,
  scoringResult: any,
): Promise<void> {
  // Transform scoring result into metafield format
  const strategies: Record<string, StrategyMetafield> = {};

  for (const s of scoringResult.personalized_strategies || []) {
    const key = s.strategy.toLowerCase();
    strategies[key] = {
      active: true,
      coupon_pct: s.coupon_pct
        ? Math.round(s.coupon_pct * 100)
        : undefined,
      target_rs: s.min_basket_rs || undefined,
      max_order_value: undefined,
      expires_at: s.expiry_hours
        ? new Date(Date.now() + s.expiry_hours * 3600000).toISOString()
        : undefined,
      message: s.message,
    };
  }

  // Always include highest_basket max even if not active
  // (the Shopify Function needs it for comparison)
  if (!strategies.highest_basket) {
    strategies.highest_basket = {
      active: false,
      max_order_value:
        scoringResult.fetched_user?.max_order_value || undefined,
    };
  }

  // Ensure all expected strategy keys exist in the object
  for (const key of ALL_STRATEGY_KEYS) {
    if (!strategies[key]) {
      strategies[key] = { active: false };
    }
  }

  const nudgePct = scoringResult.recommended_coupon_pct
    ? Math.round(scoringResult.recommended_coupon_pct * 100)
    : 10;

  const metafieldValue: ScoringMetafield = {
    tier: scoringResult.tier,
    score:
      scoringResult.deal_agnostic_score ||
      scoringResult.user_score ||
      0,
    nudge_pct: scoringResult.tier === "NUDGE" ? nudgePct : 0,
    full_pct: 25,
    hard_gate: scoringResult.hard_gate_applied || false,
    archetype: scoringResult.archetype || "UNKNOWN",
    scored_at: new Date().toISOString(),
    strategies,
  };

  const response = await admin.graphql(SET_SCORING_METAFIELD_MUTATION, {
    variables: {
      input: {
        id: customerId,
        metafields: [
          {
            namespace: METAFIELD_NAMESPACE,
            key: METAFIELD_KEY,
            type: "json",
            value: JSON.stringify(metafieldValue),
          },
        ],
      },
    },
  });

  const result = await response.json();
  const errors = result.data?.customerUpdate?.userErrors;
  if (errors && errors.length > 0) {
    throw new Error(`Metafield write failed: ${JSON.stringify(errors)}`);
  }
}
