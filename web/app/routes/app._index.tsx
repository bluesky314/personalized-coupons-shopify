import { json, type LoaderFunctionArgs } from "@remix-run/node";
import { useLoaderData } from "@remix-run/react";
import { authenticate } from "../shopify.server";
import {
  Page, Layout, Card, BlockStack, Text, InlineGrid,
  Badge, ProgressBar, Banner
} from "@shopify/polaris";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { admin } = await authenticate.admin(request);

  // Fetch aggregate stats from customer metafields
  const response = await admin.graphql(`
    query {
      customers(first: 100) {
        edges {
          node {
            id
            metafield(namespace: "personalized_coupons", key: "scoring") {
              value
            }
          }
        }
      }
    }
  `);

  const data = await response.json();
  const customers = data.data.customers.edges;

  let scored = 0;
  let suppress = 0;
  let nudge = 0;
  let fullCoupon = 0;

  for (const { node } of customers) {
    if (node.metafield?.value) {
      scored++;
      try {
        const scoring = JSON.parse(node.metafield.value);
        if (scoring.tier === "SUPPRESS") suppress++;
        else if (scoring.tier === "NUDGE") nudge++;
        else if (scoring.tier === "FULL_COUPON") fullCoupon++;
      } catch {}
    }
  }

  return json({ scored, suppress, nudge, fullCoupon, total: customers.length });
};

export default function Dashboard() {
  const { scored, suppress, nudge, fullCoupon, total } = useLoaderData<typeof loader>();

  return (
    <Page title="Personalized Coupons">
      <Layout>
        <Layout.Section>
          <Banner tone="info">
            <p>
              Your scoring engine analyzes customer purchase history and automatically
              personalizes discounts at checkout. Customers are scored after each order.
            </p>
          </Banner>
        </Layout.Section>

        <Layout.Section>
          <InlineGrid columns={4} gap="400">
            <Card>
              <BlockStack gap="200">
                <Text variant="headingSm" as="h3">Customers Scored</Text>
                <Text variant="heading2xl" as="p">{scored}</Text>
                <Text variant="bodySm" as="p" tone="subdued">of {total} total</Text>
              </BlockStack>
            </Card>

            <Card>
              <BlockStack gap="200">
                <Text variant="headingSm" as="h3">Suppress</Text>
                <Text variant="heading2xl" as="p" tone="success">{suppress}</Text>
                <Badge tone="success">No coupon needed</Badge>
              </BlockStack>
            </Card>

            <Card>
              <BlockStack gap="200">
                <Text variant="headingSm" as="h3">Nudge</Text>
                <Text variant="heading2xl" as="p" tone="warning">{nudge}</Text>
                <Badge tone="warning">Reduced coupon</Badge>
              </BlockStack>
            </Card>

            <Card>
              <BlockStack gap="200">
                <Text variant="headingSm" as="h3">Full Coupon</Text>
                <Text variant="heading2xl" as="p" tone="critical">{fullCoupon}</Text>
                <Badge tone="critical">Full discount</Badge>
              </BlockStack>
            </Card>
          </InlineGrid>
        </Layout.Section>

        {scored > 0 && (
          <Layout.Section>
            <Card>
              <BlockStack gap="400">
                <Text variant="headingSm" as="h3">Tier Distribution</Text>
                <BlockStack gap="200">
                  <Text variant="bodySm" as="p">
                    SUPPRESS ({suppress}) — {scored > 0 ? Math.round(suppress / scored * 100) : 0}%
                  </Text>
                  <ProgressBar
                    progress={scored > 0 ? (suppress / scored) * 100 : 0}
                    tone="success"
                    size="small"
                  />
                </BlockStack>
                <BlockStack gap="200">
                  <Text variant="bodySm" as="p">
                    NUDGE ({nudge}) — {scored > 0 ? Math.round(nudge / scored * 100) : 0}%
                  </Text>
                  <ProgressBar
                    progress={scored > 0 ? (nudge / scored) * 100 : 0}
                    tone="highlight"
                    size="small"
                  />
                </BlockStack>
                <BlockStack gap="200">
                  <Text variant="bodySm" as="p">
                    FULL COUPON ({fullCoupon}) — {scored > 0 ? Math.round(fullCoupon / scored * 100) : 0}%
                  </Text>
                  <ProgressBar
                    progress={scored > 0 ? (fullCoupon / scored) * 100 : 0}
                    tone="critical"
                    size="small"
                  />
                </BlockStack>
              </BlockStack>
            </Card>
          </Layout.Section>
        )}
      </Layout>
    </Page>
  );
}
