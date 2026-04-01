// Fetches customer data from Shopify Admin GraphQL API
// and transforms it to match our scoring engine's expected input format.

import { AdminApiContext } from "@shopify/shopify-app-remix/server";

export interface CustomerScoringInput {
  user_id: string;
  total_prior_orders: number;
  prior_orders_with_coupon: number;
  prior_orders_without_coupon: number;
  prior_coupon_use_rate: number;
  eligible_coupon_use_rate: number;
  prior_eligible_orders: number;
  avg_coupon_value_used: number;
  gross_lifetime_value: number;
  average_order_value: number;
  days_since_first_order: number;
  days_since_last_order: number;
  prior_cod_rate: number;
  is_first_order: number;
  max_order_value: number | null;
  avg_order_gap_days: number | null;
  top_category: string | null;
  category_concentration: number | null;
  // These can be set from current cart context
  order_total?: number;
  mrp_total?: number;
  cart_item_count?: number;
  is_cod?: number;
  company_id?: number;
  sales_channel_id?: string;
  delivery_pincode?: string;
  delivery_city?: string;
  delivery_state?: string;
}

const CUSTOMER_ORDERS_QUERY = `
  query CustomerOrders($customerId: ID!, $first: Int!) {
    customer(id: $customerId) {
      id
      numberOfOrders
      amountSpent { amount currencyCode }
      createdAt
      tags
      orders(first: $first, sortKey: CREATED_AT, reverse: true) {
        edges {
          node {
            id
            name
            createdAt
            totalPriceSet { shopMoney { amount } }
            subtotalPriceSet { shopMoney { amount } }
            totalDiscounts { amount }
            discountCodes
            lineItems(first: 50) {
              edges {
                node {
                  title
                  quantity
                  variant { id price }
                  product { id productType }
                  originalTotalSet { shopMoney { amount } }
                  discountedTotalSet { shopMoney { amount } }
                }
              }
            }
          }
        }
      }
    }
  }
`;

/** Minimum order value (in shop currency) to count as "coupon-eligible". */
const ELIGIBLE_ORDER_THRESHOLD = 1500;

function buildEmptyInput(customerId: string): CustomerScoringInput {
  return {
    user_id: customerId,
    total_prior_orders: 0,
    prior_orders_with_coupon: 0,
    prior_orders_without_coupon: 0,
    prior_coupon_use_rate: 0,
    eligible_coupon_use_rate: 0,
    prior_eligible_orders: 0,
    avg_coupon_value_used: 0,
    gross_lifetime_value: 0,
    average_order_value: 0,
    days_since_first_order: 0,
    days_since_last_order: 0,
    prior_cod_rate: 0,
    is_first_order: 1,
    max_order_value: null,
    avg_order_gap_days: null,
    top_category: null,
    category_concentration: null,
  };
}

export async function fetchCustomerScoringData(
  admin: AdminApiContext,
  customerId: string,
): Promise<CustomerScoringInput> {
  const response = await admin.graphql(CUSTOMER_ORDERS_QUERY, {
    variables: { customerId, first: 250 },
  });
  const data = await response.json();
  const customer = data.data.customer;

  if (!customer || customer.numberOfOrders === 0) {
    return buildEmptyInput(customerId);
  }

  const orders = customer.orders.edges.map((e: any) => e.node);
  const totalOrders = orders.length;

  // Coupon analysis
  let ordersWithCoupon = 0;
  let totalCouponDiscount = 0;
  let eligibleOrders = 0;
  let eligibleWithCoupon = 0;
  let maxOrderValue = 0;
  const orderDates: Date[] = [];
  const categoryCount: Record<string, number> = {};

  for (const order of orders) {
    const orderTotal = parseFloat(order.totalPriceSet.shopMoney.amount);
    const totalDiscount = parseFloat(order.totalDiscounts.amount);
    const hasCoupon = order.discountCodes.length > 0 || totalDiscount > 0;

    if (hasCoupon) {
      ordersWithCoupon++;
      totalCouponDiscount += totalDiscount;
    }

    // Eligible orders (order total >= threshold for coupon eligibility)
    if (orderTotal >= ELIGIBLE_ORDER_THRESHOLD) {
      eligibleOrders++;
      if (hasCoupon) eligibleWithCoupon++;
    }

    if (orderTotal > maxOrderValue) maxOrderValue = orderTotal;
    orderDates.push(new Date(order.createdAt));

    // Category tracking from line items
    for (const lineEdge of order.lineItems.edges) {
      const productType = lineEdge.node.product?.productType;
      if (productType) {
        categoryCount[productType] = (categoryCount[productType] || 0) + 1;
      }
    }
  }

  // Sort dates ascending for gap calculations
  orderDates.sort((a, b) => a.getTime() - b.getTime());

  const now = new Date();
  const daysSinceFirst = Math.round(
    (now.getTime() - orderDates[0].getTime()) / 86400000,
  );
  const daysSinceLast = Math.round(
    (now.getTime() - orderDates[orderDates.length - 1].getTime()) / 86400000,
  );

  // Average gap between orders
  let avgGap: number | null = null;
  if (orderDates.length >= 2) {
    const totalSpanDays =
      (orderDates[orderDates.length - 1].getTime() -
        orderDates[0].getTime()) /
      86400000;
    avgGap = totalSpanDays / (orderDates.length - 1);
  }

  // Category concentration
  let topCategory: string | null = null;
  let categoryConcentration: number | null = null;
  const totalCategoryEntries = Object.values(categoryCount).reduce(
    (a, b) => a + b,
    0,
  );
  if (totalCategoryEntries > 0) {
    const sorted = Object.entries(categoryCount).sort(
      (a, b) => b[1] - a[1],
    );
    topCategory = sorted[0][0];
    categoryConcentration = sorted[0][1] / totalCategoryEntries;
  }

  const glv = parseFloat(customer.amountSpent.amount);
  const couponRate = totalOrders > 0 ? ordersWithCoupon / totalOrders : 0;
  const eligibleRate =
    eligibleOrders > 0 ? eligibleWithCoupon / eligibleOrders : 0;
  const avgCouponVal =
    ordersWithCoupon > 0 ? totalCouponDiscount / ordersWithCoupon : 0;

  return {
    user_id: customerId,
    total_prior_orders: totalOrders,
    prior_orders_with_coupon: ordersWithCoupon,
    prior_orders_without_coupon: totalOrders - ordersWithCoupon,
    prior_coupon_use_rate: Math.round(couponRate * 1000) / 1000,
    eligible_coupon_use_rate: Math.round(eligibleRate * 1000) / 1000,
    prior_eligible_orders: eligibleOrders,
    avg_coupon_value_used: Math.round(avgCouponVal * 100) / 100,
    gross_lifetime_value: glv,
    average_order_value:
      totalOrders > 0 ? Math.round((glv / totalOrders) * 100) / 100 : 0,
    days_since_first_order: daysSinceFirst,
    days_since_last_order: daysSinceLast,
    prior_cod_rate: 0, // Shopify doesn't expose COD in the same way
    is_first_order: totalOrders === 0 ? 1 : 0,
    max_order_value: maxOrderValue,
    avg_order_gap_days: avgGap ? Math.round(avgGap * 10) / 10 : null,
    top_category: topCategory,
    category_concentration: categoryConcentration
      ? Math.round(categoryConcentration * 1000) / 1000
      : null,
  };
}
