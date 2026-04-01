import { json, type ActionFunctionArgs, type LoaderFunctionArgs } from "@remix-run/node";
import { useLoaderData, useSubmit } from "@remix-run/react";
import { authenticate } from "../shopify.server";
import {
  Page, Layout, Card, BlockStack, Text, TextField,
  Checkbox, Button, Banner, InlineStack
} from "@shopify/polaris";
import { useState, useCallback } from "react";

const STRATEGIES = [
  { key: "aov_stretch", label: "AOV Stretch", description: "Coupon that activates when basket exceeds a spend target above their historical AOV" },
  { key: "lapse_reactivation", label: "Come Back Offer", description: "Time-gated coupon for users inactive 90+ days" },
  { key: "category_upgrade", label: "Category Upgrade", description: "Discount on a new category for concentrated buyers" },
  { key: "loyalty_milestone", label: "Loyalty Milestone", description: "Reward at order count milestones (5, 10, 20, 40, 75, 150)" },
  { key: "frequency_booster", label: "Frequency Booster", description: "Nudge when purchase gap exceeds 1.5x their average" },
  { key: "highest_basket", label: "Highest Basket", description: "Extra 5% when current basket exceeds all-time max" },
];

const DEFAULT_SETTINGS = {
  suppress_threshold: 0.65,
  nudge_threshold: 0.384,
  full_coupon_pct: 25,
  enabled_strategies: STRATEGIES.map(s => s.key),
};

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { admin } = await authenticate.admin(request);

  // Read settings from shop metafield
  const response = await admin.graphql(`
    query {
      shop {
        metafield(namespace: "personalized_coupons", key: "settings") {
          value
        }
      }
    }
  `);

  const data = await response.json();
  const raw = data.data.shop.metafield?.value;
  const settings = raw ? JSON.parse(raw) : DEFAULT_SETTINGS;

  return json({ settings });
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { admin } = await authenticate.admin(request);
  const formData = await request.formData();
  const settings = JSON.parse(formData.get("settings") as string);

  await admin.graphql(`
    mutation SetSettings($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields { id }
        userErrors { field message }
      }
    }
  `, {
    variables: {
      metafields: [{
        ownerId: "gid://shopify/Shop/current",
        namespace: "personalized_coupons",
        key: "settings",
        type: "json",
        value: JSON.stringify(settings),
      }],
    },
  });

  return json({ success: true });
};

export default function Settings() {
  const { settings } = useLoaderData<typeof loader>();
  const submit = useSubmit();
  const [localSettings, setLocalSettings] = useState(settings);

  const handleStrategyToggle = useCallback((key: string) => {
    setLocalSettings((prev: any) => {
      const enabled = prev.enabled_strategies || [];
      const updated = enabled.includes(key)
        ? enabled.filter((k: string) => k !== key)
        : [...enabled, key];
      return { ...prev, enabled_strategies: updated };
    });
  }, []);

  const handleSave = useCallback(() => {
    const formData = new FormData();
    formData.set("settings", JSON.stringify(localSettings));
    submit(formData, { method: "post" });
  }, [localSettings, submit]);

  return (
    <Page title="Settings" primaryAction={{ content: "Save", onAction: handleSave }}>
      <Layout>
        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text variant="headingSm" as="h3">Tier Thresholds</Text>
              <InlineStack gap="400">
                <TextField
                  label="Suppress threshold"
                  type="number"
                  value={String(localSettings.suppress_threshold)}
                  onChange={(v) => setLocalSettings({ ...localSettings, suppress_threshold: parseFloat(v) })}
                  helpText="Score above this = no coupon (default 0.65)"
                  autoComplete="off"
                />
                <TextField
                  label="Nudge threshold"
                  type="number"
                  value={String(localSettings.nudge_threshold)}
                  onChange={(v) => setLocalSettings({ ...localSettings, nudge_threshold: parseFloat(v) })}
                  helpText="Score between nudge and suppress = reduced coupon"
                  autoComplete="off"
                />
                <TextField
                  label="Full coupon %"
                  type="number"
                  value={String(localSettings.full_coupon_pct)}
                  onChange={(v) => setLocalSettings({ ...localSettings, full_coupon_pct: parseInt(v) })}
                  helpText="Discount % for full coupon tier"
                  autoComplete="off"
                />
              </InlineStack>
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <Card>
            <BlockStack gap="400">
              <Text variant="headingSm" as="h3">Personalized Strategies</Text>
              <Text variant="bodySm" as="p" tone="subdued">
                Enable or disable individual strategies. Disabled strategies will not fire even if the customer qualifies.
              </Text>
              {STRATEGIES.map(s => (
                <Checkbox
                  key={s.key}
                  label={s.label}
                  helpText={s.description}
                  checked={(localSettings.enabled_strategies || []).includes(s.key)}
                  onChange={() => handleStrategyToggle(s.key)}
                />
              ))}
            </BlockStack>
          </Card>
        </Layout.Section>
      </Layout>
    </Page>
  );
}
