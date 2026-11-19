/** Static MRL types used by the Bun checker. The shape mirrors MRL 1.0. */

export type PrimitiveName = "bool" | "int" | "float" | "num" | "string" | "null" | "dyn";

export type MrlType =
  | { kind: "primitive"; name: PrimitiveName }
  | { kind: "list"; element: MrlType }
  | { kind: "map"; element: MrlType }
  | { kind: "record"; fields: Record<string, { type: MrlType; required: boolean }>; open: boolean };

export const BOOL: MrlType = { kind: "primitive", name: "bool" };
export const INT: MrlType = { kind: "primitive", name: "int" };
export const FLOAT: MrlType = { kind: "primitive", name: "float" };
export const NUM: MrlType = { kind: "primitive", name: "num" };
export const STRING: MrlType = { kind: "primitive", name: "string" };
export const NULL: MrlType = { kind: "primitive", name: "null" };
export const DYN: MrlType = { kind: "primitive", name: "dyn" };

export const list = (element: MrlType): MrlType => ({ kind: "list", element });
export const map = (element: MrlType): MrlType => ({ kind: "map", element });
export const record = (fields: Record<string, MrlType>, required = Object.keys(fields), open = false): MrlType => {
  const requiredSet = new Set(required);
  return { kind: "record", fields: Object.fromEntries(Object.entries(fields).map(([name, type]) => [name, { type, required: requiredSet.has(name) }])), open };
};

export function typeName(type: MrlType): string {
  if (type.kind === "primitive") return type.name;
  if (type.kind === "list") return `list<${typeName(type.element)}>`;
  if (type.kind === "map") return `map<${typeName(type.element)}>`;
  const fields = Object.entries(type.fields).sort(([a], [b]) => a.localeCompare(b)).map(([name, value]) => `${name}${value.required ? "" : "?"}: ${typeName(value.type)}`);
  return `{${fields.join(", ")}${type.open ? `${fields.length ? ", " : ""}...` : ""}}`;
}

export function isNumeric(type: MrlType): boolean {
  return type.kind === "primitive" && ["int", "float", "num"].includes(type.name);
}

export function isPrimitive(type: MrlType, name: PrimitiveName): boolean {
  return type.kind === "primitive" && type.name === name;
}

export function numericResult(a: MrlType, b: MrlType): MrlType {
  if (isPrimitive(a, "int") && isPrimitive(b, "int")) return INT;
  if (isPrimitive(a, "float") || isPrimitive(b, "float")) return FLOAT;
  return NUM;
}

export function lub(a: MrlType, b: MrlType): MrlType {
  if (typeName(a) === typeName(b)) return a;
  if (isPrimitive(a, "null") || isPrimitive(b, "null")) return DYN;
  if (isNumeric(a) && isNumeric(b)) return NUM;
  if (a.kind === "list" && b.kind === "list") return list(lub(a.element, b.element));
  if ((a.kind === "map" || a.kind === "record") && (b.kind === "map" || b.kind === "record")) return map(DYN);
  return DYN;
}

export type Compatibility = "yes" | "maybe" | "no";

export function compatible(source: MrlType, target: MrlType): Compatibility {
  if (target === DYN || typeName(source) === typeName(target)) return "yes";
  if (source === DYN) return "maybe";
  if (isPrimitive(target, "float") && isPrimitive(source, "int")) return "yes";
  if (isPrimitive(target, "num") && isNumeric(source)) return "yes";
  if (isPrimitive(source, "num") && ["int", "float"].some((name) => isPrimitive(target, name as PrimitiveName))) return "maybe";
  if (source.kind === "list" && target.kind === "list") return compatible(source.element, target.element);
  if (source.kind === "map" && target.kind === "map") return compatible(source.element, target.element);
  return "no";
}

export function comparable(a: MrlType, b: MrlType): boolean {
  if (a === DYN || b === DYN || a === NULL || b === NULL || typeName(a) === typeName(b)) return true;
  if (isNumeric(a) && isNumeric(b)) return true;
  if (a.kind === "list" && b.kind === "list") return comparable(a.element, b.element);
  return (a.kind === "map" || a.kind === "record") && (b.kind === "map" || b.kind === "record");
}

const simple: Record<string, MrlType> = { bool: BOOL, int: INT, float: FLOAT, num: NUM, string: STRING, null: NULL, dyn: DYN, any: DYN };

export function parseType(source: string): MrlType {
  let position = 0;
  const skip = () => { while (source[position] === " ") position += 1; };
  const expect = (text: string) => { skip(); if (!source.startsWith(text, position)) throw new Error(`invalid MRL type near ${position}, expected ${text}`); position += text.length; };
  const identifier = () => {
    skip();
    const start = position;
    if (!/[A-Za-z_]/.test(source[position] ?? "")) throw new Error(`invalid MRL type near ${position}, expected identifier`);
    position += 1;
    while (/[A-Za-z0-9_]/.test(source[position] ?? "")) position += 1;
    return source.slice(start, position);
  };
  const parse = (): MrlType => {
    skip();
    if (source[position] === "{") {
      position += 1;
      const fields: Record<string, MrlType> = {};
      const required: string[] = [];
      let open = false;
      skip();
      while (source[position] !== "}") {
        skip();
        if (source.startsWith("...", position)) { position += 3; open = true; break; }
        const name = identifier();
        skip();
        const optional = source[position] === "?";
        if (optional) position += 1;
        expect(":");
        fields[name] = parse();
        if (!optional) required.push(name);
        skip();
        if (source[position] !== ",") break;
        position += 1;
        skip();
        if (source[position] === "}") throw new Error("record types do not allow trailing commas");
      }
      expect("}");
      return record(fields, required, open);
    }
    const name = identifier();
    if (name === "list" || name === "map") { expect("<"); const element = parse(); expect(">"); return name === "list" ? list(element) : map(element); }
    if (simple[name]) return simple[name];
    throw new Error(`unknown MRL type ${name}`);
  };
  const result = parse();
  skip();
  if (position !== source.length) throw new Error(`invalid MRL type near ${position}`);
  return result;
}

export type TypeEnvironment = Record<string, MrlType | string>;

export function normalizeEnvironment(environment: TypeEnvironment = {}): Record<string, MrlType> {
  return Object.fromEntries(Object.entries(environment).map(([name, type]) => [name, typeof type === "string" ? parseType(type) : type]));
}
