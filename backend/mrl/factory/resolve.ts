import type { FunctionSignature } from "../checker";
import { MrlEvalError, compareCanonical, isMrlMap, type MrlValue } from "../evaluator";
import { isMrlNumber, nativeNumber } from "../numbers";
import { BOOL, STRING, isPrimitive, list, record } from "../types";

type Converter = {
  id: string;
  from: string;
  to: string;
  lossless: boolean;
  requires: string[];
  guarantees: string[];
  preserves: Set<string>;
  cost: number;
  p50: number;
};
type Path = {
  chain: string[];
  type: string;
  properties: Set<string>;
  lossless: boolean;
  cost: number;
  p50: number;
};
type Status = "direct" | "convert" | "contract" | "no-converter" | "via-invalid";
const MAX_CHAIN = 4;
const MAX_PATHS = 100_000;

function values(value: MrlValue): MrlValue[] {
  if (!Array.isArray(value)) throw new MrlEvalError("MRL-TYPE", "转换解析需要 list");
  return value;
}
function string(value: MrlValue): string {
  if (typeof value !== "string") throw new MrlEvalError("MRL-TYPE", "转换解析需要 string");
  return value;
}
function strings(value: MrlValue): string[] {
  return values(value).map(string);
}
function converter(value: MrlValue): Converter {
  if (!isMrlMap(value)) throw new MrlEvalError("MRL-TYPE", "转换库元素需要 map");
  const fields = ["id", "from", "to", "lossless", "requires", "guarantees", "preserves", "cost", "p50"];
  for (const field of fields) {
    if (!Object.hasOwn(value, field)) throw new MrlEvalError("MRL-NO-FIELD", "转换库元素缺少 " + field);
  }
  const { lossless, cost, p50 } = value;
  if (typeof lossless !== "boolean" || !isMrlNumber(cost) || !isMrlNumber(p50)) {
    throw new MrlEvalError("MRL-TYPE", "转换库的 lossless、cost 或 p50 类型不合法");
  }
  return {
    id: string(value.id), from: string(value.from), to: string(value.to), lossless,
    requires: strings(value.requires), guarantees: strings(value.guarantees), preserves: new Set(strings(value.preserves)),
    cost: Number(nativeNumber(cost)), p50: Number(nativeNumber(p50)),
  };
}
function pairIndex(value: MrlValue): Map<string, Set<string>> {
  const index = new Map<string, Set<string>>();
  for (const entry of values(value)) {
    const pair = strings(entry);
    if (pair.length !== 2) throw new MrlEvalError("MRL-TYPE", "类型与属性对需要两个元素");
    const [key, val] = pair;
    let group = index.get(key);
    if (!group) {
      group = new Set();
      index.set(key, group);
    }
    group.add(val);
  }
  return index;
}

function comparePaths(left: Path, right: Path): number {
  return Number(right.lossless) - Number(left.lossless)
    || left.chain.length - right.chain.length
    || left.cost - right.cost
    || left.p50 - right.p50
    || compareCanonical(left.chain, right.chain);
}

class Resolver {
  private readonly available: Converter[];
  private readonly subtypes: Map<string, Set<string>>;
  private readonly always: Map<string, Set<string>>;
  private readonly destination: string;
  private readonly requires: Set<string>;
  private readonly initial: Path;

  constructor(lib: MrlValue, subtypes: MrlValue, always: MrlValue, upstream: MrlValue, guarantees: MrlValue, downstream: MrlValue, requires: MrlValue) {
    this.available = values(lib).map(converter).sort((a, b) => compareCanonical(a.id, b.id));
    this.subtypes = pairIndex(subtypes);
    this.always = pairIndex(always);
    this.destination = string(downstream);
    this.requires = new Set(strings(requires));
    const type = string(upstream);
    this.initial = {
      chain: [], type, properties: new Set([...strings(guarantees), ...this.always.get(type) ?? []]), lossless: true, cost: 0, p50: 0,
    };
  }

  private subtype(source: string, target: string): boolean {
    return this.subtypes.get(source)?.has(target) === true;
  }

  private missing(requires: Iterable<string>, provided: ReadonlySet<string>): string[] {
    return [...requires].filter((property) => !provided.has(property)).sort(compareCanonical);
  }

