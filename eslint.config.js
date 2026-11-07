import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/schema.d.ts", "dev/**", ".local/**", "**/node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["tests/e2e/**/*.mjs"],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["tests/e2e/**/*.spec.ts"],
    rules: {
      "no-restricted-imports": ["error", { paths: [{ name: "@playwright/test", importNames: ["test", "default"], message: "Use the private-test fixture for credential-safe diagnostics." }] }],
    },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
    },
  },
);
