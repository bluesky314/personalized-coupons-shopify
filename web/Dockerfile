FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY prisma/ ./prisma/
RUN npx prisma generate

COPY . .
RUN npx remix vite:build

EXPOSE 3000

CMD ["npx", "remix-serve", "build/server/index.js"]
