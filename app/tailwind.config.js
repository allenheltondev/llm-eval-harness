/**
 * Colors, fonts and dark mode come from the Ready, Set, Cloud design system
 * (@readysetcloud/ui). Every color resolves through its token CSS variables,
 * so `bg-primary-600` is on-brand in light and dark alike -- never define
 * colors here (see node_modules/@readysetcloud/ui/AGENTS.md).
 */
import rscPreset from '@readysetcloud/ui/tailwind-preset'

/** @type {import('tailwindcss').Config} */
export default {
  presets: [rscPreset],
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
    // The package's components render token utility classes.
    './node_modules/@readysetcloud/ui/dist/**/*.js'
  ]
}
