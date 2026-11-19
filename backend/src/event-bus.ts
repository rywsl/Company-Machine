import type { Event } from "./models";
import { SqliteStore } from "./db";

export type EventListener = (event: Event) => void;

export class EventBus {
  private readonly listeners = new Set<EventListener>();
  constructor(private readonly store: SqliteStore) {}

  publish<T>(type: string, data: T): Event<T> {
    const event = this.store.appendEvent(type, data) as Event<T>;
    for (const listener of this.listeners) listener(event);
    return event;
  }

  after(lastEventId?: string | number): Event[] {
    const seq = typeof lastEventId === "number" ? lastEventId : Number(lastEventId ?? 0);
    return this.store.eventsAfter(Number.isFinite(seq) ? seq : 0);
  }

  subscribe(listener: EventListener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

