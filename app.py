"""A dependency-free, loopback-only web UI server for local Ollama models."""

from __future__ import annotations

import argparse
import http.client
import json
import math
from pathlib import Path
import re
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


OLLAMA_URL = "http://127.0.0.1:11434"
WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 32000
MAX_HISTORY_CHARS = 64000
MAX_MESSAGES = 100
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
CONNECT_TIMEOUT = 3
GENERATION_TIMEOUT = 180
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}\Z")
METRIC_FIELDS = (
    "done_reason", "total_duration", "load_duration", "prompt_eval_count",
    "prompt_eval_duration", "eval_count", "eval_duration",
)


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_local_model(model: dict) -> bool:
    """Cloud references can appear in /api/tags; never offer or run them."""
    name = model.get("name", "")
    if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
        return False
    lowered = name.lower()
    if "cloud" in re.split(r"[-:/._]", lowered):
        return False
    if any(model.get(key) for key in ("remote_host", "remote_model", "remote", "cloud")):
        return False
    return True


def reasoning_profile(name: str, metadata: dict | None = None) -> dict:
    """Use Ollama's supported controls; imported models may only expose a capability."""
    metadata = metadata or {}
    thinking = metadata.get("thinking")
    if isinstance(thinking, dict) and isinstance(thinking.get("values"), list):
        values = thinking["values"]
        allows_on = any(value is True for value in values)
        allows_off = any(value is False for value in values)
        if allows_on and allows_off:
            return {"mode": "toggle", "default": thinking.get("default", True)}
        if allows_on:
            return {"mode": "required", "default": True}
        if allows_off and len(values) == 1:
            return {"mode": "none", "default": False}
        if values:
            return {"mode": "default", "default": thinking.get("default", True)}
    # Preserve the controls of existing Qwen installations on older runtimes.
    model_base = name.lower().split(":", 1)[0].rsplit("/", 1)[-1]
    if model_base.startswith("qwen3") and "thinking" in name.lower():
        return {"mode": "required", "default": True}
    if model_base == "qwen3":
        return {"mode": "toggle", "default": True}
    capabilities = metadata.get("capabilities", [])
    if isinstance(capabilities, list) and "thinking" in capabilities:
        # No on/off metadata means we keep the model's native default behavior.
        return {"mode": "default", "default": True}
    return {"mode": "none", "default": False}


def apply_model_controls(payload: dict, requested: dict, profile: dict):
    """Never send an unsupported thinking toggle to an imported reasoner."""
    payload.pop("think", None)
    mode = profile["mode"]
    if mode == "toggle":
        # Keep quick direct answers as the app default for switchable models.
        payload["think"] = requested.get("think", False)
    reasons = mode in ("required", "default") or payload.get("think") is True
    payload["options"]["num_predict"] = 4096 if reasons else 1024


