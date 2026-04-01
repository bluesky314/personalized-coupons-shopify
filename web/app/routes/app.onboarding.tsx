import { json, type ActionFunctionArgs, type LoaderFunctionArgs } from "@remix-run/node";
import { useActionData, useLoaderData, useSubmit, useNavigation } from "@remix-run/react";
import { authenticate } from "../shopify.server";
import { fetchCustomerScoringData } from "../services/shopify-data-fetcher";
import { scoreCustomer } from "../services/scoring-client";
import { writeScoringMetafield } from "../services/metafield-writer";
import { runAfterInstall } from "../services/after-install";
import {
  Page,
  Layout,
  Card,
  BlockStack,
  Text,
  Banner,
  Button,
  ProgressBar,
  InlineStack,
  Box,
  List,
} from "@shopify/polaris";

// ---------------------------------------------------------------------------
// Loader — check if setup has already been done
// ---------------------------------------------------------------------------

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { admin } = await authenticate.admin(request);

  // Quick check: count customers so we can show total in UI
  const response = await admin.graphql(`
    query {
      customersCount {
        count
      }
    }
  `);
  const data = await response.json();
  const customerCount = data.data.customersCount?.count ?? 0;

  return json({ customerCount });
};

// ---------------------------------------------------------------------------
// Action — run after-install setup + bulk-score all customers
// ---------------------------------------------------------------------------

export const action = async ({ request }: ActionFunctionArgs) => {
  const { admin, session } = await authenticate.admin(request);
  const shop = session.shop;

  try {
    // Step 1: Run after-install setup (metafield definitions + automatic discount)
    await runAfterInstall(admin, shop);
  } catch (error: any) {
    console.error("After-install setup error:", error);
    return json(
      { error: `Setup failed: ${error.message}`, scored: 0, total: 0 },
      { status: 500 },
    );
  }

  // Step 2: Bulk-score all existing customers
  let cursor: string | null = null;
  let scored = 0;
  let total = 0;
  let errors: string[] = [];

  do {
    const response = await admin.graphql(
      `
      query($cursor: String) {
        customers(first: 50, after: $cursor) {
          edges {
            node {
              id
              numberOfOrders
              amountSpent { amount }
            }
            cursor
          }
          pageInfo {
            hasNextPage
          }
        }
      }
    `,
      { variables: { cursor } },
    );

    const data = await response.json();
    const edges = data.data.customers.edges;
    const hasNextPage = data.data.customers.pageInfo.hasNextPage;

    for (const { node } of edges) {
      try {
        const customerData = await fetchCustomerScoringData(admin, node.id);
        const result = await scoreCustomer(customerData);
        await writeScoringMetafield(admin, node.id, result);
        scored++;
      } catch (err: any) {
        errors.push(`${node.id}: ${err.message}`);
        console.error(`Bulk scoring failed for ${node.id}:`, err.message);
      }
    }

    total += edges.length;
    cursor = hasNextPage && edges.length > 0
      ? edges[edges.length - 1].cursor
      : null;
  } while (cursor);

  return json({
    scored,
    total,
    errors: errors.length > 0 ? errors.slice(0, 10) : undefined,
    error: null,
  });
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Onboarding() {
  const { customerCount } = useLoaderData<typeof loader>();
  const actionData = useActionData<typeof action>();
  const submit = useSubmit();
  const navigation = useNavigation();

  const isScoring = navigation.state === "submitting";
  const isDone = actionData && !actionData.error && actionData.scored !== undefined;
  const hasError = actionData?.error;

  const handleStartScoring = () => {
    submit({}, { method: "post" });
  };

  return (
    <Page title="Welcome to Personalized Coupons" narrowWidth>
      <Layout>
        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text variant="headingLg" as="h2">
                Set up your personalized discount engine
              </Text>
              <Text as="p" variant="bodyMd">
                This app uses machine learning to analyze each customer's purchase
                history and automatically decide the right discount at checkout:
              </Text>
              <List type="bullet">
                <List.Item>
                  <Text as="span" fontWeight="semibold">Suppress</Text> — loyal
                  customers who buy without discounts get no coupon (saving you margin)
                </List.Item>
                <List.Item>
                  <Text as="span" fontWeight="semibold">Nudge</Text> — price-sensitive
                  customers get a small, personalized discount to convert
                </List.Item>
                <List.Item>
                  <Text as="span" fontWeight="semibold">Full Coupon</Text> — new or
                  at-risk customers get a full discount to drive the sale
                </List.Item>
              </List>
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text variant="headingMd" as="h3">
                Initial Setup
              </Text>
              <Text as="p" variant="bodyMd">
                Clicking the button below will:
              </Text>
              <List type="number">
                <List.Item>Register metafield definitions in your store</List.Item>
                <List.Item>Create the automatic discount linked to our scoring engine</List.Item>
                <List.Item>
                  Score all {customerCount} existing customers (this may take a few
                  minutes for large stores)
                </List.Item>
              </List>

              {hasError && (
                <Banner tone="critical">
                  <p>{actionData.error}</p>
                </Banner>
              )}

              {isScoring && (
                <BlockStack gap="200">
                  <Text as="p" variant="bodySm" tone="subdued">
                    Scoring customers... this may take a while for large stores.
                  </Text>
                  <ProgressBar progress={75} tone="highlight" size="small" />
                </BlockStack>
              )}

              {isDone && (
                <Banner tone="success">
                  <p>
                    Setup complete! Scored {actionData.scored} of {actionData.total} customers.
                    {actionData.errors && actionData.errors.length > 0 && (
                      <> ({actionData.errors.length} errors — check server logs for details.)</>
                    )}
                  </p>
                </Banner>
              )}

              <InlineStack gap="300" align="start">
                {!isDone && (
                  <Button
                    variant="primary"
                    onClick={handleStartScoring}
                    loading={isScoring}
                    disabled={isScoring}
                  >
                    Start Scoring
                  </Button>
                )}
                {isDone && (
                  <Button variant="primary" url="/app">
                    Go to Dashboard
                  </Button>
                )}
              </InlineStack>
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <Box paddingBlockEnd="800">
            <Text as="p" variant="bodySm" tone="subdued">
              After setup, new customers and new orders are scored automatically via
              webhooks. You can adjust thresholds and strategies in Settings.
            </Text>
          </Box>
        </Layout.Section>
      </Layout>
    </Page>
  );
}
