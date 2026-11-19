import { createApp } from "./src/server";

const count = Math.max(1, Math.min(500, Number(Bun.argv[2] ?? 100)));
const dbPath = process.env.BENCH_DB_PATH ?? "output/backend-bench.db";
const app = createApp({ dbPath, legacyPath: dbPath });

for (let index = 0; index < count; index += 1) {
  const system = app.store.createBusinessSystem({ name: `bench-${index}` });
  app.store.createIntake({ business_system_id: system.id, name: `project-${index}`, repo_url: `https://example.test/${index}.git` });
  app.events.publish("project.intake.created", { index, systemId: system.id });
}

const measure = async (path: string) => {
  const start = performance.now();
  const response = await app.fetch(new Request(`http://localhost${path}`));
  if (response.headers.get("content-type")?.includes("text/event-stream")) {
    const reader = response.body?.getReader();
    await reader?.read();
    await reader?.cancel();
  } else {
    await response.arrayBuffer();
  }
  return { path, status: response.status, duration_ms: Number((performance.now() - start).toFixed(2)) };
};

const results = await Promise.all([
  measure("/api/web/graph?scope=factory"),
  measure("/api/v1/events?cursor=0"),
  measure("/api/v1/projects"),
]);
console.log(JSON.stringify({ count, results }, null, 2));
app.stop();
