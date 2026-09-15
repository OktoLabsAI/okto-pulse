import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// A frontend-only instance: never proxy requests to the user's running Pulse.
export default defineConfig({
  plugins: [react()],
  define: { __AUTH_MODE__: JSON.stringify('local'), __APP_VERSION__: JSON.stringify('0.3.3') },
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: { host: '127.0.0.1', port: 5189, strictPort: true, proxy: {} },
});
