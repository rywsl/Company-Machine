import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { Approvals } from "../src/legacy/approvals";
import { FactoryError } from "../src/legacy/errors";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { JobStore } from "../src/legacy/jobs";
import { loadSnapshot } from "../src/legacy/snapshot";

function expectCode(run: () => unknown, code: string): void {
  try {
    run();
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe(code);
    return;
  }
  throw new Error("Expected " + code);
}

describe("Bun in-run approval storage", () => {
  test("applies the station cost rule and isolates replay IDs by Job and attempt", async () => {
    const db = new Database(":memory:");
    const clock = new FakeClock();
    const ids = new CounterIds();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const jobs = new JobStore(db, clock, ids);
      const approvals = new Approvals({ db, clock, ids }, snapshot, jobs);
      const job = jobs.create({ station: "Station:writer@1", epoch: snapshot.epoch, inputs: {}, params: {} });
      jobs.transition(job.id, "PROVISIONING");
      jobs.transition(job.id, "RUNNING");
      const request = { jobId: job.id, attempt: 1, requestId: "r-1", payload: { kind: "expense", summary: "Draft", cost_usd: 0.5 } };
      const approved = approvals.openRequest(request);
      expect(approved).toMatchObject({ status: "approved", route: "auto", decidedBy: "auto", requestId: "r-1" });
      expect(approvals.ctlResult(approved)).toEqual({ decision: "approve", code: "", note: "" });
      expectCode(() => approvals.openRequest({ ...request, payload: { ...request.payload, cost_usd: 1 } }), "APPROVAL_PAYLOAD_INVALID");
      expect(approvals.openRequest(request)).toEqual(approved);
      expect(approvals.byRequest(job.id, 1, "r-1")).toEqual(approved);
      expect(approvals.list({ jobId: job.id })).toHaveLength(1);
      expect(jobs.get(job.id).state).toBe("RUNNING");

      jobs.transition(job.id, "FAILED");
      jobs.transition(job.id, "PENDING");
      jobs.transition(job.id, "PROVISIONING");
      jobs.transition(job.id, "RUNNING");
      const pending = approvals.openRequest({
        ...request, attempt: 2, payload: { ...request.payload, summary: "Draft token=example", cost_usd: 1 },
      });
      expect(pending).toMatchObject({ attempt: 2, route: "foreman", status: "pending", payload: { summary: "Draft token=[REDACTED]" } });
      expect(approvals.ctlResult(pending)).toBeNull();
      expectCode(() => approvals.applyDecision(pending.id, "approve", { by: "auto" }), "APPROVAL_BY_INVALID");
      const reopened = new Approvals({ db, clock, ids }, snapshot, jobs);
      const denied = reopened.applyDecision(pending.id, "deny", { by: "operator", note: "password=example" });
      expect(reopened.ctlResult(denied)).toEqual({ decision: "deny", code: "APPROVAL_DENIED", note: "password=[REDACTED]" });
      expectCode(() => reopened.applyDecision(pending.id, "approve", { by: "operator" }), "APPROVAL_STATE_INVALID");
      const events = db.query<{ kind: string }>(
        "SELECT kind FROM job_events WHERE job_id = ? AND kind IN ('approval.requested', 'job.approval') ORDER BY rowid",
      ).all(job.id);
      expect(events.map(({ kind }) => kind)).toEqual(["approval.requested", "job.approval", "approval.requested", "job.approval"]);
    } finally { db.close(); }
  });

  test("settles timeouts at the deadline and denies stations without an in-run rule", async () => {
    const db = new Database(":memory:");
    const clock = new FakeClock();
    const ids = new CounterIds();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const jobs = new JobStore(db, clock, ids);
      const approvals = new Approvals({ db, clock, ids }, snapshot, jobs);
      const start = (station: string) => {
        const job = jobs.create({ station, epoch: snapshot.epoch, inputs: {}, params: {} });
        jobs.transition(job.id, "PROVISIONING");
        return jobs.transition(job.id, "RUNNING");
      };
      snapshot.docs["Station:writer@1"].spec.approval.timeout = "1s";
      snapshot.docs["Station:writer@1"].spec.approval.in_run = { auto_if: "params.missing > 0", else: "human" };
      const job = start("Station:writer@1");
      const other = start("Station:writer@1");
      const payload = { kind: "expense", summary: "Draft", cost_usd: 1 };
      const first = approvals.openRequest({ jobId: job.id, attempt: 1, requestId: "r-1", payload });
      const second = approvals.openRequest({ jobId: other.id, attempt: 1, requestId: "r-1", payload });
      expect(first).toMatchObject({ route: "human", note: "APPROVAL_EVAL_ERROR" });
      expect(second.id).not.toBe(first.id);
      expect(approvals.expireDue({ jobId: job.id, attempt: 1 })).toEqual([]);
      clock.advance(1);
      expectCode(() => approvals.applyDecision(first.id, "approve", { by: "operator" }), "APPROVAL_TIMEOUT");
      expect(approvals.ctlResult(approvals.get(first.id))).toEqual({
        decision: "timeout", code: "APPROVAL_TIMEOUT", note: "APPROVAL_TIMEOUT",
      });
      expect(approvals.expireDue({ jobId: job.id, attempt: 1 })).toEqual([]);
      expect(approvals.expireDue()).toEqual([second.id]);
      const ending = approvals.openRequest({ jobId: job.id, attempt: 1, requestId: "r-2", payload });
      expect(approvals.closePending({ jobId: job.id, attempt: 1 }, "Attempt interrupted")).toEqual([ending.id]);
      expect(approvals.get(ending.id)).toMatchObject({ status: "denied", decidedBy: "runner", note: "Attempt interrupted" });
      expect(approvals.closePending({ jobId: job.id, attempt: 1 }, "Attempt interrupted")).toEqual([]);

      const noRule = start("Station:upper@1");
      const denied = approvals.openRequest({ jobId: noRule.id, attempt: 1, payload });
      expect(denied).toMatchObject({ status: "denied", route: "missing", decidedBy: "auto" });
      expect(approvals.ctlResult(denied)).toMatchObject({ decision: "deny", code: "APPROVAL_RULE_MISSING" });
      expect(approvals.ctlResult({ ...denied, note: "APPROVAL_DENIED" })?.code).toBe("APPROVAL_RULE_MISSING");
      expect(approvals.list({ jobId: noRule.id, status: "pending" })).toEqual([]);
      expect(Date.parse(denied.deadline!) - Date.parse(denied.createdAt)).toBe(600_000);
      expect(jobs.get(noRule.id).state).toBe("RUNNING");
    } finally { db.close(); }
  });

  test("denies in-run review without a quorum and requires distinct Runtime votes when configured", async () => {
    const db = new Database(":memory:");
    const clock = new FakeClock();
    const ids = new CounterIds();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
      const station = "Station:quote@1";
      snapshot.docs[station].spec.approval.in_run = "review";
      const jobs = new JobStore(db, clock, ids);
      const approvals = new Approvals({ db, clock, ids }, snapshot, jobs);
      const job = jobs.create({ station, epoch: snapshot.epoch, inputs: {}, params: {} });
      jobs.transition(job.id, "PROVISIONING");
      jobs.transition(job.id, "RUNNING");
      const request = (requestId: string) => ({
        jobId: job.id, attempt: 1, requestId, payload: { kind: "expense", summary: "Publish", cost_usd: 1 },
      });

      const noQuorum = approvals.openRequest(request("missing-quorum"));
      expect(noQuorum).toMatchObject({ phase: "in_run", route: "review", status: "denied" });
      expect(approvals.ctlResult(noQuorum)).toMatchObject({ decision: "deny", code: "REVIEW_QUORUM_UNSET" });
      expect(approvals.openRequest(request("missing-quorum"))).toEqual(noQuorum);
      expect(approvals.list({ jobId: job.id, status: "pending" })).toEqual([]);
      expect(jobs.get(job.id).state).toBe("RUNNING");

      snapshot.result.registry.constitution[0].source += '\nlimit("approval_review_quorum", 2.0).\n';
      const pending = approvals.openRequest(request("approval"));
      expect(pending.status).toBe("pending");
      expectCode(() => approvals.applyDecision(pending.id, "approve", { by: "operator" }), "APPROVAL_BY_INVALID");
      const first = approvals.applyDecision(pending.id, "approve", { by: "runtime:first@1" });
      expect(first.status).toBe("pending");
      expect(approvals.ctlResult(first)).toBeNull();
      expectCode(() => approvals.applyDecision(pending.id, "approve", { by: "runtime:first@2" }), "APPROVAL_STATE_INVALID");
      const approved = approvals.applyDecision(pending.id, "approve", { by: "runtime:second@1" });
      expect(approved).toMatchObject({ status: "approved", decidedBy: "runtime:first@1,runtime:second@1" });
      expect(approvals.votes(pending.id).map(({ runtime }) => runtime)).toEqual(["first", "second"]);

      const split = approvals.openRequest(request("split"));
      approvals.applyDecision(split.id, "approve", { by: "runtime:first@1" });
      const denied = approvals.applyDecision(split.id, "deny", { by: "runtime:second@1" });
      expect(approvals.ctlResult(denied)).toMatchObject({ decision: "deny", code: "APPROVAL_REVIEW_SPLIT" });
    } finally { db.close(); }
  });
});

