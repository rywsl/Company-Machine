import type { Project } from "./models";

export function repositoryReference(value: unknown, mode: "public" | "intake" = "public"): string | undefined {
  if (typeof value !== "string" || !value.trim() || /[\u0000-\u001f\u007f]/.test(value)) return undefined;
  const reference = value.trim();
  try {
    const url = new URL(reference);
    if (!["https:", "http:", "ssh:", "git:"].includes(url.protocol)) return undefined;
    const http = url.protocol === "https:" || url.protocol === "http:";
    if (!url.hostname || (mode === "intake" && (url.password || (http && url.username) || /[?#]/.test(reference)))) return undefined;
    if (http) url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.href;
  } catch {
    return /^[\w.-]+@[\w.-]+:[^\s\\?#]+$/.test(reference) ? reference : undefined;
  }
}

export function projectSourceFacts(project: Project, localAccess: boolean): Array<{
  kind: Project["sources"][number]["kind"];
  repository: string | undefined;
  folder: string | undefined;
  file: string | undefined;
  reference: string | undefined;
  name: string | undefined;
  download: string | undefined;
  commit?: string;
  checkout?: string;
}> {
  return project.sources.map((source) => {
    const digest = typeof source.metadata.digest === "string" && /^sha256:[0-9a-f]{64}$/.test(source.metadata.digest)
      ? source.metadata.digest : undefined;
    return {
      kind: source.kind,
      repository: repositoryReference(source.repoUrl),
      folder: localAccess ? source.localFolder : undefined,
      file: source.fileRef,
      reference: source.kind === "local_folder" ? `intake://${project.id}/${source.id}` : undefined,
      name: typeof source.metadata.name === "string" ? source.metadata.name : undefined,
      download: digest ? `/api/v1/projects/${project.id}/files/${digest}` : undefined,
    };
  });
}
