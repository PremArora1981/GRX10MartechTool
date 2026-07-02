/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The FastAPI base URL is read at runtime in server components via
  // process.env.NEXT_PUBLIC_API_BASE_URL (wired by render.yaml / .env).
  experimental: {
    // Recharts/visx are client-only; keep them out of the RSC bundle graph
    // until a screen explicitly marks "use client".
  },
};

export default nextConfig;
