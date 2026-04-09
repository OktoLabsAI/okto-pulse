/**
 * Authenticated HTTP client with automatic token injection
 */

export type TokenGetter = (options?: { skipCache?: boolean }) => Promise<string | null>;

export interface AuthFetchOptions extends RequestInit {
  skipAuth?: boolean;
  maxRetries?: number;
}

export class AuthenticatedFetch {
  private tokenGetter: TokenGetter;
  private baseUrl: string;

  constructor(tokenGetter: TokenGetter, baseUrl: string = '') {
    this.tokenGetter = tokenGetter;
    this.baseUrl = baseUrl;
  }

  async fetch(url: string, options: AuthFetchOptions = {}): Promise<Response> {
    const { skipAuth = false, maxRetries = 1, ...fetchOptions } = options;

    const fullUrl = url.startsWith('http') ? url : `${this.baseUrl}${url}`;
    const token = skipAuth ? null : await this.tokenGetter();
    const headers = new Headers(fetchOptions.headers);

    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    let response = await fetch(fullUrl, { ...fetchOptions, headers });

    // Handle 401 - retry with fresh token
    if (response.status === 401 && !skipAuth && maxRetries > 0) {
      const newToken = await this.tokenGetter({ skipCache: true });
      if (newToken) {
        headers.set('Authorization', `Bearer ${newToken}`);
        response = await fetch(fullUrl, { ...fetchOptions, headers });
      }
    }

    return response;
  }

  async fetchJson<T>(url: string, options: AuthFetchOptions = {}): Promise<T> {
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    if (!headers.has('Accept')) {
      headers.set('Accept', 'application/json');
    }

    const response = await this.fetch(url, { ...options, headers });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(
        errorData.detail || errorData.message || `HTTP ${response.status}: ${response.statusText}`
      );
    }

    return response.json();
  }
}

// Singleton instance
let globalInstance: AuthenticatedFetch | null = null;

export function initAuthFetch(tokenGetter: TokenGetter, baseUrl?: string): void {
  globalInstance = new AuthenticatedFetch(tokenGetter, baseUrl);
}

export function getAuthFetch(): AuthenticatedFetch {
  if (!globalInstance) {
    throw new Error('AuthenticatedFetch not initialized. Call initAuthFetch() first.');
  }
  return globalInstance;
}

export async function authFetch(url: string, options?: AuthFetchOptions): Promise<Response> {
  return getAuthFetch().fetch(url, options);
}

export async function authFetchJson<T>(url: string, options?: AuthFetchOptions): Promise<T> {
  return getAuthFetch().fetchJson<T>(url, options);
}
