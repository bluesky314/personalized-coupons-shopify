// @ts-check

/**
 * Shopify Function: Personalized Discount
 *
 * Reads pre-computed scoring data from customer metafield and applies
 * the appropriate discount at checkout.
 *
 * Metafield schema (namespace: "personalized_coupons", key: "scoring"):
 * {
 *   "tier": "SUPPRESS" | "NUDGE" | "FULL_COUPON",
 *   "score": 0.47,
 *   "nudge_pct": 12,
 *   "full_pct": 25,
 *   "hard_gate": false,
 *   "strategies": {
 *     "aov_stretch": { "active": true, "target_rs": 2000, "coupon_pct": 12 },
 *     "highest_basket": { "active": false, "max_order_value": 4500 },
 *     "lapse_reactivation": { "active": true, "coupon_pct": 15, "expires_at": "..." },
 *     "loyalty_milestone": { "active": true, "coupon_pct": 12 },
 *     "frequency_booster": { "active": false },
 *     "category_upgrade": { "active": false }
 *   }
 * }
 */

export function run(input) {
  const NO_DISCOUNT = { discounts: [], discountApplicationStrategy: "FIRST" };

  const customer = input.cart?.buyerIdentity?.customer;
  if (!customer) return NO_DISCOUNT;

  // Read pre-computed scoring metafield
  const rawMetafield = customer.metafield?.value;
  if (!rawMetafield) return NO_DISCOUNT;

  let scoring;
  try {
    scoring = JSON.parse(rawMetafield);
  } catch {
    return NO_DISCOUNT;
  }

  const cartTotal = parseFloat(input.cart.cost.totalAmount.amount);
  const allTargets = input.cart.lines.map(line => ({ cartLine: { id: line.id } }));
  const discounts = [];

  // Read merchant settings from discount node metafield
  let settings = {};
  const settingsRaw = input.discountNode?.metafield?.value;
  if (settingsRaw) {
    try { settings = JSON.parse(settingsRaw); } catch {}
  }

  // ── Main tier discount ──────────────────────────────────────
  if (scoring.tier === "SUPPRESS") {
    // No base discount — but strategies may still fire below
  } else if (scoring.tier === "NUDGE") {
    const pct = scoring.nudge_pct || 10;
    discounts.push({
      value: { percentage: { value: String(pct) } },
      targets: allTargets,
      message: `Personalized ${pct}% off for you`
    });
  } else if (scoring.tier === "FULL_COUPON") {
    const pct = scoring.full_pct || 25;
    discounts.push({
      value: { percentage: { value: String(pct) } },
      targets: allTargets,
      message: `${pct}% off your order`
    });
  }

  // ── Strategy discounts (additive, merchant toggles which are active) ──
  const strategies = scoring.strategies || {};

  // AOV Stretch — conditional on cart total meeting target
  if (strategies.aov_stretch?.active && cartTotal >= strategies.aov_stretch.target_rs) {
    if (!settings.disabled_strategies?.includes("aov_stretch")) {
      discounts.push({
        value: { percentage: { value: String(strategies.aov_stretch.coupon_pct) } },
        targets: allTargets,
        message: `Spend target reached — ${strategies.aov_stretch.coupon_pct}% off unlocked!`
      });
    }
  }

  // Highest Basket — conditional on cart total exceeding historical max
  if (strategies.highest_basket?.max_order_value && cartTotal > strategies.highest_basket.max_order_value) {
    if (!settings.disabled_strategies?.includes("highest_basket")) {
      discounts.push({
        value: { percentage: { value: "5" } },
        targets: allTargets,
        message: "Your biggest order ever! Extra 5% off"
      });
    }
  }

  // Lapse Reactivation — time-gated
  if (strategies.lapse_reactivation?.active) {
    if (!settings.disabled_strategies?.includes("lapse_reactivation")) {
      const expires = new Date(strategies.lapse_reactivation.expires_at);
      if (new Date() < expires) {
        discounts.push({
          value: { percentage: { value: String(strategies.lapse_reactivation.coupon_pct) } },
          targets: allTargets,
          message: "Welcome back! Special discount just for you"
        });
      }
    }
  }

  // Loyalty Milestone — pre-qualified by server
  if (strategies.loyalty_milestone?.active) {
    if (!settings.disabled_strategies?.includes("loyalty_milestone")) {
      discounts.push({
        value: { percentage: { value: String(strategies.loyalty_milestone.coupon_pct) } },
        targets: allTargets,
        message: `Loyalty reward — ${strategies.loyalty_milestone.coupon_pct}% off`
      });
    }
  }

  // Frequency Booster — pre-qualified by server
  if (strategies.frequency_booster?.active) {
    if (!settings.disabled_strategies?.includes("frequency_booster")) {
      discounts.push({
        value: { percentage: { value: String(strategies.frequency_booster.coupon_pct) } },
        targets: allTargets,
        message: "Back on track — here's a little extra off"
      });
    }
  }

  // Category Upgrade — pre-qualified by server
  // NOTE: This ideally should only apply to items in new categories.
  // Shopify Functions can filter targets by product/collection, but we'd need
  // the "excluded category" info in the metafield. For v1, apply to all items.
  if (strategies.category_upgrade?.active) {
    if (!settings.disabled_strategies?.includes("category_upgrade")) {
      discounts.push({
        value: { percentage: { value: String(strategies.category_upgrade.coupon_pct) } },
        targets: allTargets,
        message: "Try something new — special category discount"
      });
    }
  }

  if (discounts.length === 0) return NO_DISCOUNT;

  return {
    discountApplicationStrategy: "MAXIMUM",
    discounts
  };
}
