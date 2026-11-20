import type { Project } from "./models";

function repositoryReference(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (!["https:", "http:", "ssh:", "git:"].includes(url.protocol)) return undefined;
    if (url.protocol === "https:" || url.protocol === "http:") url.username = "";
    url.password = "";
    for (const name of [...url.searchParams.keys()]) {
      if (/token|secret|password|api[_-]?key|authorization|credential/i.test(name)) url.searchParams.delete(name);
    }
    return url.href;
  } catch {
    return /^[\w.-]+@[\w.-]+:[^\s]+$/.test(value) ? value : undefined;
  }
}

export function projectSourceFacts(project: Project, localAccess: boolean) {
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
