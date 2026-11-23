// the rules of Scan Codebase's eslint run: the security plugin's recommended set for JavaScript, and the same for TypeScript through its parser
import security from "eslint-plugin-security";
import tseslint from "typescript-eslint";

export default [
  security.configs.recommended,
  { files: ["**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts"], languageOptions: { parser: tseslint.parser } },
  { ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.venv/**"] },
];
