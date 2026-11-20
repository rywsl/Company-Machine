import { useEffect, useId, useState } from "react";
import { api } from "./api";

const statuses = {
  planned: { label: "已计划", tone: "warn" },
  running: { label: "转换中", tone: "warn" },
  completed: { label: "已完成", tone: "good" },
  failed: { label: "失败", tone: "bad" },
} as const;

interface Conversion {
  id: string;
  kind: string;
  summary: string;
  status: keyof typeof statuses;
  inputs: string[];
  outputs: string[];
}

function ConversionReferences({ label, references }: { label: string; references: string[] }) {
  return <div className="conversion-references">
    <span className="conversion-label">{label}</span>
    {references.length ? <ul>
      {references.map((reference, index) =>
        <li key={`${reference}-${index}`} className="asset-flow-node">{reference}</li>)}
    </ul> : <span className="conversion-empty">未登记</span>}
  </div>;
}

function ConversionFlow({ conversion }: { conversion: Conversion }) {
  const status = statuses[conversion.status];
  return <li className="conversion-flow">
    <ConversionReferences label="输入引用" references={conversion.inputs} />
    <b className="conversion-arrow" aria-hidden="true">↓</b>
    <div className="conversion-step">
      <h3>{conversion.summary.trim() || conversion.kind}</h3>
      <span className={`status-badge ${status.tone}`}>{status.label}</span>
    </div>
    <b className="conversion-arrow" aria-hidden="true">↓</b>
    <ConversionReferences label="输出引用" references={conversion.outputs} />
  </li>;
}

export function WorkareaConversions({ businessSystemId, revision }: { businessSystemId: string; revision: number }) {
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState<Conversion[]>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const url = `/api/v1/business-systems/${encodeURIComponent(businessSystemId)}/conversions`;
    void api<Conversion[]>(url, { signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      setRecords(result);
      setFailed(false);
    }).catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    });
    return () => controller.abort();
  }, [businessSystemId, open, revision]);

  const openPanel = () => {
    setRecords(undefined);
    setFailed(false);
    setOpen(true);
  };

  return <>
    <button type="button" className="secondary-button" aria-expanded={open} aria-controls={open ? panelId : undefined}
      onClick={open ? () => setOpen(false) : openPanel}>物料转换</button>
    {open && <aside id={panelId} className="context-drawer" aria-labelledby={`${panelId}-title`}>
      <button type="button" className="icon-button" onClick={() => setOpen(false)} aria-label="关闭物料转换">×</button>
      <h2 id={`${panelId}-title`}>物料转换</h2>
      {failed && <p role="status">无法读取物料转换。</p>}
      {!failed && !records && <p role="status">正在读取物料转换…</p>}
      {!failed && records?.length === 0 && <p>当前厂区还没有转换记录。</p>}
      {!!records?.length && <ul className="conversion-flows">
        {records.map((conversion) => <ConversionFlow key={conversion.id} conversion={conversion} />)}
      </ul>}
    </aside>}
  </>;
}
