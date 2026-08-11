import eslint from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/lib/**/*.ts", "src-vanilla/**/*.ts", "vite.config.ts"],
    rules: {
      // TypeScript already performs this check with the correct DOM globals.
      "no-undef": "off",
    },
  },
);
