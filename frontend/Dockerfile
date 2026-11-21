# 构建阶段：node 只用来打包，最终镜像里不保留 node
FROM node:20-alpine AS build

WORKDIR /app

# 国内网络友好；可用 --build-arg NPM_REGISTRY=... 覆盖
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "$NPM_REGISTRY" && npm config set fund false

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
ARG VITE_BASE_PATH=/
RUN npm run build


# 运行阶段：nginx 只提供静态文件 + 反代 /api
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
ENV QUANTLAB_API_UPSTREAM=api
COPY nginx.conf /etc/nginx/templates/default.conf.template

EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
