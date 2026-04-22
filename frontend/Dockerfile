# Stage 1: build
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
# Remove lock file to fix npm optional deps bug, then install fresh
RUN rm -f package-lock.json && npm install
COPY . .
ARG VITE_CLERK_PUBLISHABLE_KEY
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# Stage 2: nginx
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
