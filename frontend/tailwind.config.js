/** Tailwind v3.4 config for the bundled Trove SPA.
 *
 * The static build (css/tailwind.css) replaces the Play CDN compiler so the
 * strict 'self' CSP holds. Content = index.html + every frontend JS file
 * (Trove templates + Dockwatch modules); class literals in issuerColor,
 * terminal prefixes, filter buttons etc. are full strings so they're captured.
 */
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./js/**/*.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  corePlugins: {
    preflight: true,
  },
  plugins: [],
};