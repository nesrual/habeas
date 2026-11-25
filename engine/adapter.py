"""openai_compat adapter: one chat completion with retries and classification
(plan 8.1, 8.2).

Field names were checked against the OpenRouter docs on 2026-11-22:
`reasoning.effort`, `response_format.json_schema`, the `provider` routing
object, `usage.prompt_tokens` / `completion_tokens` / `cost` (always returned,
no request flag), `choices[0].finish_reason == "content_filter"` and
`message.refusal` for refusals, HTTP 403 with `error.metadata.error_type` of
`content_policy_violation` or `refusal` for blocked input, and error objects
inside HTTP 200 bodies. The serving provider is read from the top-level
`provider` field when present, otherwise from `openrouter_metadata`, which
OpenRouter returns when the request carries `X-OpenRouter-Metadata: enabled`.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

from . import schema as schema_mod

MAX_ATTEMPTS = 5
# A per-minute rate limit clears by waiting, so a 429 gets more tries than other errors.
MAX_ATTEMPTS_RATE_LIMITED = 9
RETRYABLE = {0, 408, 429}
REFUSAL_TYPES = {"content_policy_violation", "refusal"}
REFUSAL_RE = re.compile(
    r"\b(i\s+(can(?:no|')t|cannot|won't|will not|am (?:not able|unable)|'m (?:not able|unable))"
    r"\s+(?:\w+\s+){0,3}?(help|assist|comply|provide|review|analy[sz]e|do|continue|complete|fulfil)"
    r"|i must decline|i have to decline|unable to (help|assist|comply))", re.I)
REPAIR = ("Your reply was not valid JSON matching the schema. "
          "Return only the JSON object, with no prose and no code fences.")


class AdapterError(Exception):
    pass


def api_key(endpoint: dict) -> str | None:
    env = endpoint.get("key_env")
    if not env:
        return None
    key = os.environ.get(env)
    if not key:
        raise AdapterError(f"environment variable {env} is not set")
    return key


def build_body(seat: dict, provider: dict | None, messages: list, effort: str | None,
               schema: dict | None, schema_name: str) -> dict:
    body: dict = {"model": seat["model"], "messages": messages}
    if effort:
        body["reasoning"] = {"effort": effort}
    if provider:
        body["provider"] = provider
    if schema and seat.get("json_schema", True):
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema_name, "strict": True, "schema": schema}}
    if seat.get("stop_on_whitespace_run"):
        # valid JSON output never holds these runs outside a string, and inside one they are escaped
        body["stop"] = ["\t\t", " \t ", "\r\n\r\n", " " * 16]
    body.update(seat.get("extra_body") or {})
    return body


def _post(url: str, headers: dict, body: dict, timeout: float):
    """Returns (http_status, response_headers, text). Status 0 is a transport failure."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {}, f"transport error: {e}"


def _backoff(attempt: int, headers: dict, code: int = 0) -> float:
    base = float(os.environ.get("HABEAS_BACKOFF_BASE", "1.0"))
    # rate limits are per minute: wait long enough for the window to move (about 75 s in total)
    delay = base * (5 if code == 429 else 1) * (2 ** (attempt - 1)) * (0.5 + random.random())
    if code == 429:
        delay = min(delay, base * 60 * (0.75 + 0.5 * random.random()))   # at most about a minute per wait
    retry_after = {k.lower(): v for k, v in headers.items()}.get("retry-after")
    if retry_after and base >= 1.0:
        try:
            delay = max(delay, min(float(retry_after), 60.0))
        except ValueError:
            pass
    return delay


def _error_of(status: int, data) -> tuple[int, dict] | None:
    """An error in the HTTP status or inside a 200 body, as (code, error object)."""
    err = None
    if isinstance(data, dict):
        err = data.get("error")
        if not err and not status >= 400:
            choices = data.get("choices") or [{}]
            err = (choices[0] or {}).get("error")
    if status == 200 and not err:
        return None
    if not isinstance(err, dict):
        err = {"message": str(err) if err else ""}
    code = err.get("code")
    code = code if isinstance(code, int) else (status if status != 200 else 500)
    return code, err


