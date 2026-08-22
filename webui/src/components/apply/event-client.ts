export type ApplyEvent = {
  event: string;
  opportunity_id?: string;
  job_id?: string;
  agent_run_id?: string;
  import_run_id?: string;
  status?: string;
};

export function subscribeApplyEvents(
  onEvent: (event: ApplyEvent) => void,
): () => void {
  let closed = false;
  let socket: WebSocket | null = null;
  let retryTimer: number | null = null;

  const connect = () => {
    if (closed) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${window.location.host}/ws/apply`);
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(String(message.data)) as ApplyEvent;
        if (event.event !== "apply_connected") onEvent(event);
      } catch {
        // Ignore malformed invalidation events.
      }
    };
    socket.onclose = () => {
      if (!closed) retryTimer = window.setTimeout(connect, 1500);
    };
  };

  connect();
  return () => {
    closed = true;
    if (retryTimer !== null) window.clearTimeout(retryTimer);
    socket?.close();
  };
}
