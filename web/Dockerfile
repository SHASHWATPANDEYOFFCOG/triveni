# Triveni dashboard. Built at M16; this image is here so `docker compose up`
# brings the whole product up in one command once the web app lands.
FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY . .

ENV NEXT_PUBLIC_API_BASE=http://api:8000
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