def validate_chat(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise APIError(400, "Send a JSON object containing model and messages.")
    if set(payload) - {"model", "messages", "temperature", "think"}:
        raise APIError(400, "Only model, messages, temperature, and think are supported.")
    name = payload.get("model")
    if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
        raise APIError(400, "Choose a valid installed model.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise APIError(400, f"Send between 1 and {MAX_MESSAGES} chat messages.")
    clean_messages = []
    total = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content"}:
            raise APIError(400, "Each message needs only a role and text content.")
        role, content = message.get("role"), message.get("content")
        if role not in ("system", "user", "assistant"):
            raise APIError(400, "Message roles must be system, user, or assistant.")
        if not isinstance(content, str) or not content.strip():
            raise APIError(400, "Messages must contain nonempty text.")
        if len(content) > MAX_MESSAGE_CHARS:
            raise APIError(400, f"A message can contain at most {MAX_MESSAGE_CHARS:,} characters.")
        total += len(content)
        clean_messages.append({"role": role, "content": content})
    if total > MAX_HISTORY_CHARS:
        raise APIError(400, "This conversation is too long. Start a new chat.")
    if not any(message["role"] == "user" for message in clean_messages):
        raise APIError(400, "Include at least one user message.")
    temperature = payload.get("temperature", 0.7)
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise APIError(400, "Temperature must be a number from 0 to 2.")
    if "think" in payload and not isinstance(payload["think"], bool):
        raise APIError(400, "Think must be true or false.")
    result = {
        "model": name,
        "messages": clean_messages,
        "stream": True,
        "keep_alive": "5m",
        "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": 1024},
    }
    return result


class OllamaClient:
    """Only connects directly to loopback, bypassing any configured HTTP proxies."""

    def __init__(self, base_url: str = OLLAMA_URL):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("Ollama must use an HTTP URL on 127.0.0.1.")
        self.port = parsed.port or 80
        self._model_profiles = {}
        self._profile_lock = threading.Lock()

    def request(self, method: str, path: str, payload: dict | None = None,
                *, stream: bool = False, on_connect=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=CONNECT_TIMEOUT)
        try:
            connection.connect()
            connection.sock.settimeout(GENERATION_TIMEOUT if stream else CONNECT_TIMEOUT)
            if on_connect is not None:
                on_connect(connection.sock)
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status >= 400:
                raw = response.read(MAX_UPSTREAM_BYTES + 1)
                try:
                    detail = json.loads(raw).get("error", "")
                except (ValueError, AttributeError, UnicodeDecodeError):
                    detail = ""
                if not isinstance(detail, str):
                    detail = ""
                raise APIError(502, detail[:1000] or f"Ollama returned HTTP {response.status}.")
            return connection, response
        except APIError:
            connection.close()
            raise
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            if isinstance(error, (TimeoutError, socket.timeout)):
                raise APIError(504, "Ollama took too long to respond. Try a smaller model or retry.") from error
            raise APIError(503, "Ollama is not running. Start the local Ollama service and retry.") from error

    def json(self, path: str, payload: dict | None = None) -> dict:
        connection, response = self.request("POST" if payload is not None else "GET", path, payload)
        try:
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise APIError(502, "Ollama returned an unexpectedly large response.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except (ValueError, UnicodeDecodeError) as error:
            raise APIError(502, "Ollama returned an invalid response.") from error
        except (OSError, http.client.HTTPException) as error:
            raise APIError(503, "The connection to Ollama was interrupted. Retry the request.") from error
        finally:
            connection.close()

    def models(self) -> list[dict]:
        data = self.json("/api/tags")
        if not isinstance(data.get("models"), list):
            raise APIError(502, "Ollama returned an invalid model list.")
        models = []
        for model in data["models"]:
            if not isinstance(model, dict) or not is_local_model(model):
                continue
            name = model["name"]
            identity = (name, str(model.get("digest", model.get("modified_at", ""))),
                        str(model.get("size", "")))
            with self._profile_lock:
                if identity not in self._model_profiles:
                    try:
                        metadata = self.json("/api/show", {"model": name})
                    except APIError:
                        metadata = {}
                    self._model_profiles[identity] = reasoning_profile(name, metadata)
                profile = self._model_profiles[identity]
            public = {key: model[key] for key in ("name", "size", "modified_at", "details") if key in model}
            public["reasoning"] = profile
            models.append(public)
        return models


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, ollama_url: str = OLLAMA_URL,
                 web_dir: Path = WEB_DIR):
        if address[0] != "127.0.0.1":
            raise ValueError("The chatbot may only listen on 127.0.0.1.")
        self.ollama_url = ollama_url
        self.ollama = OllamaClient(ollama_url)
        self.web_dir = Path(web_dir)
        self.generation_lock = threading.Lock()
        super().__init__(address, ChatHandler)


class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalChat/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _headers(self, status: int, content_type: str, length: int | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; "
                         "script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.close_connection = True
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, payload: dict):
        encoded = json.dumps(payload).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(encoded))
        self.wfile.write(encoded)

    def _check_host(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed |= {"127.0.0.1", "localhost"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            raise APIError(403, "Open the chatbot using its localhost address.")

    def _check_origin(self):
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0].lower() != "http://" + self.headers["Host"].lower()):
            raise APIError(403, "Cross-origin chat requests are not allowed.")
        if self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            raise APIError(403, "Open this page directly on localhost to send messages.")

    def do_GET(self):
        try:
            self._check_host()
            path = urlsplit(self.path).path
            if path == "/api/status":
                try:
                    version = self.server.ollama.json("/api/version").get("version", "")
                    self._json(200, {"service": "local-chat", "ok": True, "offline": True, "ollama_url": self.server.ollama_url,
                                     "ollama": {"available": True, "version": version}})
                except APIError as error:
                    self._json(error.status, {"service": "local-chat", "ok": False, "offline": True,
                                              "ollama": {"available": False}, "error": str(error)})
            elif path == "/api/models":
                self._json(200, {"models": self.server.ollama.models(), "offline": True})
            elif path in ASSETS:
                filename, content_type = ASSETS[path]
                try:
                    content = (self.server.web_dir / filename).read_bytes()
                except OSError:
                    raise APIError(404, "The requested web asset is missing.")
                self._headers(200, content_type, len(content))
                self.wfile.write(content)
            else:
                raise APIError(404, "This page does not exist.")
        except APIError as error:
            try:
                self._json(error.status, {"error": str(error)})
            except OSError:
                pass
        except OSError:
            pass

    def _read_payload(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "Chunked request bodies are not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError(411, "Provide a Content-Length header.")
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY_BYTES:
            raise APIError(413, "The request is too large. Shorten the conversation.")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "Send the request as application/json.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError(400, "The request body was incomplete.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "The request body must contain valid JSON.")

    def _event(self, payload: dict):
        self.wfile.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.wfile.flush()

    def _stream_chat(self, payload: dict):
        finished = threading.Event()
        disconnected = threading.Event()
        transport = []

        def abort_transport(upstream_socket):
            try:
                upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # A makefile-backed HTTP read can retain the socket after close,
            # and on Windows shutdown alone may leave its select waiting.
            # Detach and close the owned descriptor to wake that read promptly.
            try:
                descriptor = upstream_socket.detach()
                if descriptor >= 0:
                    socket.close(descriptor)
            except OSError:
                pass

        def on_connect(upstream_socket):
            transport.append(upstream_socket)
            if disconnected.is_set():
                abort_transport(upstream_socket)

        def watch_disconnect():
            # Abort a blocked Ollama read when the browser's Stop button closes
            # this request, including while Ollama is still loading a model.
            while not finished.wait(0.25):
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                        disconnected.set()
                        if transport:
                            abort_transport(transport[0])
                        return
                except (ConnectionResetError, ConnectionAbortedError):
                    disconnected.set()
                    if transport:
                        abort_transport(transport[0])
                    return
                except (OSError, ValueError):
                    return

        watcher = threading.Thread(target=watch_disconnect, daemon=True)
        watcher.start()
        connection = None
        started = False
        try:
            connection, response = self.server.ollama.request(
                "POST", "/api/chat", payload, stream=True, on_connect=on_connect)
            self._headers(200, "application/x-ndjson; charset=utf-8")
            started = True
            while True:
                line = response.readline(MAX_UPSTREAM_BYTES + 1)
                if not line:
                    raise APIError(502, "Ollama ended the response before completion. Retry the message.")
                if len(line) > MAX_UPSTREAM_BYTES:
                    raise APIError(502, "Ollama returned an unexpectedly large chat event.")
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise APIError(502, "Ollama returned an invalid chat event.")
                if not isinstance(event, dict):
                    raise APIError(502, "Ollama returned an invalid chat event.")
                if event.get("error"):
                    detail = event["error"]
                    raise APIError(502, detail[:1000] if isinstance(detail, str) else "Ollama could not generate a response.")
                message = event.get("message", {})
                if not isinstance(message, dict):
                    raise APIError(502, "Ollama returned an invalid chat message.")
                for field, event_type in (("thinking", "thinking"), ("content", "token")):
                    content = message.get(field)
                    if content is not None and not isinstance(content, str):
                        raise APIError(502, "Ollama returned invalid response text.")
                    if content:
                        self._event({"type": event_type, "content": content})
                if event.get("done") is True:
                    done_event = {"type": "done", "model": payload["model"]}
                    for key in METRIC_FIELDS:
                        if key in event:
                            done_event[key] = event[key]
                    self._event(done_event)
                    break
        except APIError as error:
            if not disconnected.is_set():
                if not started:
                    raise
                self._event({"type": "error", "error": str(error)})
        except (socket.timeout, TimeoutError):
            self._event({"type": "error", "error": "Generation timed out. Try a smaller model or retry."})
        except http.client.HTTPException:
            self._event({"type": "error", "error": "The Ollama stream was interrupted. Retry the message."})
        except OSError:
            if not disconnected.is_set():
                self._event({"type": "error", "error": "The connection to Ollama was interrupted. Retry the message."})
        finally:
            finished.set()
            if connection is not None:
                connection.close()

    def do_POST(self):
        acquired = False
        try:
            self._check_host()
            self._check_origin()
            if urlsplit(self.path).path != "/api/chat":
                raise APIError(404, "This API route does not exist.")
            requested = self._read_payload()
            payload = validate_chat(requested)
            models = self.server.ollama.models()
            model = next((model for model in models if model["name"] == payload["model"]), None)
            if model is None:
                raise APIError(400, "This local model is not installed. Refresh the model list and choose an installed model.")
            apply_model_controls(payload, requested, model["reasoning"])
            acquired = self.server.generation_lock.acquire(blocking=False)
            if not acquired:
                raise APIError(409, "Another response is still running. Wait for it to finish and retry.")
            self._stream_chat(payload)
        except APIError as error:
            try:
                self._json(error.status, {"error": str(error)})
            except OSError:
                pass
        except OSError:
            # Upstream failures are already translated into API errors/events.
            # A remaining socket error is a disconnected browser, including
            # Windows ConnectionAbortedError when Stop closes an active stream.
            pass
        finally:
            if acquired:
                self.server.generation_lock.release()

    def log_message(self, format, *args):
        # Never log request bodies or model response text.
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    parser = argparse.ArgumentParser(description="Offline multi-model chatbot using local Ollama.")
    parser.add_argument("--port", type=int, default=8080, help="Local web UI port (default: 8080).")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    try:
        server = ChatServer(("127.0.0.1", args.port))
    except OSError as error:
        parser.exit(1, f"Cannot start localhost:{args.port}: {error}. Try another --port.\n")
    print(f"Local chatbot: http://localhost:{args.port}")
    print("Uses installed local Ollama models only. Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nChatbot stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
