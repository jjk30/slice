import "dotenv/config";
import express from "express";
import { proxyHandler } from "./proxy";
import { statsRouter } from "./stats";
import { logger } from "./logger";
import { closeDb, runMigrations } from "./db";
import { connectRedis, closeRedis } from "./redis";

const app = express();

// Capture the raw request body as a Buffer for *every* content type so we can
// forward it byte-for-byte and still peek at the `model` field. We never parse
// or mutate it — slice is a transparent proxy.
app.use(express.raw({ type: () => true, limit: "50mb" }));

// Lightweight liveness check that does NOT hit the upstream.
app.get("/healthz", (_req, res) => {
  res.json({ ok: true, service: "slice-gateway", phase: 1 });
});

// Phase 5 — read-only stats API for the dashboard. Mounted BEFORE the catch-all
// proxy so /api/* is served locally and never forwarded upstream. These routes
// only ever run SELECT queries; they cannot affect AI traffic.
app.use("/api", statsRouter);

// Everything else is forwarded to the Anthropic upstream.
app.all(/.*/, proxyHandler);

const port = Number(process.env.PORT ?? 8080);
const server = app.listen(port, () => {
  logger.info(
    {
      port,
      upstream: process.env.ANTHROPIC_UPSTREAM ?? "https://api.anthropic.com",
      router_enabled: process.env.ROUTER_ENABLED === "true",
    },
    "slice gateway listening",
  );

  // Ensure migrations (routing + cache columns, budget_events). Best-effort: a
  // DB outage at startup must not stop the gateway from serving traffic.
  runMigrations().catch((err) => {
    logger.warn({ err: (err as Error).message }, "startup db migration failed (continuing)");
  });

  // Connect Redis in the background. Best-effort: the cache/caps fail open until
  // it's ready, so a Redis outage never stops the gateway from serving.
  void connectRedis();
});

// Graceful shutdown: stop accepting connections, then close the DB pool.
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    logger.info({ signal }, "shutting down");
    server.close(() => {
      Promise.allSettled([closeDb(), closeRedis()]).finally(() => process.exit(0));
    });
  });
}
