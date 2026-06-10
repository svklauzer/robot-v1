FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json* ./

RUN npm install

COPY . .

# NEXT_PUBLIC_* инлайнится в бандл во время build, поэтому значение должно
# попасть в сборку. Render автоматически прокидывает env сервиса как build-arg,
# если в Dockerfile объявлен соответствующий ARG.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start"]