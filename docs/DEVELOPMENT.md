# Development

## Install Dependencies

```bash
pnpm install
```

## Run Desktop App

```bash
pnpm dev
```

This opens the native Tauri desktop application. The Vite server is started
only as an internal development renderer for hot reload.

## Run Web Renderer Preview

```bash
pnpm web:dev
```

Use this only for quick UI layout debugging. It is not the product surface users
should run.

## Run ASR Service Stub

```bash
pnpm service:dev
```

Health check:

```bash
curl http://127.0.0.1:17863/health
```

## Validate

```bash
pnpm typecheck
cargo check -p voxkey-core
pnpm desktop:build
```
