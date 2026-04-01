FROM node:20-slim

WORKDIR /app

# Install deps
COPY package.json package-lock.json ./
RUN npm ci

# Prisma
COPY prisma/ ./prisma/
RUN npx prisma generate

# Copy app source
COPY . .

# Build needs SHOPIFY_API_KEY at compile time
ENV SHOPIFY_API_KEY=0b13ded941fa8b8908df09912d9f18ad
RUN npx remix vite:build

# Create SQLite DB
RUN npx prisma db push --accept-data-loss || true

ENV NODE_ENV=production
EXPOSE 3000

CMD ["sh", "-c", "npx prisma db push --accept-data-loss 2>/dev/null; node node_modules/.bin/remix-serve build/server/index.js"]
