import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "build/**",
      "node_modules/**",
      "test-results/**",
      "playwright-report/**",
      "src-tauri/target/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["tests/e2e/**/*.ts"],
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
);
