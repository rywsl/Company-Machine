import { readFileSync } from "node:fs";
import { parseNumericScalar } from "../numbers";

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

function splitDocuments(source: string): Array<{ text: string; line: number; start: number; index: number }> {
  const lines = source.split(/\r?\n/);
  const documents: Array<{ text: string; line: number; start: number; index: number }> = [];
  let start = 1;
  let documentIndex = 0;
  let explicitStart = false;
  let current: string[] = [];
  const push = () => {
    const firstContentLine = current.findIndex((line) => line.trim() && !line.trimStart().startsWith("#"));
    if (firstContentLine >= 0) documents.push({ text: current.join("\n"), line: start + firstContentLine, start, index: documentIndex });
    if (firstContentLine >= 0 || explicitStart) documentIndex += 1;
    current = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^---\s*(?:#.*)?$/.test(line)) {
      push();
      explicitStart = true;
      start = index + 2;
      continue;
    }
    if (/^\.\.\.\s*(?:#.*)?$/.test(line)) {
      push();
      explicitStart = false;
      start = index + 2;
      continue;
    }
    current.push(line);
  }
  push();
  return documents;
}

interface SourcePoint {
  line: number;
  col: number;
  offset: number;
}
interface FlowFrame {
  keys?: Set<string>;
  start: SourcePoint;
  firstToken?: SourcePoint;
  hasKey: boolean;
}
const numericScalar = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;
const keySeparator = /[\s,[\]{}]/;
const scalarHeader = /^[|>](?:[1-9][+-]?|[+-][1-9]?)?(?:\s+#.*|\s*)$/;
const property = /^(?:&[^\s,[\]{}]+|![^\s,[\]{}]*)\s+/;

// Bun discards duplicate mapping keys and the lexical kind of decimal scalars.
// Walk the source once to retain those details before letting Bun parse YAML.
function inspectSource(source: string, startLine: number, preserveNumbers: boolean, parse: (text: string) => unknown) {
  const block = new Map<number, Set<string>>();
  const flow: FlowFrame[] = [];
  const replacements: { start: number; end: number; text: string }[] = [];
  let marker = "__mao_mrl_numeric_";
  while (preserveNumbers && source.includes(marker)) marker = "_" + marker;
  let offset = 0;
  let quote = "";
  let blockScalarIndent = -1;
  let canStartValue = true;
  const readKey = (raw: string) => {
    if (!raw.startsWith('"') && !raw.startsWith("'")) return raw;
    return String(parse(raw));
  };
  const addKey = (keys: Set<string>, raw: string, point: SourcePoint) => {
    const key = readKey(raw.trim());
    if (keys.has(key)) throw new YamlError("重复的键 '" + key + "'", point.line, point.col);
    keys.add(key);
  };
  const retainScalar = (start: number, end: number) => {
    if (!preserveNumbers || start >= end) return;
    const raw = source.slice(start, end);
    const leading = raw.match(/^\s*/)?.[0].length ?? 0;
    const scalar = raw.trim();
    if (numericScalar.test(scalar)) {
      replacements.push({ start: start + leading, end: start + leading + scalar.length, text: JSON.stringify(marker + scalar) });
    }
  };
  for (const [index, line] of source.split("\n").entries()) {
    const lineStart = offset;
    offset += line.length + 1;
    const indent = line.length - line.trimStart().length;
    if (indent === line.length || line[indent] === "#") continue;
    if (blockScalarIndent >= 0 && indent > blockScalarIndent) continue;
    blockScalarIndent = -1;
    let at = indent;
    const point = (col: number): SourcePoint => ({ line: startLine + index, col: col + 1, offset: lineStart + col });
    if (flow.length === 0 && !quote) {
      for (const level of block.keys()) if (level > indent) block.delete(level);
      while (/^-(?:\s|$)/.test(line.slice(at))) {
        at += 1;
        while (/\s/.test(line[at] ?? "")) at += 1;
        for (const level of block.keys()) if (level >= at) block.delete(level);
      }
      const key = /^(?:"(?:\\.|[^"\\])*"|'(?:''|[^'])*'|[^"'[{#][^:[\]{}]*?)\s*:(?:\s|$)/.exec(line.slice(at));
      let valueIndent = indent;
      if (key) {
        const colon = key[0].lastIndexOf(":");
        let keys = block.get(at);
        if (!keys) block.set(at, keys = new Set());
        addKey(keys, key[0].slice(0, colon), point(at));
        valueIndent = at;
        at += colon + 1;
      }
      while (/\s/.test(line[at] ?? "")) at += 1;
      let prefix: RegExpExecArray | null;
      while ((prefix = property.exec(line.slice(at)))) at += prefix[0].length;
      const rest = line.slice(at);
      if (scalarHeader.test(rest)) {
        blockScalarIndent = valueIndent;
        continue;
      }
      if (!rest.startsWith("{") && !rest.startsWith("[") && !rest.startsWith('"') && !rest.startsWith("'")) {
        const comment = rest.search(/\s#/);
        retainScalar(lineStart + at, lineStart + (comment < 0 ? line.length : at + comment));
        continue;
      }
      canStartValue = true;
    }
    for (; at < line.length; at += 1) {
      const character = line[at];
      const frame = flow.at(-1);
      if (quote) {
        if ((quote === '"' && character === "\\") || (quote === "'" && character === "'" && line[at + 1] === "'")) at += 1;
        else if (character === quote) quote = "";
        continue;
      }
      if (/\s/.test(character)) continue;
      if (character === "#" && (at === 0 || /\s/.test(line[at - 1]))) break;
      if (frame && !frame.firstToken && !",]}".includes(character)) frame.firstToken = point(at);
      if (canStartValue && (character === '"' || character === "'")) {
        quote = character;
        canStartValue = false;
      } else if (canStartValue && (character === "{" || character === "[")) {
        flow.push({ keys: character === "{" ? new Set() : undefined, start: point(at + 1), hasKey: false });
      } else if (frame && character === ":" && !frame.hasKey
        && (keySeparator.test(line[at + 1] ?? " ") || ['"', "'"].includes(line[at - 1]))) {
        const key = frame.firstToken ?? frame.start;
        if (frame.keys) addKey(frame.keys, source.slice(key.offset, lineStart + at), key);
        frame.start = point(at + 1);
        frame.firstToken = undefined;
        frame.hasKey = true;
        canStartValue = true;
      } else if (frame && (character === "," || character === "}" || character === "]")) {
        if (!frame.keys || frame.hasKey) retainScalar(frame.start.offset, lineStart + at);
        frame.start = point(at + 1);
        frame.firstToken = undefined;
        frame.hasKey = false;
        if (character !== ",") flow.pop();
        canStartValue = character === ",";
      } else {
        canStartValue = false;
      }
    }
  }
  let text = "";
  let end = 0;
  for (const item of replacements.sort((a, b) => a.start - b.start)) {
    text += source.slice(end, item.start) + item.text;
    end = item.end;
  }
  return { text: text + source.slice(end), marker };
}

function restoreNumbers(value: unknown, marker: string): unknown {
  if (typeof value === "string" && value.startsWith(marker)) return parseNumericScalar(value.slice(marker.length));
  if (Array.isArray(value)) return value.map((item) => restoreNumbers(item, marker));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, restoreNumbers(item, marker)]));
  }
  return value;
}

export function loadDocuments(source: string, preserveNumbers = false): YamlDocument[] {
  const parse = yamlParser();
  return splitDocuments(source).flatMap(({ text, line, start, index }) => {
    try {
      const scanned = inspectSource(text, start, preserveNumbers, parse);
      const parsed = parse(scanned.text);
      const data = preserveNumbers ? restoreNumbers(parsed, scanned.marker) : parsed;
      if (data === null || data === undefined) return [];
      return [{ index, line, data, positions: { "": [line, 1] } }];
    } catch (error) {
      if (error instanceof YamlError) throw error;
      throw new YamlError(error instanceof Error ? error.message : "invalid YAML", line, 1);
    }
  });
}

export function loadOne(source: string, preserveNumbers = false): unknown {
  const documents = loadDocuments(source, preserveNumbers);
  if (documents.length !== 1) throw new YamlError(`expected one YAML document, got ${documents.length}`);
  return documents[0].data;
}

export function loadFile(path: string, preserveNumbers = false): unknown {
  return loadOne(readFileSync(path, "utf8"), preserveNumbers);
}

