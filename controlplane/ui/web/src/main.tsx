import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { OverlayProvider } from './components/overlays';
import { router } from './router';
import './app.css';

const client = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false } } });

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <OverlayProvider>
        <RouterProvider router={router} />
      </OverlayProvider>
    </QueryClientProvider>
  </StrictMode>,
);
