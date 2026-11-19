export * from "./models";
export * from "./db";
export * from "./event-bus";
export * from "./server";
export * from "./harness";
import { startServer } from "./server";

if (import.meta.main) {
  const app = startServer();
  console.log(`backend listening on ${app.server.url}`);
}

