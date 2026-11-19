import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/mao/webapp/static/dist",
    emptyOutDir: true,
    sourcemap: false,
    manifest: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/.pnpm/@xyflow") || id.includes("node_modules/@xyflow")) return "react-flow";
          if (id.includes("node_modules/.pnpm/react@") || id.includes("node_modules/.pnpm/react-dom@") || id.includes("node_modules/react/") || id.includes("node_modules/react-dom/")) return "react-vendor";
          return undefined;
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
});
