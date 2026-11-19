import { createApp } from "./server";

const args = Bun.argv.slice(2);
const command = args[0] ?? "help";
const value = (name: string) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
};
const redactText = (text: string) => text.replace(/(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`);

function usage(): void {
  console.log(`factory-bun commands
  serve
  status
  project intake --name NAME [--repo URL | --folder PATH | --file-ref REF]
  foreman inbox [--limit N]
  foreman claim ID
  foreman done ID
  talk send TEXT
  talk reply TEXT --turn-id ID`);
  process.exit(0);
}

if (command === "help") usage();
if (command === "serve") {
  const app = (await import("./server")).startServer({ port: Number(process.env.PORT ?? 3000) });
  console.log(`Bun control plane listening on ${app.server.url}`);
} else {
  const app = createApp();
  try {
    if (command === "status") {
      console.log(JSON.stringify({ ok: true, businessSystems: app.store.listBusinessSystems().length, foremanInbox: app.store.listForemanInbox().length }, null, 2));
    } else if (command === "project" && args[1] === "intake") {
      const repo = value("--repo");
      const folder = value("--folder");
      const fileRef = value("--file-ref");
      if (!repo && !folder && !fileRef) throw new Error("one source is required");
      const name = value("--name") ?? "新项目";
      const system = app.store.createBusinessSystem({ name, metadata: { intake: true, source: "factory-bun" } });
      const project = app.store.createIntake({ name, business_system_id: system.id, repo_url: repo, local_folder: folder, uploaded_file_refs: fileRef ? [fileRef] : [] });
      app.events.publish("business-system.created", system);
      app.events.publish("project.intake.created", project);
      console.log(JSON.stringify(project, null, 2));
    } else if (command === "foreman" && args[1] === "inbox") {
      console.log(JSON.stringify(app.store.listForemanInbox(Number(value("--limit") ?? 50)), null, 2));
    } else if (command === "foreman" && (args[1] === "claim" || args[1] === "done")) {
      const id = args[2];
      if (!id) throw new Error("message id is required");
      const result = args[1] === "claim" ? app.store.claimForemanMessage(id) : app.store.completeForemanMessage(id);
      if (!result) throw new Error("message not found");
      console.log(JSON.stringify(result, null, 2));
    } else if (command === "talk" && args[1] === "send") {
      const text = args.slice(2).join(" ").trim();
      if (!text) throw new Error("text is required");
      const message = app.store.addTalkMessage({ body: redactText(text) });
      const envelope = app.store.queueForemanMessage(message.id, message.body);
      app.events.publish("talk.message.created", message);
      app.events.publish("foreman.message.queued", envelope);
      console.log(JSON.stringify(message, null, 2));
    } else if (command === "talk" && args[1] === "reply") {
      const turnId = value("--turn-id");
      const text = args.slice(2).filter((item) => item !== "--turn-id" && item !== turnId).join(" ").trim();
      if (!text || !turnId) throw new Error("text and --turn-id are required");
      const message = app.store.addTalkMessage({ role: "foreman", body: redactText(text), metadata: { turn_id: turnId } });
      app.events.publish("talk.foreman.replied", message);
      console.log(JSON.stringify(message, null, 2));
    } else {
      usage();
    }
  } finally {
    app.stop();
  }
}
