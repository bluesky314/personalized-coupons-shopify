FROM node:20-slim AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --production=false

COPY prisma/ ./prisma/
RUN npx prisma generate

COPY . .

# Build needs these env vars at compile time
ARG SHOPIFY_API_KEY=0b13ded941fa8b8908df09912d9f18ad
ENV SHOPIFY_API_KEY=$SHOPIFY_API_KEY

RUN npx remix vite:build

FROM node:20-slim

WORKDIR /app

COPY --from=build /app/build ./build
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./
COPY --from=build /app/prisma ./prisma

# Generate prisma client in runtime image
RUN npx prisma generate
# Create the SQLite DB
RUN npx prisma db push --accept-data-loss 2>/dev/null || true

EXPOSE 3000

CMD ["npx", "remix-serve", "build/server/index.js"]