def _send(url, headers, body, timeout, attempts_log) -> tuple[dict | None, tuple | None]:
    """POST with retries. Returns (response json, None) or (None, (code, error))."""
    failure = None
    for attempt in range(1, MAX_ATTEMPTS_RATE_LIMITED + 1):
        status, rheaders, text = _post(url, headers, body, timeout)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        attempts_log.append({"http_status": status, "body": data if data is not None else text[:20000]})
        failure = _error_of(status, data)
        if failure is None and not isinstance(data, dict):
            failure = (500, {"message": "response body is not a JSON object"})
        if failure is None:
            return data, None
        code = failure[0]
        limit = MAX_ATTEMPTS_RATE_LIMITED if code == 429 else MAX_ATTEMPTS
        if not (code in RETRYABLE or code >= 500) or attempt >= limit:
            break
        time.sleep(_backoff(attempt, rheaders, code))
    return None, failure


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def _parse(text: str, schema: dict | None):
    """(value, error). Accepts a bare JSON object or one inside a code fence."""
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```$", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        value = json.loads(s)
    except ValueError:
        # Seen with Mistral in JSON mode (2026-11-22): a complete object body, then whitespace
        # until the output limit, with the closing brace never written. Only that exact shape
        # is accepted, and the result still has to pass the schema.
        if not s.startswith("{"):
            return None, "not JSON"
        try:
            value = json.loads(s + "}")
        except ValueError:
            return None, "not JSON"
        # the fields the model never reached are the trailing ones: give them empty values
        if isinstance(value, dict) and schema and schema.get("type") == "object":
            empty = {"string": "", "array": [], "object": {}}
            for name in schema.get("required") or []:
                kind = (schema.get("properties") or {}).get(name, {}).get("type")
                if name not in value and kind in empty:
                    value[name] = empty[kind]
    errs = schema_mod.errors(value, schema) if schema else []
    return (None, "; ".join(errs[:5])) if errs else (value, None)


def _served_provider(data: dict) -> str | None:
    if isinstance(data.get("provider"), str):
        return data["provider"]
    meta = data.get("openrouter_metadata")
    available = ((meta or {}).get("endpoints") or {}).get("available") if isinstance(meta, dict) else None
    for ep in available or []:
        if isinstance(ep, dict) and ep.get("selected"):
            return ep.get("provider")
    return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def same_model(seat: dict, served: str | None) -> bool:
    """The served model is the pinned ID, its dated snapshot, or the seat's canonical_model."""
    requested = seat["model"]
    if served in (requested, seat.get("canonical_model")):
        return True
    return bool(served) and re.fullmatch(re.escape(requested) + r"-\d{8}", served) is not None


def complete(seat: dict, endpoint: dict, provider: dict | None, system: str, user: str,
             effort: str | None = None, schema: dict | None = None,
             schema_name: str = "result") -> dict:
    """One logical call. Returns the fields of a `calls` row plus `parsed`,
    `reason` and `raw` (request and every response, with no credentials)."""
    url = endpoint["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = api_key(endpoint)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider is not None:
        headers["X-OpenRouter-Metadata"] = "enabled"
    timeout = float(seat.get("timeout_s") or endpoint.get("timeout_s") or 600)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    out = {"status": "error", "reason": None, "finish_reason": None, "served_model": None,
           "served_provider": None, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
           "parsed": None}
    attempts: list = []
    request_body = None
    started = time.monotonic()
    repaired = False
    while True:
        body = build_body(seat, provider, messages, effort, schema, schema_name)
        request_body = request_body or body
        data, failure = _send(url, headers, body, timeout, attempts)
        if failure:
            code, err = failure
            etype = (err.get("metadata") or {}).get("error_type") if isinstance(err.get("metadata"), dict) else None
            flagged = code == 403 and "flagged" in str(err.get("message", "")).lower()
            if code == 403 and etype in REFUSAL_TYPES:
                out.update(status="refused", reason=f"http_403_{etype}")
            elif flagged:
                # a content flag without the error type: still the model's side declining the input,
                # so it goes to the fallback model like any other refusal
                out.update(status="refused", reason="http_403_flagged")
            else:
                out.update(status="error", reason=f"http_{code}: {str(err.get('message', ''))[:200]}")
            break
        usage = data.get("usage") or {}
        out["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        out["output_tokens"] += int(usage.get("completion_tokens") or 0)
        out["cost_usd"] += float(usage.get("cost") or 0)
        out["served_model"] = data.get("model")
        out["served_provider"] = _served_provider(data)
        choice = (data.get("choices") or [{}])[0] or {}
        message = choice.get("message") or {}
        out["finish_reason"] = choice.get("finish_reason")
        text = _text_of(message)
        if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
            out.update(status="refused", reason="endpoint_refusal")
            break
        value, problem = _parse(text, schema)
        if problem is None:
            out.update(status="ok", parsed=value)
            break
        if "{" not in text and REFUSAL_RE.search(text):
            out.update(status="refused", reason="refusal_text")
            break
        if repaired:
            out.update(status="error", reason=f"invalid_json: {problem}"[:300])
            break
        repaired = True
        if text.strip():
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": REPAIR}]

    if out["status"] == "ok":
        only = (provider or {}).get("only")
        if seat.get("open_weights") and only:
            allowed = {_norm(p) for p in only}
            served = out["served_provider"]
            if not served or _norm(served) not in allowed:
                out.update(status="error", parsed=None,
                           reason=f"served_provider {served!r} is not in provider.only")
        if out["status"] == "ok" and not same_model(seat, out["served_model"]):
            out.update(status="downgraded", reason=f"served {out['served_model']!r}")
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    out["raw"] = {
        "url": url,
        "request_headers": {k: ("[redacted]" if k == "Authorization" else v) for k, v in headers.items()},
        "request": request_body, "repair_retry": repaired, "responses": attempts,
    }
    return out
