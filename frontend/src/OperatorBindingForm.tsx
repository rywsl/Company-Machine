import { useEffect, useState, type FormEvent } from "react";
import { updateOperatorBinding } from "./api";
import type { HarnessProfile, HarnessRecord, OperatorBindingView } from "./types";

interface Props {
  unitId: string;
  binding: OperatorBindingView;
  harnesses: HarnessRecord[];
  profiles: HarnessProfile[];
  onConfigure: (runtime?: string) => void;
  onSaved: (unitId: string, binding: OperatorBindingView) => void;
}

const status: Record<string, string> = {
  "operator-harness-ambiguous": "请选择业务 Operator 要使用的 Harness",
  "operator-harness-not-configured": "此 Harness 尚未配置",
  "operator-harness-unsupported": "请选择受支持的官方 Harness",
  "operator-model-invalid": "此 Harness 的模型配置无效",
};

export function OperatorBindingForm({ unitId, binding, harnesses, profiles, onConfigure, onSaved }: Props) {
  const [runtime, setRuntime] = useState(binding.harnessRuntime ?? "");
  const [model, setModel] = useState(binding.model ?? "");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const profile = profiles.find((item) => item.role === "operator" && item.runtime === runtime);
  const runtimeAvailable = harnesses.some((item) => item.id === runtime);

  useEffect(() => {
    setRuntime(binding.harnessRuntime ?? "");
    setModel(binding.model ?? "");
  }, [binding.harnessRuntime, binding.model]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!runtimeAvailable || saving) return;
    setSaving(true);
    setMessage("");
    try {
      const { binding: updated } = await updateOperatorBinding(binding.businessSystemId, unitId, runtime, model.trim() || undefined);
      onSaved(unitId, updated);
      setMessage("Operator 绑定已保存");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "绑定保存失败");
    } finally {
      setSaving(false);
    }
  };

  return <div className="detail-section">
    <form onSubmit={submit}>
      <label className="config-field">
        <span>官方 Harness</span>
        <select value={runtimeAvailable ? runtime : ""} onChange={(event) => setRuntime(event.target.value)} required>
          <option value="">选择 Harness</option>
          {harnesses.map((harness) => <option key={harness.id} value={harness.id}>{harness.name}</option>)}
        </select>
      </label>
      <label className="config-field">
        <span>模型</span>
        <input value={model} placeholder={profile?.model || "使用 Harness 默认模型"} onChange={(event) => setModel(event.target.value)} />
      </label>
      <div className="config-actions">
        <button className="primary-button" type="submit" disabled={saving || !runtimeAvailable}>{saving ? "保存中…" : "保存绑定"}</button>
        <button className="secondary-button" type="button" onClick={() => onConfigure(runtimeAvailable ? runtime : undefined)}>配置 Harness</button>
      </div>
    </form>
    {binding.effectiveModel && <div className="context-line"><span>当前生效模型</span><strong>{binding.effectiveModel}</strong></div>}
    {binding.reason && status[binding.reason] && <div className="context-note">{status[binding.reason]}</div>}
    {message && <div className="context-note" role="status">{message}</div>}
  </div>;
}
