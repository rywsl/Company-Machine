export class YamlError extends Error {
  constructor(readonly message: string, readonly line = 0, readonly col = 0) {
    super(message);
    this.name = "YamlError";
  }
}

export interface YamlDocument {
  index: number;
  line: number;
  data: unknown;
  positions: Record<string, [number, number]>;
}

function yamlParser(): ((source: string) => unknown) {
  const bun = (globalThis as { Bun?: { YAML?: { parse?: (source: string) => unknown } } }).Bun;
  if (!bun?.YAML?.parse) throw new Error("Bun YAML support is required");
  return bun.YAML.parse;
}

function splitDocuments(source: string): Array<{ text: string; line: number; index: number }> {
  const lines = source.split(/\r?\n/);
  const documents: Array<{ text: string; line: number; index: number }> = [];
  let start = 1;
  let documentIndex = 0;
  let current: string[] = [];
  const push = () => {
    const text = current.join("\n");
    if (text.trim() && !/^\.\.\.\s*(?:#.*)?$/.test(text.trim())) documents.push({ text, line: start, index: documentIndex });
    documentIndex += 1;
    current = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^---\s*(?:#.*)?$/.test(line)) {
      push();
      start = index + 2;
      continue;
    }
    if (/^\.\.\.\s*(?:#.*)?$/.test(line)) {
      push();
      start = index + 2;
      continue;
    }
    current.push(line);
  }
  push();
  return documents;
}

export function loadDocuments(source: string): YamlDocument[] {
  const parse = yamlParser();
  return splitDocuments(source).flatMap(({ text, line, index }) => {
    try {
      const data = parse(text);
      if (data === null || data === undefined) return [];
      return [{ index, line, data, positions: { "": [line, 1] } }];
    } catch (error) {
      throw new YamlError(error instanceof Error ? error.message : "invalid YAML", line, 1);
    }
  });
}

export function loadOne(source: string): unknown {
  const documents = loadDocuments(source);
  if (documents.length !== 1) throw new YamlError(`expected one YAML document, got ${documents.length}`);
  return documents[0].data;
}

export function loadFile(path: string): unknown {
  return loadOne(readFileSync(path, "utf8"));
}
import { readFileSync } from "node:fs";

