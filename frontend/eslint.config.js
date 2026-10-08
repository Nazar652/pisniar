import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

const DEV_PATTERNS = ['**/dev', '**/\bdev\b/*']
const DEV_IMPORT_MESSAGE = 'src/dev is reached only by the DEV-guarded import in src/main.ts'

export default tseslint.config(
  {
    ignores: ['node_modules', 'dist', 'dist-check', 'src/api/schema.ts'],
  },
  {
    linterOptions: { reportUnusedDisableDirectives: 'error' },
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: { globals: { ...globals.browser } },
    rules: {
      'vue/no-v-html': 'error',
      '@typescript-eslint/no-explicit-any': 'error',
      'no-restricted-imports': [
        'error',
        { patterns: [{ group: DEV_PATTERNS, message: DEV_IMPORT_MESSAGE }] },
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: 'ImportExpression[source.value=/\\bdev\\b/]',
          message: DEV_IMPORT_MESSAGE,
        },
      ],
    },
  },
  {
    files: ['src/main.ts', 'tests/dev-shim.test.ts', 'src/\bdev\b/**/*.ts'],
    rules: { 'no-restricted-imports': 'off', 'no-restricted-syntax': 'off' },
  },
  {
    files: ['scripts/**/*.ts', 'tests/scripts/**/*.ts', 'vite.config.ts', 'eslint.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  prettier,
)
