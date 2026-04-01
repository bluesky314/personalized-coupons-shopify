import { json, type LoaderFunctionArgs } from "@remix-run/node";
import { Outlet, useLoaderData } from "@remix-run/react";
import { AppProvider } from "@shopify/shopify-app-remix/react";
import polarisStyles from "@shopify/polaris/build/esm/styles.css?url";
import type { LinksFunction } from "@remix-run/node";

export const links: LinksFunction = () => [
  { rel: "stylesheet", href: polarisStyles },
];

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const apiKey = process.env.SHOPIFY_API_KEY || "";
  return json({ apiKey });
};

export default function App() {
  const { apiKey } = useLoaderData<typeof loader>();

  return (
    <AppProvider isEmbeddedApp apiKey={apiKey}>
      <Outlet />
    </AppProvider>
  );
}
