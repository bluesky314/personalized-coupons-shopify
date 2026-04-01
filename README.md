# Personalized Coupons — Shopify App

A public Shopify App Store app that uses a LightGBM model to personalize discounts at checkout. Merchants install it, it scores all their customers, and the right discount is automatically applied — suppressing coupons for loyal buyers, nudging borderline users, and offering full discounts to deal-seekers.

**Pricing:** $49 / $199 / $499 per month (50% below Promi AI, the main competitor at $99 / $399 / $999).

## Architecture

```
Merchant installs app
        │
        ▼
Onboarding: bulk-scores all existing customers
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│                      OUR SERVER                           │
│                                                           │
│  Webhooks (orders/create, customers/create)               │
│       │                                                   │
│       ▼                                                   │
│  Fetch customer history via Shopify Admin API             │
│       │                                                   │
│       ▼                                                   │
│  Scoring Service (Python/FastAPI, port 8081)              │
│  - LightGBM model (predict_full)                          │
│  - Evidence confidence weighting                          │
│  - 6 personalized strategies                              │
│       │                                                   │
│       ▼                                                   │
│  Write results → Customer Metafield                       │
│  (personalized_coupons.scoring = JSON)                    │
└───────────────────────────┬───────────────────────────────┘
                            │
                 ┌──────────▼──────────┐
                 │  SHOPIFY FUNCTION   │
                 │  (Wasm, ~2ms)       │
                 │                     │
                 │  Reads metafield →  │
                 │  SUPPRESS: no disc  │
                 │  NUDGE: 7-20% off   │
                 │  FULL: 25% off      │
                 │  + strategy bonuses  │
                 └──────────┬──────────┘
                            │
                 ┌──────────▼──────────┐
                 │  STOREFRONT         │
                 │  Theme App Ext:     │
                 │  - Offer banners    │
                 │  - Price strike-    │
                 │    throughs         │
                 └─────────────────────┘
```

Four components:

- **Scoring Service** (Python/FastAPI, port 8081) — Runs the LightGBM model. Takes customer history, computes deal-agnostic score, assigns tier (SUPPRESS / NUDGE / FULL_COUPON), evaluates 6 personalized strategies.
- **Web App** (Remix, port 3000) — Embedded Shopify admin UI. OAuth, billing, dashboard, settings, customer list, onboarding. Calls scoring service on webhooks.
- **Shopify Function** (extensions/) — Wasm code that runs at checkout inside Shopify's infrastructure. Reads pre-computed metafield, applies discount. Zero network calls.
- **Theme App Extension** (extensions/) — Storefront-facing Liquid blocks: personalized offer banners + price strikethroughs on product pages.

## Prerequisites

- Node.js 18+
- Python 3.11+
- [Shopify CLI](https://shopify.dev/docs/api/shopify-cli) (`npm install -g @shopify/cli`)
- A [Shopify Partner account](https://partners.shopify.com/) with a development store
- Docker & Docker Compose (for containerized deployment)

## Local Development

### 1. Set up the scoring service

```bash
cd scoring-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8081
```

### 2. Set up the web app

```bash
cd web
npm install
npx prisma generate
npx prisma db push
```

### 3. Configure environment

Create `web/.env`:

```
SHOPIFY_API_KEY=<from Partner Dashboard>
SHOPIFY_API_SECRET=<from Partner Dashboard>
SHOPIFY_APP_URL=<your ngrok/cloudflare tunnel URL>
SCOPES=read_customers,write_customers,read_orders,read_products,write_discounts,read_discounts
DATABASE_URL=file:./prisma/dev.db
SCORING_SERVICE_URL=http://localhost:8081
```

### 4. Run with Shopify CLI

```bash
cd web
shopify app dev
```

This starts the Remix dev server and sets up the ngrok tunnel automatically.

## Docker Deployment

```bash
cd shopify_app
cp .env.example .env   # fill in your credentials
docker compose up --build
```

## Deploy to Fly.io

```bash
# Deploy scoring service
cd scoring-service
fly launch --name your-app-scoring
fly deploy

# Deploy web app
cd web
fly launch --name your-app-web
fly secrets set SHOPIFY_API_KEY=... SHOPIFY_API_SECRET=... SHOPIFY_APP_URL=https://your-app-web.fly.dev SCORING_SERVICE_URL=https://your-app-scoring.fly.dev
fly deploy
```

Update `shopify.app.toml` with your production `application_url` and `client_id`, then run `shopify app deploy` to push the Shopify Function extension.

## Environment Variables

| Variable | Description |
|---|---|
| `SHOPIFY_API_KEY` | App API key from Partner Dashboard |
| `SHOPIFY_API_SECRET` | App API secret from Partner Dashboard |
| `SHOPIFY_APP_URL` | Public URL of the web app |
| `SCOPES` | Shopify API permission scopes |
| `DATABASE_URL` | Prisma database connection string |
| `SCORING_SERVICE_URL` | URL of the scoring microservice |
| `SHOP_CUSTOM_DOMAIN` | (Optional) Custom myshopify domain |
