import forms from '@tailwindcss/forms';
import containerQueries from '@tailwindcss/container-queries';

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
      },
      colors: {
        brand: {
          navy: '#0b192c',
          dark: '#0e1f38',
          accent: '#0284c7',
          teal: '#0d9488',
          tealDark: '#0f766e',
        },
      },
    },
  },
  plugins: [
    forms,
    containerQueries,
  ],
};

