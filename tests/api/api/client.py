"""The one HTTP session every API class uses, with a log line for each call.

requests.Session works like requests.get and requests.post, with two extras:
  - it keeps connections to a service open between calls, so tests run faster;
  - it can run a function after every response. Here log_call writes the call
    to the log, and pytest shows that log under any test that fails.

So when a test fails, you see every request it sent and every answer it got.
"""

import logging
import re

import requests

log = logging.getLogger("api")

# A token or password in a log is a secret anyone with the log could use, so hide them.
TOKENS = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+|rt_[\w-]{10,}")
PASSWORDS = re.compile(r'("password"\s*:\s*")[^"]*')


def hide_secrets(text):
    text = TOKENS.sub("<token hidden>", text)
    return PASSWORDS.sub(r"\1<hidden>", text)


def log_call(response, *args, **kwargs):
    """requests calls this after every response (it is a "hook")."""
    request = response.request
    milliseconds = round(response.elapsed.total_seconds() * 1000)
    log.info(f"{request.method} {request.url} -> {response.status_code} in {milliseconds} ms")
    sent = request.body
    if isinstance(sent, bytes) and "json" in request.headers.get("Content-Type", ""):
        sent = sent.decode()  # a JSON body is text, sent as bytes
    if isinstance(sent, str):
        log.info(f"  sent: {hide_secrets(sent)[:500]}")
    elif sent:
        log.info(f"  sent: {len(sent)} bytes (a file upload)")
    if "json" in response.headers.get("Content-Type", ""):
        log.info(f"  got:  {hide_secrets(response.text)[:500]}")


http = requests.Session()
http.hooks["response"].append(log_call)
