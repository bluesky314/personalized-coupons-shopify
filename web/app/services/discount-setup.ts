// Sets up the automatic discount linked to our Shopify Function,
// and registers metafield definitions so they appear in the Shopify admin.

import { AdminApiContext } from "@shopify/shopify-app-remix/server";

const METAFIELD_NAMESPACE = "personalized_coupons";
const METAFIELD_KEY = "scoring";

// ---------------------------------------------------------------------------
// 1. Create the automatic discount that invokes our Shopify Function
// ---------------------------------------------------------------------------

export async function createAutomaticDiscount(
  admin: AdminApiContext,
): Promise<string> {
  // Find our deployed discount function
  const functionsResponse = await admin.graphql(`
    query {
      shopifyFunctions(first: 25) {
        nodes {
          apiType
          title
          id
        }
      }
    }
  `);

  const functions = await functionsResponse.json();
  const discountFunction = functions.data.shopifyFunctions.nodes.find(
    (f: any) => f.apiType === "discount" && f.title.includes("personalized"),
  );

  if (!discountFunction) {
    throw new Error(
      "Discount function not found — make sure the extension is deployed",
    );
  }

  // Create the automatic app discount
  const createResponse = await admin.graphql(
    `
    mutation CreateDiscount($input: DiscountAutomaticAppInput!) {
      discountAutomaticAppCreate(automaticAppDiscount: $input) {
        automaticAppDiscount {
          discountId
          title
          status
        }
        userErrors {
          field
          message
        }
      }
    }
  `,
    {
      variables: {
        input: {
          title: "Personalized Coupon",
          functionId: discountFunction.id,
          startsAt: new Date().toISOString(),
          combinesWith: {
            orderDiscounts: false,
            productDiscounts: true,
            shippingDiscounts: true,
          },
        },
      },
    },
  );

  const result = await createResponse.json();
  const userErrors =
    result.data.discountAutomaticAppCreate.userErrors;

  if (userErrors.length > 0) {
    throw new Error(
      `Failed to create automatic discount: ${JSON.stringify(userErrors)}`,
    );
  }

  const discountId =
    result.data.discountAutomaticAppCreate.automaticAppDiscount.discountId;
  console.log(`Created automatic discount: ${discountId}`);
  return discountId;
}

// ---------------------------------------------------------------------------
// 2. Register metafield definitions so they show in the Shopify admin UI
// ---------------------------------------------------------------------------

const METAFIELD_DEFINITION_MUTATION = `
  mutation CreateMetafieldDefinition($definition: MetafieldDefinitionInput!) {
    metafieldDefinitionCreate(definition: $definition) {
      createdDefinition {
        id
        name
      }
      userErrors {
        field
        message
      }
    }
  }
`;

export async function registerMetafieldDefinitions(
  admin: AdminApiContext,
): Promise<void> {
  const definitions = [
    {
      name: "Personalized Coupon Scoring",
      namespace: METAFIELD_NAMESPACE,
      key: METAFIELD_KEY,
      type: "json",
      ownerType: "CUSTOMER",
      description:
        "AI-generated scoring data used to personalize discounts at checkout. Contains tier, score, archetype, and per-strategy config.",
      pin: true,
    },
  ];

  for (const def of definitions) {
    try {
      const response = await admin.graphql(METAFIELD_DEFINITION_MUTATION, {
        variables: { definition: def },
      });
      const result = await response.json();
      const errors =
        result.data.metafieldDefinitionCreate.userErrors;

      if (errors.length > 0) {
        // "already exists" is fine — skip silently
        const alreadyExists = errors.some((e: any) =>
          e.message.toLowerCase().includes("already exists"),
        );
        if (!alreadyExists) {
          console.error(
            `Metafield definition error for ${def.key}:`,
            errors,
          );
        }
      } else {
        console.log(
          `Registered metafield definition: ${result.data.metafieldDefinitionCreate.createdDefinition?.name}`,
        );
      }
    } catch (error) {
      console.error(
        `Failed to register metafield definition ${def.key}:`,
        error,
      );
    }
  }
}
