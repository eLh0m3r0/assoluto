/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/templates/**/*.html',
    './app/static/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#f1f5fe',
          100: '#e2ebfd',
          500: '#3b6efb',
          600: '#2f5bd9',
          700: '#2247b0',
        },
      },
    },
  },
  plugins: [],
};
