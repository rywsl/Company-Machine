interface RepositoryRegistration {
  name: string;
  repoUrl: string;
  systemId?: string;
}

export type RegisterRepository = (input: RepositoryRegistration) => { projectId: string; systemId: string } | undefined;

export function parseRepositoryRegistration(args: { repoUrl?: unknown; name?: unknown; systemId?: unknown }): RepositoryRegistration | undefined {
  const { repoUrl, name, systemId } = args;
  if (typeof repoUrl !== "string" || (name !== undefined && (typeof name !== "string" || !name.trim()))) return;
  if (systemId !== undefined && (typeof systemId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$/.test(systemId))) return;
  const source = repoUrl.trim();
  if (!/^(?:https?|ssh|git):\/\//i.test(source) || /[\s\\?#]/.test(source)) return;
  try {
    const url = new URL(source);
    const slug = url.pathname.split("/").filter(Boolean).at(-1)?.replace(/\.git$/i, "");
    if (!url.hostname || url.username || url.password || !slug) return;
    const projectName = typeof name === "string" ? name.trim() : decodeURIComponent(slug);
    if (!projectName.trim() || projectName.length > 256 || /[\u0000-\u001f\u007f]/.test(projectName)) return;
    return { name: projectName, repoUrl: url.href, systemId };
  } catch { return; }
}
