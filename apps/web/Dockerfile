# Frontend: Node build → `vite preview`（纯静态 SPA，无后端逻辑）
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=build /app/dist ./dist
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
EXPOSE 3000
# vite preview 是给 SPA 用的静态服务器（无 SSR），demo 规模足够
CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "3000"]