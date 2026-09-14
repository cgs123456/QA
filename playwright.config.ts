import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "qa_sse.spec.ts",
  timeout: 120000,
  fullyParallel: false,
  reporter: "list",
});
