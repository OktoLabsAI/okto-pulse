export type { AuthAdapter } from './types';
import { localAdapter } from './local';

export const adapterReady = Promise.resolve();
export { localAdapter as authAdapter };
