import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from 'react-hot-toast';
import { authAdapter, adapterReady } from '@/adapters/auth';
import App from './App';
import './index.css';
import { ApiProvider } from '@/contexts/ApiContext';

const handleAuthFailure = () => {
  console.warn('[Auth] Authentication failed');
};

function Root() {
  const { Provider: AuthProvider } = authAdapter;
  return (
    <AuthProvider>
      <ApiProvider onAuthFailure={handleAuthFailure}>
        <App />
        <Toaster position="top-right" containerStyle={{ zIndex: 20000 }} />
      </ApiProvider>
    </AuthProvider>
  );
}

// Wait for the auth adapter to load, then render
adapterReady.then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <Root />
    </StrictMode>
  );
});
