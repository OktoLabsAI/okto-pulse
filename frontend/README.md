# OmniFlow Project Dashboard

Frontend React para o Dashboard Kanban estilo Trello.

## Funcionalidades

- Board Kanban com drag and drop
- 6 colunas: Não Iniciado, Iniciado, Em Andamento, Em Pendência, Finalizado, Cancelado
- Cards com detalhes, anexos, Q&A e comentários
- Autenticação via Clerk
- Interface responsiva e dark mode

## Instalação

```bash
cd frontend
npm install
```

## Configuração

Copie `.env.example` para `.env`:

```bash
cp .env.example .env
```

Configure a chave pública do Clerk:
```
VITE_CLERK_PUBLISHABLE_KEY=pk_test_xxx
```

## Execução

```bash
npm run dev
```

## Build

```bash
npm run build
```

## Stack

- React 18 + TypeScript
- Vite
- Tailwind CSS
- Zustand (state management)
- @dnd-kit (drag and drop)
- Clerk (autenticação)
- Lucide React (ícones)
