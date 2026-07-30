/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_APP_NAME: string;
  readonly VITE_ENABLE_MICROSOFT_SSO: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
