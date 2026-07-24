# EVETIS WB Analytics — общий образ для всех Cloud Run Jobs (PR-Mig0).
# Один образ; конкретный загрузчик выбирается командой: node dist/cli.js <loader>.
# Продвигаем ОДИН immutable digest из shadow в prod (образ в prod не пересобирается).

FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:20-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
# Cloud Run Job переопределяет args: ["<loader>"]. По умолчанию — noop (foundation).
ENTRYPOINT ["node", "dist/cli.js"]
CMD ["noop"]
