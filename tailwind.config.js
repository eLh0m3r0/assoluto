/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './app/templates/**/*.html',
    './app/static/js/**/*.js',
  ],
  safelist: [
    // Dynamic tints used in Jinja macros on marketing pages (features/index
    // feat() and state_row() macros). Tailwind JIT can't discover
    // `bg-{{ tint }}-50`, so we list the full combinations explicitly.
    ...['blue','emerald','violet','amber','teal','indigo','rose','slate','brand','purple','pink','sky'].flatMap(c => [
      `bg-${c}-50`, `bg-${c}-100`, `bg-${c}-500`, `bg-${c}-600`,
      `text-${c}-600`, `text-${c}-700`, `text-${c}-400`, `text-${c}-300`,
      `ring-${c}-100`, `ring-${c}-200`, `ring-${c}-500/30`, `ring-${c}-900/40`,
      `dark:bg-${c}-500/10`, `dark:bg-${c}-500/20`,
      `dark:text-${c}-400`, `dark:text-${c}-300`, `dark:text-${c}-200`,
      `dark:ring-${c}-500/30`, `dark:ring-${c}-900/40`,
    ]),
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#f1f5fe',
          100: '#e2ebfd',
          200: '#c8d9fc',
          300: '#9dbbfa',
          400: '#6b94f7',
          500: '#3b6efb',
          600: '#2f5bd9',
          700: '#2247b0',
          800: '#1c3a8f',
          900: '#1a2f6e',
        },
      },
    },
  },
  plugins: [],
};
