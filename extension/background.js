// Talks to the local finance-mcp guardrail endpoint (127.0.0.1 only).
// Content scripts can't fetch localhost from an https page, so they message us.
const BASE = "http://127.0.0.1:8975/guardrail";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const url =
    msg.type === "check"
      ? `${BASE}/check?amount=${encodeURIComponent(msg.amount)}` +
        (msg.category ? `&category=${encodeURIComponent(msg.category)}` : "")
      : `${BASE}/status`;
  fetch(url)
    .then((r) => r.json())
    .then(sendResponse)
    .catch((e) => sendResponse({ error: "guardrail server unreachable: " + e }));
  return true; // async sendResponse
});
