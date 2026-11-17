FROM node:24-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/dsh-bridge/package.json ./
COPY services/dsh-bridge/src ./src
ENV NODE_ENV=production
EXPOSE 8104
CMD ["node", "src/sandbox.mjs"]
