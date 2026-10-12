"""Phase 0: summarize the JSON API calls captured in discovery/session.har.

Prints one row per unique (method, URL pattern), and per GraphQL operation:
query param / variable names with redacted example values, which auth-ish
headers were sent (names only, never values), status codes, and the
response's top-level JSON keys.

  uv run python scripts/summarize_har.py [path/to/session.har]
  uv run python scripts/summarize_har.py --dump-samples

--dump-samples writes one example per endpoint / GraphQL operation to
discovery/samples/ (gitignored, mode 600), with token-like values scrubbed, so
parsers can be written against real shapes.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HAR = ROOT / "discovery" / "session.har"
SAMPLES_DIR = ROOT / "discovery" / "samples"

AUTH_HEADER_NAMES = {"authorization", "cookie", "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token"}
SENSITIVE_KEY = re.compile(r"token|key|auth|session|secret|sig|password|jwt|code|credential", re.I)
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?)?$|^\d{10,13}$")
STATIC_EXT = re.compile(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ttf|otf|ico|map|webp|env)$", re.I)
GRAPHQL_OP = re.compile(r"\b(query|mutation|subscription)\s+(\w+)")
GRAPHQL_FIRST_FIELD = re.compile(r"\{\s*(\w+)")

ID_SEGMENTS = [
    (re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]*$"), "{jwt}"),
    (re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I), "{uuid}"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "{date}"),
    (re.compile(r"^\d+$"), "{id}"),
    (re.compile(r"^[0-9a-f]{24}$", re.I), "{objectid}"),
    (re.compile(r"^(?=.*\d)[\w.~:-]{16,}$"), "{token_or_id}"),
    (re.compile(r"^.{40,}$"), "{long}"),  # anything this long is never safe to print
]


def url_pattern(url: str) -> str:
    parts = urlsplit(url)
    segments = []
    for seg in parts.path.split("/"):
        for rx, label in ID_SEGMENTS:
            if rx.match(seg):
                seg = label
                break
        segments.append(seg)
    return f"{parts.scheme}://{parts.netloc}{'/'.join(segments)}"


def redact_value(name: str, value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    if SENSITIVE_KEY.search(name) or JWT.search(text):
        return "<redacted>"
    if DATE_LIKE.match(text) or len(text) <= 12:
        return text
    return f"<{len(text)} chars>"


def scrub(obj: Any) -> Any:
    """Recursively blank token-like keys and JWT-shaped strings."""
    if isinstance(obj, dict):
        return {k: "<redacted>" if SENSITIVE_KEY.search(k) and isinstance(v, str) else scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return JWT.sub("<redacted-jwt>", obj)
    return obj


def shape(body: Any) -> str:
    if isinstance(body, dict):
        return "{" + ", ".join(list(body.keys())[:15]) + (", ..." if len(body) > 15 else "") + "}"
    if isinstance(body, list):
        first = body[0] if body else None
        inner = shape(first) if isinstance(first, (dict, list)) else type(first).__name__
        return f"list[{len(body)}] of {inner}"
    return type(body).__name__


def parse_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def body_text(content: dict) -> str | None:
    text = content.get("text")
    if text and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except ValueError:
            return None
    return text


def is_api_call(entry: dict) -> bool:
    req, resp = entry["request"], entry["response"]
    if STATIC_EXT.search(urlsplit(req["url"]).path):
        return False
    mime = (resp.get("content") or {}).get("mimeType", "")
    return "json" in mime or entry.get("_resourceType", "") in ("xhr", "fetch")


def graphql_ops(req: dict) -> list[dict]:
    """GraphQL operations in a request body (batched requests have several)."""
    body = parse_json((req.get("postData") or {}).get("text"))
    ops = body if isinstance(body, list) else [body]
    return [op for op in ops if isinstance(op, dict) and isinstance(op.get("query"), str)]


def op_name(op: dict) -> str:
    if op.get("operationName"):
        kind = m[1] if (m := GRAPHQL_OP.search(op["query"])) else "query"
        return f"{kind} {op['operationName']}"
    if m := GRAPHQL_OP.search(op["query"]):
        return f"{m[1]} {m[2]}"
    m = GRAPHQL_FIRST_FIELD.search(op["query"])
    return f"query {{{m[1] if m else '?'}}}"


def new_group() -> dict:
    return {"count": 0, "params": {}, "auth": set(), "status": set(), "shape": "-", "sample": None}


def summarize(har_path: Path, dump_samples: bool) -> int:
    har = json.loads(har_path.read_text())
    groups: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for entry in har["log"]["entries"]:
        if not is_api_call(entry):
            continue
        req, resp = entry["request"], entry["response"]
        pattern = url_pattern(req["url"])
        response = parse_json(body_text(resp.get("content") or {}))
        ops = graphql_ops(req) if req["method"] == "POST" else []
        # One group per GraphQL operation; batched responses line up by index.
        targets = [(f"{pattern} :: {op_name(op)}", op, response[i] if isinstance(response, list) and i < len(response) else response)
                   for i, op in enumerate(ops)] or [(pattern, None, response)]
        for label, op, op_response in targets:
            g = groups.setdefault((req["method"], label), new_group())
            g["count"] += 1
            g["status"].add(resp.get("status"))
            for name, value in parse_qsl(urlsplit(req["url"]).query, keep_blank_values=True):
                g["params"].setdefault(name, redact_value(name, value))
            for name, value in ((op or {}).get("variables") or {}).items():
                g["params"].setdefault(f"${name}", redact_value(name, value))
            for h in req.get("headers", []):
                if h["name"].lower() in AUTH_HEADER_NAMES or "token" in h["name"].lower():
                    g["auth"].add(h["name"].lower())
            if op_response is not None and g["sample"] is None:
                data = op_response.get("data") if op and isinstance(op_response, dict) else None
                g["shape"] = f"data: {shape(data)}" if isinstance(data, dict) else shape(op_response)
                g["sample"] = {"query": op["query"], "variables": scrub(op.get("variables")), "response": scrub(op_response)} if op else scrub(op_response)

    if not groups:
        print("No JSON/XHR calls found in the HAR. Did you log in and click through?", file=sys.stderr)
        return 1

    for i, ((method, label), g) in enumerate(groups.items(), 1):
        params = ", ".join(f"{k}={v}" for k, v in g["params"].items()) or "-"
        print(f"[{i}] {method} {label}  (x{g['count']}, status {sorted(s for s in g['status'] if s)})")
        print(f"     params: {params}")
        print(f"     auth headers: {', '.join(sorted(g['auth'])) or '-'}")
        print(f"     response: {g['shape']}")

    if dump_samples:
        if SAMPLES_DIR.exists():
            shutil.rmtree(SAMPLES_DIR)  # don't leave stale (possibly unscrubbed) samples behind
        SAMPLES_DIR.mkdir(parents=True, mode=0o700)
        for i, ((method, label), g) in enumerate(groups.items(), 1):
            if g["sample"] is None:
                continue
            slug = re.sub(r"[^A-Za-z0-9]+", "_", urlsplit(label.split(" :: ")[0]).path + " " + label.partition(" :: ")[2])
            path = SAMPLES_DIR / f"{i:02d}_{method}_{slug.strip('_')[:80]}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(g["sample"], f, indent=2)
        print(f"\nScrubbed samples written to {SAMPLES_DIR.relative_to(ROOT)}/ (personal data; gitignored)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("har", nargs="?", type=Path, default=DEFAULT_HAR)
    parser.add_argument("--dump-samples", action="store_true")
    args = parser.parse_args()
    if not args.har.exists():
        sys.exit(f"{args.har} not found. Run scripts/discover.py first.")
    sys.exit(summarize(args.har, args.dump_samples))


if __name__ == "__main__":
    main()
