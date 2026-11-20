export const MIN_INT64 = -(1n << 63n);
export const MAX_INT64 = (1n << 63n) - 1n;

export type MrlInt = number | bigint;
export type MrlNumber = MrlInt | IntegralFloat;

// Number preserves the kind of fractional values but collapses 1.0 to 1.
export class IntegralFloat {
  constructor(readonly value: number) {
    Object.freeze(this);
  }

  valueOf(): number {
    return this.value;
  }
}

export function narrowInt(value: bigint): MrlInt {
  const native = Number(value);
  return Number.isSafeInteger(native) ? native : value;
}

export function floatValue(value: number): number | IntegralFloat {
  return Number.isInteger(value) ? new IntegralFloat(value === 0 ? 0 : value) : value;
}

export function isInt(value: unknown): value is MrlInt {
  return typeof value === "bigint" || (typeof value === "number" && Number.isSafeInteger(value));
}

export function isMrlNumber(value: unknown): value is MrlNumber {
  return isInt(value) || value instanceof IntegralFloat || (typeof value === "number" && Number.isFinite(value));
}

export function nativeNumber(value: MrlNumber): number | bigint {
  return value instanceof IntegralFloat ? value.value : value;
}

export function parseNumericScalar(text: string): MrlNumber {
  return /^[+-]?[0-9]+$/.test(text) ? narrowInt(BigInt(text)) : floatValue(Number(text));
}

export function mrlJsonReplacer(_key: string, value: unknown): unknown {
  const rawJson = (JSON as typeof JSON & { rawJSON(text: string): unknown }).rawJSON;
  if (typeof value === "bigint") return rawJson(value.toString());
  if (value instanceof IntegralFloat) {
    const text = value.value.toString();
    return rawJson(text.includes("e") ? text : text + ".0");
  }
  return value;
}