describe("Bun proposal approval storage", () => {
  test("persists the only proposal per attempt and applies the proposal cost rule", async () => {
    const db = new Database(":memory:");
    const clock = new FakeClock();
    const ids = new CounterIds();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
      const jobs = new JobStore(db, clock, ids);
      const approvals = new Approvals({ db, clock, ids }, snapshot, jobs);
      const start = () => {
        const job = jobs.create({ station: "Station:quote@1", epoch: snapshot.epoch, inputs: {}, params: {} });
        jobs.transition(job.id, "PROVISIONING");
        return jobs.transition(job.id, "RUNNING");
      };
      const automatic = start();
      const payload = { summary: "Quote", cost_estimate_usd: 1, time_estimate_s: 20 };
      const approved = approvals.openProposal({ jobId: automatic.id, attempt: 1, payload });
      expect(approved).toMatchObject({ phase: "propose", route: "auto", status: "approved" });
      expect(approvals.openProposal({ jobId: automatic.id, attempt: 1, payload })).toEqual(approved);
      expectCode(() => approvals.openProposal({ jobId: automatic.id, attempt: 1, payload: { ...payload, summary: "Changed" } }), "PROPOSAL_INVALID");

      const manual = start();
      const review = approvals.openProposal({
        jobId: manual.id, attempt: 1,
        payload: { ...payload, cost_estimate_usd: 2, plan: { api_key: "example", item: "report" } },
      });
      expect(review).toMatchObject({ phase: "propose", route: "foreman", status: "pending" });
      expect(review.payload).toMatchObject({ plan: { api_key: "[REDACTED]", item: "report" } });
      const resumed = new Approvals({ db, clock, ids }, snapshot, jobs);
      expect(resumed.proposal(manual.id, 1)).toEqual(review);
      const denied = resumed.applyDecision(review.id, "deny", { by: "operator" });
      expect(denied.status).toBe("denied");
      expect(resumed.ctlResult(denied)?.code).toBe("APPROVAL_DENIED");
      expect(resumed.forProvenance(manual.id)).toEqual([{
        id: review.id, phase: "propose", decision: "deny", decided_by: "operator", route: "foreman", cost_usd: 2,
      }]);
      expect(resumed.proposal(automatic.id, 2)).toBeNull();
    } finally { db.close(); }
  });

  test("uses a configured distinct-Runtime quorum and settles proposal deadlines", async () => {
    const db = new Database(":memory:");
    const clock = new FakeClock();
    const ids = new CounterIds();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
      const jobs = new JobStore(db, clock, ids);
      const approvals = new Approvals({ db, clock, ids }, snapshot, jobs);
      const station = snapshot.docs["Station:quote@1"].spec;
      station.approval.propose = "review";
      const start = () => {
        const job = jobs.create({ station: "Station:quote@1", epoch: snapshot.epoch, inputs: {}, params: {} });
        jobs.transition(job.id, "PROVISIONING");
        return jobs.transition(job.id, "RUNNING");
      };
      const make = (jobId: string) => approvals.openProposal({
        jobId, attempt: 1, payload: { summary: "Quote", cost_estimate_usd: 2, time_estimate_s: 20 },
      });
      const review = start();
      expectCode(() => make(review.id), "REVIEW_QUORUM_UNSET");
      snapshot.result.registry.constitution[0].source += '\nlimit("approval_review_quorum", 2.0).\n';
      const pending = make(review.id);
      expectCode(() => approvals.applyDecision(pending.id, "approve", { by: "operator" }), "APPROVAL_BY_INVALID");
      expect(approvals.applyDecision(pending.id, "approve", { by: "runtime:first@1" }).status).toBe("pending");
      expectCode(() => approvals.applyDecision(pending.id, "approve", { by: "runtime:first@2" }), "APPROVAL_STATE_INVALID");
      expect(approvals.applyDecision(pending.id, "approve", { by: "runtime:second@1" })).toMatchObject({
        status: "approved", decidedBy: "runtime:first@1,runtime:second@1",
      });
      const split = make(start().id);
      expect(approvals.ctlResult(approvals.applyDecision(split.id, "deny", { by: "runtime:first@1" }))?.code)
        .toBe("APPROVAL_REVIEW_SPLIT");

      const waiting = make(start().id);
      clock.advance(1);
      expect(approvals.expireDue()).toEqual([waiting.id]);
      expect(approvals.get(waiting.id)).toMatchObject({ status: "denied", decidedBy: "timeout" });
      delete station.approval.timeout;
      const indefinite = make(start().id);
      expect(indefinite.deadline).toBeNull();
      clock.advance(100);
      expect(approvals.expireDue()).toEqual([]);
    } finally { db.close(); }
  });
});
