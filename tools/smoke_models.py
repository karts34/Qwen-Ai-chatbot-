"""Check each installed chat model through the offline localhost web API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = (
    "qwen3:1.7b",
    "llama3.2:1b",
    "gemma3:1b",
    "qwen3-4b-thinking-local:latest",
)
PROMPT = "What is 2 + 2? Answer briefly."
MAX_EVENT_BYTES = 2 * 1024 * 1024
METRICS = (
    "done_reason", "total_duration", "load_duration", "prompt_eval_count",
    "prompt_eval_duration", "eval_count", "eval_duration",
)


def endpoint_address(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError("Use an HTTP localhost endpoint, such as http://127.0.0.1:8080.")
    # Connect directly to IPv4 loopback. HTTPConnection never uses HTTP proxies.
    return "127.0.0.1", parsed.port or 80


def get_json(address: tuple[str, int], path: str, timeout: float) -> dict:
    connection = http.client.HTTPConnection(*address, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("The API response exceeded the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("The API returned an invalid JSON object.")
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {payload.get('error', 'API request failed')}")
        return payload
    finally:
        connection.close()


def check_model(address: tuple[str, int], name: str, timeout: float) -> dict:
    started = time.monotonic()
    deadline = started + timeout
    result = {"model": name, "ok": False}
    answer, thinking = [], []
    connection = http.client.HTTPConnection(*address, timeout=timeout)
    try:
        payload = {
            "model": name,
            "messages": [{"role": "user", "content": PROMPT}],
            "temperature": 0.6,
        }
        connection.connect()
        stream_socket = connection.sock
        connection.request("POST", "/api/chat", body=json.dumps(payload).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            detail = response.read(MAX_EVENT_BYTES).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {response.status}: {detail[:1000]}")
        if response.headers.get_content_type() != "application/x-ndjson":
            raise ValueError("Chat did not return an NDJSON stream.")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Generation exceeded {timeout:g} seconds.")
            # Keep the socket reference because close-delimited responses detach
            # it from HTTPConnection after getresponse().
            stream_socket.settimeout(remaining)
            line = response.readline(MAX_EVENT_BYTES + 1)
            if not line:
                raise ValueError("The chat stream ended without a done event.")
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("A chat event exceeded the size limit.")
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("The chat stream returned an invalid event.")
            event_type = event.get("type")
            if event_type in ("token", "thinking"):
                content = event.get("content")
                if not isinstance(content, str):
                    raise ValueError("A chat text event did not contain text.")
                (answer if event_type == "token" else thinking).append(content)
            elif event_type == "error":
                raise RuntimeError(str(event.get("error", "Chat generation failed.")))
            elif event_type == "done":
                for key in METRICS:
                    if key in event:
                        result[key] = event[key]
                if not "".join(answer).strip():
                    reason = event.get("done_reason", "unknown")
                    raise ValueError(f"The model finished with no answer (done_reason={reason}).")
                result["ok"] = True
                break
            else:
                raise ValueError(f"Unexpected chat event type: {event_type!r}.")
    except (OSError, http.client.HTTPException, ValueError, RuntimeError) as error:
        result["error"] = str(error)
    finally:
        connection.close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["answer"] = "".join(answer)
        result["thinking_characters"] = sum(map(len, thinking))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=180,
                        help="Maximum seconds per model (default: 180).")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime" / "smoke-results.json",
                        help="Write the JSON verification report to this path.")
    args = parser.parse_args()
    if not 0 < args.timeout <= 3600:
        parser.error("Timeout must be between 0 and 3600 seconds.")
    try:
        address = endpoint_address(args.endpoint)
        listed = get_json(address, "/api/models", min(args.timeout, 10))
        models = listed.get("models")
        if listed.get("offline") is not True or not isinstance(models, list):
            raise ValueError("The API did not return an offline model list.")
        installed = {model["name"] for model in models
                     if isinstance(model, dict) and isinstance(model.get("name"), str)}
    except (OSError, http.client.HTTPException, ValueError, RuntimeError) as error:
        print(f"Cannot check local models: {error}")
        return 1

    results = []
    for requested in args.models:
        # Ollama represents an untagged local import as name:latest.
        name = requested if requested in installed else requested + ":latest"
        if name not in installed:
            result = {"model": requested, "ok": False, "error": "Model is not installed."}
        else:
            print(f"Checking {name}...", flush=True)
            result = check_model(address, name, args.timeout)
        results.append(result)
        if result["ok"]:
            print(f"PASS {result['model']}: {result['answer'][:200]!r}; "
                  f"done_reason={result.get('done_reason', 'unknown')}, "
                  f"eval_count={result.get('eval_count', 'unknown')}, "
                  f"elapsed={result['elapsed_seconds']}s", flush=True)
        else:
            print(f"FAIL {result['model']}: {result['error']}", flush=True)

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "prompt": PROMPT,
        "ok": all(result["ok"] for result in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {args.output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
