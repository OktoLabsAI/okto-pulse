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
      // Extract detail: direct field, or nested in backend_error (BFF proxy pattern)
      const detail = errorData.backend_error?.detail ?? errorData.detail;
      const message = typeof detail === 'string' ? detail
        : typeof detail === 'object' && detail !== null
          ? ((detail as Record<string, unknown>).message as string | undefined)
            ?? ((detail as Record<string, unknown>).error as string | undefined)
            ?? JSON.stringify(detail)
          : errorData.message || errorData.error || `HTTP ${response.status}: ${response.statusText}`;
      throw new Error(message);
    }

    // 204 No Content (and any other empty-body success, e.g. DELETE endpoints) carry
    // no JSON. Calling response.json() on an empty body throws "Unexpected end of JSON
    // input", so guard it: return undefined for empty bodies, parse otherwise.
    if (response.status === 204) {
      return undefined as T;
    }
    const text = await response.text();
    return (text ? (JSON.parse(text) as T) : (undefined as T));
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
