/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The public dashboard NEVER talks to the broker or IB Gateway directly. It only reads the
  // backend Dashboard API (read-only) via NEXT_PUBLIC_API_BASE_URL. No secrets are ever bundled.
};

export default nextConfig;
