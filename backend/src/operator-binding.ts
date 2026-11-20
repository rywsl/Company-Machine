import { officialHarnesses, type HarnessDefinition, type OfficialHarnessName } from "./harness";
import type { BusinessUnit, HarnessProfile } from "./models";

export interface OperatorBinding {
  harnessRuntime: OfficialHarnessName;
  model?: string;
}

type Resolution =
  | { definition: HarnessDefinition; profile: HarnessProfile; model: string; explicitModel: boolean }
  | { reason: "operator-harness-not-configured" | "operator-harness-unsupported" | "operator-harness-ambiguous" | "operator-model-invalid" };

const definitions = new Map(officialHarnesses().map((definition) => [definition.id as string, definition]));
const legacyRuntime = /^(?:OperatorRuntime:)?([a-z][a-z0-9-]*)(?:@[0-9]+)?$/i;

function storedHarness(runtime: string): HarnessDefinition | undefined {
  const id = legacyRuntime.exec(runtime.trim())?.[1].toLowerCase();
  return id ? definitions.get(id) : undefined;
}

function modelId(value: unknown): string | undefined {
  if (typeof value !== "string" || /[\u0000-\u001f\u007f]/.test(value)) return undefined;
  const id = value.trim();
  return id && !id.startsWith("-") ? id : undefined;
}

export function parseOperatorBinding(value: unknown): OperatorBinding | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const input = value as Record<string, unknown>;
  if (Object.keys(input).some((key) => key !== "harness_runtime" && key !== "model")) return undefined;
  const definition = typeof input.harness_runtime === "string" ? definitions.get(input.harness_runtime) : undefined;
  if (!definition) return undefined;
  const model = input.model === null || input.model === undefined ? undefined : modelId(input.model);
  if (!model && input.model != null) return undefined;
  return { harnessRuntime: definition.id, model: model === "default" ? undefined : model };
}

export function resolveOperatorBinding(
  unit: Pick<BusinessUnit, "harnessRuntime" | "model">,
  profiles: readonly HarnessProfile[],
): Resolution {
  const requested = unit.harnessRuntime ? storedHarness(unit.harnessRuntime) : undefined;
  if (unit.harnessRuntime && !requested) return { reason: "operator-harness-unsupported" };
  const candidates = profiles.flatMap((profile) => {
    if (profile.role !== "operator") return [];
    const definition = storedHarness(profile.runtime);
    if (!definition || (requested && definition.id !== requested.id)) return [];
    return [{ profile, definition }];
  });
  const selected = requested
    ? candidates.find(({ profile }) => profile.id === `operator:${requested.id}`)
    : undefined;
  const resolved = selected ?? (candidates.length === 1 ? candidates[0] : undefined);
  if (!resolved) return { reason: candidates.length ? "operator-harness-ambiguous" : "operator-harness-not-configured" };
  const explicitModel = unit.model !== undefined && unit.model.trim() !== "" && unit.model.trim() !== "default";
  const model = modelId(explicitModel ? unit.model : resolved.profile.model);
  return model ? { ...resolved, model, explicitModel } : { reason: "operator-model-invalid" };
}

export function operatorBindingView(unit: BusinessUnit, profiles: readonly HarnessProfile[]) {
  const resolved = resolveOperatorBinding(unit, profiles);
  const model = unit.model?.trim();
  return {
    businessSystemId: unit.businessSystemId,
    harnessRuntime: "reason" in resolved ? (unit.harnessRuntime && storedHarness(unit.harnessRuntime)?.id) || undefined : resolved.definition.id,
    model: model && model !== "default" ? model : undefined,
    ...("reason" in resolved ? resolved : { effectiveModel: resolved.model, profileId: resolved.profile.id }),
  };
}
