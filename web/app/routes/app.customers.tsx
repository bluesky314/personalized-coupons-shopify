import { json, type LoaderFunctionArgs } from "@remix-run/node";
import { useLoaderData } from "@remix-run/react";
import { authenticate } from "../shopify.server";
import {
  Page, Layout, Card, DataTable, Badge, Text
} from "@shopify/polaris";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { admin } = await authenticate.admin(request);

  const response = await admin.graphql(`
    query {
      customers(first: 50, sortKey: UPDATED_AT, reverse: true) {
        edges {
          node {
            id
            displayName
            email
            numberOfOrders
            amountSpent { amount }
            metafield(namespace: "personalized_coupons", key: "scoring") {
              value
            }
          }
        }
      }
    }
  `);

  const data = await response.json();
  const customers = data.data.customers.edges.map(({ node }: any) => {
    let tier = "—";
    let score = "—";
    let scoredAt = "—";

    if (node.metafield?.value) {
      try {
        const scoring = JSON.parse(node.metafield.value);
        tier = scoring.tier;
        score = scoring.score?.toFixed(3) || "—";
        scoredAt = scoring.scored_at ? new Date(scoring.scored_at).toLocaleDateString() : "—";
      } catch {}
    }

    return {
      id: node.id,
      name: node.displayName || "—",
      email: node.email || "—",
      orders: node.numberOfOrders,
      spent: `Rs. ${Math.round(parseFloat(node.amountSpent.amount))}`,
      tier,
      score,
      scoredAt,
    };
  });

  return json({ customers });
};

function tierBadge(tier: string) {
  switch (tier) {
    case "SUPPRESS": return <Badge tone="success">Suppress</Badge>;
    case "NUDGE": return <Badge tone="warning">Nudge</Badge>;
    case "FULL_COUPON": return <Badge tone="critical">Full Coupon</Badge>;
    default: return <Badge>Not scored</Badge>;
  }
}

export default function Customers() {
  const { customers } = useLoaderData<typeof loader>();

  const rows = customers.map((c: any) => [
    c.name,
    c.email,
    c.orders,
    c.spent,
    tierBadge(c.tier),
    c.score,
    c.scoredAt,
  ]);

  return (
    <Page title="Customer Scores">
      <Layout>
        <Layout.Section>
          <Card>
            <DataTable
              columnContentTypes={["text", "text", "numeric", "text", "text", "text", "text"]}
              headings={["Name", "Email", "Orders", "Spent", "Tier", "Score", "Scored"]}
              rows={rows}
            />
          </Card>
        </Layout.Section>
      </Layout>
    </Page>
  );
}
