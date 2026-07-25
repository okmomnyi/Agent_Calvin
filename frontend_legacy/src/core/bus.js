// Tiny event bus decoupling panels/HUD from transport and from each other.
// No framework, no build step (A0) — just EventTarget with a friendlier API.

class Bus extends EventTarget {
  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  // Returns an unsubscribe function.
  on(type, handler) {
    const wrapped = (e) => handler(e.detail);
    this.addEventListener(type, wrapped);
    return () => this.removeEventListener(type, wrapped);
  }
}

export const bus = new Bus();