  private step(path: Path, edge: Converter): Path {
    return {
      chain: [...path.chain, edge.id], type: edge.to,
      properties: new Set([...edge.guarantees, ...[...path.properties].filter((item) => edge.preserves.has(item)), ...this.always.get(edge.to) ?? []]),
      lossless: path.lossless && edge.lossless, cost: path.cost + edge.cost, p50: path.p50 + edge.p50,
    };
  }

  private result(status: Status, path: Path, detail = "", chain = path.chain): MrlValue {
    return {
      status, chain, type: path.type, props: [...path.properties].sort(compareCanonical),
      missing: this.missing(this.requires, path.properties),
      lossless: (status === "direct" || status === "convert") && path.lossless, detail,
    };
  }

  auto(): MrlValue {
    const direct = this.subtype(this.initial.type, this.destination);
    if (direct && !this.missing(this.requires, this.initial.properties).length) return this.result("direct", this.initial);
    let best: Path | undefined;
    let frontier = [this.initial];
    let explored = 0;
    for (let depth = 0; depth < MAX_CHAIN && frontier.length; depth += 1) {
      const next: Path[] = [];
      for (const path of frontier) {
        for (const edge of this.available) {
          if (path.chain.includes(edge.id) || !this.subtype(path.type, edge.from)
            || this.missing(edge.requires, path.properties).length) continue;
          explored += 1;
          if (explored > MAX_PATHS) throw new MrlEvalError("MRL-COST", "转换解析探索的路径超过 100000 条");
          const moved = this.step(path, edge);
          if (depth + 1 < MAX_CHAIN) next.push(moved);
          if (this.subtype(moved.type, this.destination) && !this.missing(this.requires, moved.properties).length
            && (!best || comparePaths(moved, best) < 0)) best = moved;
        }
      }
      frontier = next;
    }
    return best ? this.result("convert", best) : this.result(direct ? "contract" : "no-converter", this.initial);
  }

  explicit(chain: string[]): MrlValue {
    const library = new Map(this.available.map((edge) => [edge.id, edge]));
    let path = this.initial;
    for (const [index, id] of chain.entries()) {
      const edge = library.get(id);
      const prefix = "via[" + index + "]：" + id;
      if (!edge) return this.result("via-invalid", path, prefix + " 不在转换库中（不存在、不是 converter 或已退役）", chain);
      if (!this.subtype(path.type, edge.from)) return this.result("via-invalid", path, prefix + " 接受 " + edge.from + "，但此处的类型是 " + path.type, chain);
      const missing = this.missing(edge.requires, path.properties);
      if (missing.length) return this.result("via-invalid", path, prefix + " 要求的属性 {" + missing.join(", ") + "} 在此处没有被覆盖", chain);
      path = this.step(path, edge);
    }
    if (!this.subtype(path.type, this.destination)) {
      return this.result("via-invalid", path, "转换链的终点类型 " + path.type + " 不是 " + this.destination + " 的子类型", chain);
    }
    return this.result(this.missing(this.requires, path.properties).length ? "contract" : "convert", path);
  }
}

const resultType = record({
  status: STRING, chain: list(STRING), type: STRING, props: list(STRING), missing: list(STRING), lossless: BOOL, detail: STRING,
});

function signature(arity: number): FunctionSignature {
  return {
    minArgs: arity, maxArgs: arity, returns: resultType,
    accepts: (args) => args.every((type, index) => isPrimitive(type, "dyn")
      || (index === 3 || index === 5 ? isPrimitive(type, "string") : type.kind === "list")),
  };
}

export const resolutionFunctions = {
  signatures: { resolve: signature(7), resolveVia: signature(8) },
  functions: {
    resolve: (lib: MrlValue, subtypes: MrlValue, always: MrlValue, upstream: MrlValue, guarantees: MrlValue, downstream: MrlValue, requires: MrlValue) =>
      new Resolver(lib, subtypes, always, upstream, guarantees, downstream, requires).auto(),
    resolveVia: (lib: MrlValue, subtypes: MrlValue, always: MrlValue, upstream: MrlValue, guarantees: MrlValue, downstream: MrlValue, requires: MrlValue, chain: MrlValue) =>
      new Resolver(lib, subtypes, always, upstream, guarantees, downstream, requires).explicit(strings(chain)),
  },
};
