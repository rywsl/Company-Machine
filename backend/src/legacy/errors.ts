export class FactoryError extends Error {
  constructor(readonly code: string, message: string, readonly detail: Record<string, unknown> = {}) {
    super(message);
    this.name = "FactoryError";
  }
}
