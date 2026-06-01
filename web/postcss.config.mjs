/**
 * Pronaos admin UI — PostCSS config.
 * tailwindcss runs first to produce utility classes,
 * autoprefixer adds vendor prefixes for the browser matrix.
 */
const config = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};

export default config;
