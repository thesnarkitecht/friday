import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

// Relative base: the same build is served by the FRIDAY server at /glasses/ and by the browser preview.
export default defineConfig({base: './', plugins: [react()]});
