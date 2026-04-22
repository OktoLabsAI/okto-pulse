/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  readonly VITE_CLERK_PUBLISHABLE_KEY: string;
  readonly VITE_AUTH_MODE: 'clerk' | 'local';
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** Compile-time constant injected by Vite `define` — see vite.config.ts */
declare const __AUTH_MODE__: 'clerk' | 'local';
