/** @type {import('next').NextConfig} */
// Контейнер запускается через `next start` (см. Dockerfile), поэтому
// output: "standalone" НЕ используется — иначе next start работает некорректно
// и серверные route handlers (app/api/proxy) могут не подниматься.
const nextConfig = {};

module.exports = nextConfig;
