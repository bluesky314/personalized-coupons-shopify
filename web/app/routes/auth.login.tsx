import type { ActionFunctionArgs, LoaderFunctionArgs } from "@remix-run/node";
import { json } from "@remix-run/node";
import { Form, useLoaderData } from "@remix-run/react";
import { login } from "../shopify.server";
import { Page, Card, BlockStack, TextField, Button, Text } from "@shopify/polaris";
import { useState } from "react";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const errors = login(request);
  return json({ errors, polarisTranslations: require("@shopify/polaris/locales/en.json") });
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const errors = await login(request);
  return json({ errors });
};

export default function Auth() {
  const { errors } = useLoaderData<typeof loader>();
  const [shop, setShop] = useState("");

  return (
    <Page>
      <Card>
        <Form method="post">
          <BlockStack gap="400">
            <Text variant="headingMd" as="h2">Log in</Text>
            <TextField
              type="text"
              name="shop"
              label="Shop domain"
              helpText="e.g. my-shop.myshopify.com"
              value={shop}
              onChange={setShop}
              autoComplete="on"
              error={errors?.shop}
            />
            <Button submit variant="primary">Log in</Button>
          </BlockStack>
        </Form>
      </Card>
    </Page>
  );
}
