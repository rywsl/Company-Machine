ARG BASE_IMAGE=oven/bun:1.3.14

FROM ${BASE_IMAGE} AS build
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json frontend/package.json
RUN npm install --global pnpm@11.16.0 \
 && pnpm install --frozen-lockfile
COPY . .
RUN pnpm --filter @mao/web build

FROM ${BASE_IMAGE}
WORKDIR /app
COPY --from=build /app /app
ENV FACTORY_ROOT=/var/lib/mao \
    FACTORY_DB_PATH=/var/lib/mao/factory.db \
    BUSINESS_DB_PATH=/var/lib/mao/factory.db \
    MAO_WEB_DIST=/app/frontend/dist \
    PORT=3000
EXPOSE 3000
CMD ["bun", "run", "backend/src/index.ts"]
