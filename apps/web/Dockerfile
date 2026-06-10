FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json* ./

RUN npm install

COPY . .

# Конфиг API больше не нужен на этапе build: owner-вызовы идут через серверный
# прокси (app/api/proxy), который читает API_BASE_URL / OWNER_API_TOKEN в
# рантайме. Поэтому build-arg'ов для API здесь нет.

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start"]