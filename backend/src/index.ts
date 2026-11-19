export * from "./models";
export * from "./db";
export * from "./event-bus";
export * from "./server";
export * from "./harness";
export * from "./acp";
export * from "./foreman";
export * from "./operator";
// The Bun control plane exposes the MRL boundary so business projections and
// Foreman adapters can validate predicates without importing the Python tree.
export * from "../mrl";
import { startServer } from "./server";

if (import.meta.main) {
  const app = startServer();
  console.log(`backend listening on ${app.server.url}`);
}

