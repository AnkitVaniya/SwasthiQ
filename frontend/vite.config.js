import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy API calls to the FastAPI server. In production FastAPI serves the built UI itself (one origin).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000", "/agent": "http://localhost:8000", "/tools": "http://localhost:8000" } },
});
