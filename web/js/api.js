/* The API client.
 *
 * One place that knows about the network, so every screen can be written as a pure
 * function of data it is handed. Two things worth noting:
 *
 * **Failures are values.** Every call returns `{ok, data, error}` rather than
 * throwing. A dashboard whose panel disappears because a fetch rejected is worse
 * than one that says which panel could not load and why - and the empty states are
 * written to say exactly that.
 *
 * **Nothing is invented on failure.** There is no placeholder data, no "example"
 * mode, no last-known-good cache pretending to be live. If the API is not there the
 * screen says so and prints the command that starts it.
 */

const BASE = window.TRIVENI_API_BASE ?? "";

async function request(path, options = {}) {
  try {
    const response = await fetch(BASE + path, {
      headers: { Accept: "application/json" },
      ...options,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      return { ok: false, data: null, error: `${path} returned non-JSON` };
    }
    if (!response.ok) {
      const detail = data?.detail ?? `HTTP ${response.status}`;
      return { ok: false, data, error: typeof detail === "string" ? detail : JSON.stringify(detail) };
    }
    return { ok: true, data, error: "" };
  } catch (cause) {
    return {
      ok: false,
      data: null,
      error: `cannot reach the Triveni API at ${BASE || window.location.origin}${path}`,
    };
  }
}

export const api = {
  health: () => request("/health"),
  root: () => request("/"),
  boundaries: () => request("/boundaries"),
  close: (date = "2026-03-31", alpha = "0.01") =>
    request(`/close?date=${encodeURIComponent(date)}&alpha=${encodeURIComponent(alpha)}`),
  exceptions: ({ type = "", severity = "", limit = 100 } = {}) =>
    request(
      `/exceptions?type=${encodeURIComponent(type)}&severity=${encodeURIComponent(severity)}&limit=${limit}`
    ),
  settlement: (id) => request(`/settlements/${encodeURIComponent(id)}`),
  resolve: (id, body) =>
    request(`/exceptions/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    }),
  ask: (question) => request(`/ask?question=${encodeURIComponent(question)}`),
  askCatalogue: () => request("/ask/catalogue"),
  verifyAudit: (from = 0, to = 0) => request(`/audit/verify?from_size=${from}&to_size=${to}`),
  mcpTools: () => request("/mcp/tools"),
  mcpCall: (tool, args = {}) =>
    request(`/mcp/call/${encodeURIComponent(tool)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(args),
    }),
};

/**
 * Subscribe to the close stream.
 *
 * `EventSource` rather than a fetch reader because reconnection, event framing and
 * cleanup are already solved there, and this is exactly the one-directional traffic
 * it was designed for. Returns a close function.
 */
export function streamClose(date, handlers = {}) {
  const source = new EventSource(`${BASE}/close/stream?date=${encodeURIComponent(date)}`);
  const bind = (name) => {
    if (!handlers[name]) return;
    source.addEventListener(name, (event) => {
      let payload = {};
      try {
        payload = JSON.parse(event.data);
      } catch {
        payload = { raw: event.data };
      }
      handlers[name](payload);
    });
  };
  bind("start");
  bind("stage");
  bind("done");

  source.addEventListener("error", () => {
    // A completed stream also lands here; `done` having fired is how we tell the
    // difference, and the caller decides what that means.
    handlers.error?.();
    source.close();
  });

  source.addEventListener("done", () => {
    // The server has nothing more to send. Close rather than letting EventSource
    // reconnect on a one-shot endpoint.
    setTimeout(() => source.close(), 0);
  });

  return () => source.close();
}
