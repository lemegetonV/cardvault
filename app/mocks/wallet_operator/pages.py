"""The operator's embeddable pages: wallet terms (framed by the bank's web app) and help.

Plain server-rendered HTML, no auth (spec 7.1).

* ``GET /embed/terms``: the terms, an "I agree" checkbox and a "Continue" button. Continue
  sends ``postMessage({type: "terms-accepted", version})`` to the parent window, addressed
  to each allow-listed origin in turn (a message whose target origin does not match the
  parent's is dropped by the browser, so only the real parent receives it). Framing is
  allowed only from ``WEB_ALLOWED_ORIGINS`` (``Content-Security-Policy: frame-ancestors``).
* ``GET /help``: a static help page, opened by the web app in a new tab.
"""

from __future__ import annotations

import html
import json
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from shared import seed_data

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "WEB_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]
TERMS_VERSION = seed_data.TERMS_VERSION

router = APIRouter(tags=["pages"])

_STYLE = """
  body { font-family: system-ui, sans-serif; margin: 0; padding: 16px; color: #1d2433;
         background: #ffffff; line-height: 1.45; }
  h1 { font-size: 1.15rem; margin: 0 0 8px; }
  .terms { max-height: 180px; overflow-y: auto; border: 1px solid #d5dae3; padding: 8px 12px;
           border-radius: 6px; font-size: 0.9rem; }
  label { display: flex; gap: 8px; align-items: center; margin: 12px 0; }
  button { font: inherit; padding: 8px 16px; border-radius: 6px; border: 0;
           background: #2a5bd7; color: #fff; cursor: pointer; }
  button:disabled { background: #9aa9cc; cursor: not-allowed; }
  .done { color: #1a7f37; margin-top: 8px; }
"""


def _terms_page() -> str:
    origins = json.dumps(ALLOWED_ORIGINS)
    version = html.escape(TERMS_VERSION)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallet terms</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
  <h1>Checkout wallet terms <span data-testid="terms-version">(version {version})</span></h1>
  <div class="terms" data-testid="terms-text" tabindex="0">
    <p>By adding a card to the checkout wallet you let the wallet operator store a token that
    stands in for your card number. Merchants that accept the wallet are charged through that
    token; your card number is never shared with them.</p>
    <p>Your bank decides whether a card can be added and may suspend or remove the token,
    for example when the card is reported lost. You can remove a card, or opt out of the
    wallet entirely, at any time.</p>
    <p>This is a simulation: no real cards, money or operators are involved.</p>
  </div>
  <label><input type="checkbox" id="agree" data-testid="terms-agree"> I agree</label>
  <button type="button" id="continue" data-testid="terms-continue" disabled>Continue</button>
  <p class="done" id="done" data-testid="terms-accepted" role="status" hidden>
    Thank you. You can close this and continue.</p>
</main>
<script>
  (function () {{
    var allowed = {origins};
    var version = {json.dumps(TERMS_VERSION)};
    var agree = document.getElementById("agree");
    var cont = document.getElementById("continue");
    agree.addEventListener("change", function () {{ cont.disabled = !agree.checked; }});
    cont.addEventListener("click", function () {{
      if (!agree.checked || window.parent === window) return;
      for (var i = 0; i < allowed.length; i++) {{
        window.parent.postMessage({{ type: "terms-accepted", version: version }}, allowed[i]);
      }}
      cont.disabled = true;
      agree.disabled = true;
      document.getElementById("done").hidden = false;
    }});
  }})();
</script>
</body>
</html>"""


@router.get(
    "/embed/terms",
    summary="Embeddable wallet terms (HTML)",
    description="Shown in an iframe by the bank's web app. Sends postMessage "
    '{"type":"terms-accepted","version"} to the parent when the customer agrees. Framing is '
    "allowed only from WEB_ALLOWED_ORIGINS.",
    response_class=HTMLResponse,
)
async def terms() -> HTMLResponse:
    return HTMLResponse(
        _terms_page(),
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "frame-ancestors " + " ".join(ALLOWED_ORIGINS)
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


_HELP = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallet help</title>
<style>{_STYLE} body {{ max-width: 720px; margin: 0 auto; }}</style>
</head>
<body>
<main>
  <h1 data-testid="help-title">Checkout wallet help</h1>
  <h2>Which cards can I add?</h2>
  <p>Active credit and debit cards that you used in the last 90 days, once your email and
  phone are verified. Your bank shows the reason when a card cannot be added.</p>
  <h2>Why is my card "pending"?</h2>
  <p>The wallet operator confirms each change a moment after your bank asks for it.</p>
  <h2>I lost my card</h2>
  <p>Report it lost with your bank. The card is suspended in the wallet straight away and
  comes back when the replacement card is issued.</p>
  <h2>How do I leave the wallet?</h2>
  <p>Opt out from the wallet page. Every card is removed; you can opt in again later.</p>
</main>
</body>
</html>"""


@router.get(
    "/help",
    summary="Wallet help page (HTML)",
    description="A static help page, linked from the web app with target=_blank.",
    response_class=HTMLResponse,
)
async def help_page() -> HTMLResponse:
    return HTMLResponse(_HELP, headers={"X-Content-Type-Options": "nosniff"})
