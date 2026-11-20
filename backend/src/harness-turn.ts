import { spawnOfficialProcess, type HarnessEvent, type HarnessSession, type HarnessSessionRequest, type OfficialSpawn } from "./harness";

export const defaultSessionTimeoutSeconds = 600;

export function validSessionTimeout(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 2_147_483;
}

export function waitForHarness<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const interrupt = () => reject(signal.reason);
    work.then((value) => {
      signal.removeEventListener("abort", interrupt);
      resolve(value);
    }, (error) => {
      signal.removeEventListener("abort", interrupt);
      reject(error);
    });
    if (signal.aborted) interrupt();
    else signal.addEventListener("abort", interrupt, { once: true });
  });
}

/** Owns one official turn, including startup before a session is available. */
export class HarnessTurn {
  private readonly cancellation = new AbortController();
  private readonly deadline: ReturnType<typeof setTimeout>;
  private killProcess?: () => Promise<void>;
  private session?: HarnessSession;
  private closing?: Promise<void>;

  constructor(timeoutSeconds: number) {
    this.deadline = setTimeout(() => this.cancellation.abort(new Error("SESSION_TIMEOUT")), timeoutSeconds * 1000);
  }

  wait<T>(work: Promise<T>): Promise<T> {
    return waitForHarness(work, this.cancellation.signal);
  }

  ownCliProcess(spawn?: OfficialSpawn): OfficialSpawn {
    return this.ownProcess(spawn ?? ((argv, options) => spawnOfficialProcess(argv, { ...options, stdin: undefined })));
  }

  ownProcess<Args extends unknown[], Child extends { kill(): void | Promise<void> }>(spawn: (...args: Args) => Child | Promise<Child>) {
    return async (...args: Args): Promise<Child> => {
      const { signal } = this.cancellation;
      signal.throwIfAborted();
      const child = await spawn(...args);
      let killed: Promise<void> | undefined;
      const kill = () => killed ??= Promise.resolve().then(() => child.kill());
      this.killProcess = kill;
      if (signal.aborted) {
        void kill().catch(() => undefined);
        throw signal.reason;
      }
      return { ...child, kill };
    };
  }

  start(transport: { start(request: HarnessSessionRequest, signal?: AbortSignal): Promise<HarnessSession> }, request: HarnessSessionRequest): Promise<HarnessSession> {
    const { signal } = this.cancellation;
    signal.throwIfAborted();
    return this.wait(transport.start(request, signal).then((session) => {
      if (signal.aborted) {
        void session.close().catch(() => undefined);
        throw signal.reason;
      }
      this.session = session;
      return session;
    }));
  }

  async *events(session: HarnessSession): AsyncIterable<HarnessEvent> {
    const reader = session.events()[Symbol.asyncIterator]();
    try {
      for (;;) {
        const next = await this.wait(reader.next());
        this.cancellation.signal.throwIfAborted();
        if (next.done) return;
        yield next.value;
      }
    } finally {
      void reader.return?.().catch(() => undefined);
    }
  }

  close(): Promise<void> {
    return this.closing ??= this.stop();
  }

  private async stop(): Promise<void> {
    clearTimeout(this.deadline);
    this.cancellation.abort(new Error("SESSION_CANCELLED"));
    // A slow process-tree or relay cleanup cannot block later turns indefinitely.
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        (this.session?.close() ?? this.killProcess?.())?.catch(() => undefined),
        new Promise<void>((resolve) => { timer = setTimeout(resolve, 4000); }),
      ]);
    } finally { clearTimeout(timer); }
  }
}
