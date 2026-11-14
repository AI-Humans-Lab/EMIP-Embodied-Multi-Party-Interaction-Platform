FROM node:24.21.0-bookworm-slim

WORKDIR /app

# 依存ファイルを先にコピー
COPY package*.json ./
COPY mineflayer-4.33.0 ./mineflayer-4.33.0

RUN npm ci --omit=dev --install-links

# 残りのソースをコピー
COPY . .

CMD ["node", "server.js"]
