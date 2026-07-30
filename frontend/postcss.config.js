/**
 * PostCSS pipeline.
 *
 * Without this file the `@tailwind` directives in src/styles/index.css are passed
 * through to the browser untouched and every utility class in the app silently does
 * nothing - the page renders as unstyled HTML with no build error to explain it.
 *
 * ESM syntax because package.json declares `"type": "module"`; a `module.exports`
 * here would fail to load.
 */

export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
