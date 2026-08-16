import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep Turbopack scoped to this app. A package-lock elsewhere in a developer's
  // home directory must not change module resolution for spark-hacks.
  turbopack: {
    root: process.cwd(),
  },
};

export default nextConfig;
