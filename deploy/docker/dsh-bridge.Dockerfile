FROM node:24-bookworm-slim
WORKDIR /app
COPY services/dsh-bridge/package.json ./
COPY services/dsh-bridge/src ./src
ENV NODE_ENV=production
EXPOSE 8103
CMD ["node", "src/index.mjs"]
