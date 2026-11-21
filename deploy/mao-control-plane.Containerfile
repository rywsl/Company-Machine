ARG BASE_IMAGE=oven/bun:1.3.14

FROM ${BASE_IMAGE} AS build
WORKDIR /app
COPY package.json bun.lock ./
COPY frontend/package.json frontend/package.json
RUN bun install --frozen-lockfile
COPY . .
RUN bun run --cwd frontend build

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
