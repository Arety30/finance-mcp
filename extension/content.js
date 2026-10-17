// Finance Guardrail — detects payment forms, checks the order total against
// the local finance-mcp guardrails, and shows a banner BEFORE the card is used.
(() => {
  const CC_SELECTOR = [
    'input[autocomplete*="cc-number"]',
    'input[autocomplete*="cc-"]',
    'input[name*="cardnumber" i]',
    'input[name*="card_number" i]',
    'input[name*="card-number" i]',
    'input[id*="cardnumber" i]',
    'input[id*="card-number" i]',
    'iframe[src*="checkout" i][src*="card" i]',
  ].join(",");

  let shown = false;

  // Try to scrape an order total from the page text, e.g. "Total: QAR 45.00".
  function findTotal() {
    const text = document.body ? document.body.innerText.slice(0, 20000) : "";
    const re = /(?:order\s+total|grand\s+total|amount\s+due|total)\s*:?\s*(?:QAR|QR|ر\.ق|\$|€|£|USD|SAR|AED)?\s*([\d,]+(?:\.\d{1,2})?)/gi;
    let best = null, m;
    while ((m = re.exec(text)) !== null) {
      const v = parseFloat(m[1].replace(/,/g, ""));
      if (v > 0 && (best === null || v > best)) best = v;
    }
    return best;
  }

  function banner() {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;";
    const root = host.attachShadow({ mode: "closed" });
    const box = document.createElement("div");
    box.style.cssText =
      "font:13px/1.5 system-ui,sans-serif;padding:10px 16px;display:flex;gap:12px;" +
      "align-items:center;flex-wrap:wrap;color:#fff;background:#555;box-shadow:0 2px 8px rgba(0,0,0,.35)";
    root.appendChild(box);
    document.documentElement.appendChild(host);

    const msg = document.createElement("span");
    msg.style.flex = "1";
    msg.textContent = "Guardrail: payment form detected — checking…";
    box.appendChild(msg);

    const input = document.createElement("input");
    input.type = "number";
    input.placeholder = "amount (QAR)";
    input.style.cssText = "width:110px;padding:4px 6px;border:none;border-radius:4px";
    const btn = document.createElement("button");
    btn.textContent = "Check";
    btn.style.cssText = "padding:4px 12px;border:none;border-radius:4px;cursor:pointer;background:#fff;color:#333";
    const close = document.createElement("button");
    close.textContent = "✕";
    close.style.cssText = "padding:4px 8px;border:none;border-radius:4px;cursor:pointer;background:transparent;color:#fff";
    close.onclick = () => host.remove();
    box.append(input, btn, close);

    function render(res, amount) {
      if (res.error) {
        box.style.background = "#7a6400";
        msg.textContent = "Guardrail: " + res.error;
      } else if (res.ok) {
        box.style.background = "#1c6b32";
        msg.textContent = `Guardrail OK — ${amount.toFixed(2)} ${res.currency} on '${res.card}' fits your limits.`;
      } else {
        box.style.background = "#a11a1a";
        msg.textContent = `Guardrail: DO NOT PAY ${amount.toFixed(2)} ${res.currency} — ` + res.reasons.join("; ");
      }
    }

    function check(amount) {
      chrome.runtime.sendMessage({ type: "check", amount }, (res) => render(res || { error: "no response" }, amount));
    }
    btn.onclick = () => { const v = parseFloat(input.value); if (v > 0) check(v); };

    const total = findTotal();
    if (total) {
      input.value = total;
      check(total);
    } else {
      chrome.runtime.sendMessage({ type: "status" }, (res) => {
        if (!res || res.error) {
          box.style.background = "#7a6400";
          msg.textContent = "Guardrail: " + (res ? res.error : "no response");
          return;
        }
        const c = res.cards && res.cards[0];
        box.style.background = "#1f4f8f";
        msg.textContent = c
          ? `Guardrail: payment form detected. '${c.nickname}' …${c.last4} has ` +
            `${c.remaining_this_month === null ? "no limit set" : c.remaining_this_month.toFixed(2) + " " + res.currency + " left this month"}. ` +
            "Couldn't read the order total — enter it to check:"
          : "Guardrail: payment form detected, but no card is tracked yet.";
      });
    }
  }

  function scan() {
    if (!shown && document.querySelector(CC_SELECTOR)) {
      shown = true;
      banner();
    }
  }

  scan();
  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
})();
