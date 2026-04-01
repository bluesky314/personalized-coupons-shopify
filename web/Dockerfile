FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY prisma/ ./prisma/
RUN npx prisma generate

COPY . .

ENV SHOPIFY_API_KEY=0b13ded941fa8b8908df09912d9f18ad
RUN npx remix vite:build

ENV NODE_ENV=production

CMD ["sh", "-c", "DATABASE_URL=file:/tmp/prod.db npx prisma db push --accept-data-loss 2>/dev/null; node node_modules/.bin/remix-serve build/server/index.js"]
