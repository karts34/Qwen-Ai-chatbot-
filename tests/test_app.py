"""Integration checks for local API validation and the Ollama streaming bridge."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app


class MockOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, payload):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/api/version":
            self.respond(200, {"version": "test"})
        elif self.path == "/api/tags":
            self.respond(200, {"models": [
                {"name": "qwen3:1.7b", "size": 1000, "details": {"family": "qwen3"}},
                {"name": "llama3.2:1b", "size": 2000},
                {"name": "qwen3-4b-thinking-local:latest", "size": 3000},
                {"name": "qwen3:4b-thinking", "size": 3000},
                {"name": "deepseek-r1-hf:1.5b", "size": 1500,
                 "digest": self.server.model_digest},
                {"name": "opaque-local:latest", "size": 2000},
                {"name": "large:cloud", "size": 4000},
                {"name": "remote:latest", "remote_host": "https://ollama.com"},
            ]})
        else:
            self.respond(404, {"error": "Unknown route"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self.server.shown.append(payload["model"])
            if self.server.mode == "no-show":
                self.respond(404, {"error": "Unknown route"})
                return
            profiles = {
                "qwen3:1.7b": {"thinking": {"values": [False, True], "default": True}},
                "llama3.2:1b": {"thinking": {"values": [False], "default": False}},
                "qwen3-4b-thinking-local:latest": {"capabilities": ["thinking", "completion"]},
                "qwen3:4b-thinking": {"thinking": {"values": [True], "default": True}},
                "deepseek-r1-hf:1.5b": {"capabilities": ["thinking", "completion"]},
                "opaque-local:latest": {"thinking": {"values": [True, False], "default": True}},
            }
            self.respond(200, profiles[payload["model"]])
            return
        self.server.received.append(payload)
        self.server.started.set()
        mode = self.server.mode
        if mode == "http-error":
            self.respond(500, {"error": "Model failed to load: insufficient memory"})
            return
        if mode == "delayed":
            self.server.resume.wait(5)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        events = self.server.events
        if mode == "malformed":
            raw = b"not JSON\n"
        elif mode == "empty":
            raw = b""
        else:
            raw = b"".join(json.dumps(event).encode() + b"\n" for event in events)
        try:
            self.wfile.write(raw)
        except (OSError, ConnectionError):
            pass


class ChatAPITest(unittest.TestCase):
    def setUp(self):
        self.log_patch = patch.object(app.ChatHandler, "log_message")
        self.log_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.web_dir = Path(self.temp.name)
        (self.web_dir / "index.html").write_text("<title>Local chat</title>")
        (self.web_dir / "app.js").write_text("console.log('local');")
        (self.web_dir / "styles.css").write_text("body { color: black; }")
        (self.web_dir / "secret.txt").write_text("must not be served")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaHandler)
        self.upstream.daemon_threads = True
        self.upstream.received = []
        self.upstream.shown = []
        self.upstream.model_digest = "model-digest-1"
        self.upstream.mode = "normal"
        self.upstream.started = threading.Event()
        self.upstream.resume = threading.Event()
        self.upstream.events = [
            {"message": {"thinking": "Checking."}, "done": False},
            {"message": {"content": "Hello "}, "done": False},
            {"message": {"content": "world"}, "done": False},
            {"message": {"content": "!"}, "done": True,
             "eval_count": 3, "eval_duration": 1000000000, "done_reason": "stop"},
        ]
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever,
                                                kwargs={"poll_interval": 0.02}, daemon=True)
        self.upstream_thread.start()
        self.server = app.ChatServer(("127.0.0.1", 0), web_dir=self.web_dir,
                                     ollama_url=f"http://127.0.0.1:{self.upstream.server_port}")
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.upstream.resume.set()
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()
        self.log_patch.stop()

    def request(self, method="GET", path="/api/status", payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        if payload is not None:
            raw = json.dumps(payload).encode()
        headers = dict(headers or {})
        if method == "POST":
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        status, response_headers, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, body

    @staticmethod
    def chat_payload(model="qwen3:1.7b"):
        return {"model": model, "messages": [{"role": "user", "content": "Hello"}]}

    def chat(self, payload=None, **kwargs):
        return self.request("POST", "/api/chat", self.chat_payload() if payload is None else payload, **kwargs)

    def test_status_and_cloud_models(self):
        status, _, raw = self.request()
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(result["service"], "local-chat")
        self.assertTrue(result["offline"])
        self.assertTrue(result["ollama"]["available"])
        status, _, raw = self.request(path="/api/models")
        self.assertEqual(status, 200)
        self.assertEqual([model["name"] for model in json.loads(raw)["models"]],
                         ["qwen3:1.7b", "llama3.2:1b", "qwen3-4b-thinking-local:latest",
                          "qwen3:4b-thinking", "deepseek-r1-hf:1.5b", "opaque-local:latest"])

    def test_allowlisted_assets_and_security_headers(self):
        status, headers, raw = self.request(path="/")
        self.assertEqual(status, 200)
        self.assertIn(b"Local chat", raw)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path in ("/secret.txt", "/../app.py", "/%2e%2e/app.py", "/web/index.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)

    def test_blocks_foreign_origin_and_rebinding_host(self):
        cases = [{"Origin": "https://example.com"}, {"Origin": "null"},
                 {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                 {"Host": f"attacker.example:{self.server.server_port}"}]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.chat(headers=headers)[0], 403)
        self.assertEqual(self.upstream.received, [])
        status, _, _ = self.chat(headers={"Origin": f"http://127.0.0.1:{self.server.server_port}",
                                         "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)

    def test_validates_json_body_and_media_type(self):
        self.assertEqual(self.request("POST", "/api/chat", raw=b"not json")[0], 400)
        self.assertEqual(self.chat(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/chat", raw=b"x" * (app.MAX_BODY_BYTES + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/chat", raw=b"{}",
                                      headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_rejects_invalid_message_payloads_without_generation(self):
        invalid = [None, [], {"model": "not installed", "messages": []},
                   {"model": "qwen3:1.7b", "messages": []},
                   {"model": "qwen3:1.7b", "messages": [{"role": "tool", "content": "x"}]},
                   {"model": "qwen3:1.7b", "messages": [{"role": "user", "content": " "}]},
                   {"model": "qwen3:1.7b", "messages": [{"role": "system", "content": "x"}]},
                   {**self.chat_payload(), "temperature": True},
                   {**self.chat_payload(), "temperature": -1},
                   {**self.chat_payload(), "temperature": float("nan")},
                   {**self.chat_payload(), "think": "false"},
                   {**self.chat_payload(), "remote": "https://example.com"}]
        for payload in invalid:
            with self.subTest(payload=payload):
                status, _, _ = self.request("POST", "/api/chat", raw=json.dumps(payload).encode())
                self.assertEqual(status, 400)
        self.assertEqual(self.upstream.received, [])

    def test_bounds_messages_and_history(self):
        for messages in (
            [{"role": "user", "content": "x"}] * (app.MAX_MESSAGES + 1),
            [{"role": "user", "content": "x" * (app.MAX_MESSAGE_CHARS + 1)}],
            [{"role": "user", "content": "x" * 22000}] * 3,
        ):
            with self.subTest(count=len(messages)):
                self.assertEqual(self.chat({"model": "qwen3:1.7b", "messages": messages})[0], 400)

    def test_rejects_uninstalled_and_cloud_models(self):
        for model in ("missing:latest", "large:cloud", "remote:latest"):
            with self.subTest(model=model):
                status, _, raw = self.chat(self.chat_payload(model))
                self.assertEqual(status, 400)
                self.assertIn("not installed", json.loads(raw)["error"])
        self.assertEqual(self.upstream.received, [])

    def test_streams_tokens_thinking_and_metrics(self):
        status, headers, raw = self.chat()
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/x-ndjson"))
        self.assertEqual(events[0], {"type": "thinking", "content": "Checking."})
        self.assertEqual("".join(event["content"] for event in events if event["type"] == "token"), "Hello world!")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["eval_count"], 3)
        self.assertEqual(events[-1]["done_reason"], "stop")
        forwarded = self.upstream.received[-1]
        self.assertFalse(forwarded["think"])
        self.assertEqual(forwarded["options"]["num_ctx"], 4096)
        self.assertEqual(forwarded["options"]["num_predict"], 1024)
        self.assertEqual(forwarded["keep_alive"], "5m")

    def test_thinking_controls_match_model_capabilities(self):
        self.chat({**self.chat_payload(), "think": True})
        self.assertTrue(self.upstream.received[-1]["think"])
        self.assertEqual(self.upstream.received[-1]["options"]["num_predict"], 4096)
        for model in ("qwen3-4b-thinking-local:latest", "qwen3:4b-thinking"):
            with self.subTest(model=model):
                self.chat(self.chat_payload(model))
                self.assertNotIn("think", self.upstream.received[-1])
                self.assertEqual(self.upstream.received[-1]["options"]["num_predict"], 4096)
        self.chat({**self.chat_payload("llama3.2:1b"), "think": False})
        self.assertNotIn("think", self.upstream.received[-1])

    def test_imported_reasoner_uses_metadata_and_preserves_native_thinking(self):
        status, _, raw = self.request(path="/api/models")
        self.assertEqual(status, 200)
        models = {model["name"]: model for model in json.loads(raw)["models"]}
        self.assertEqual(models["deepseek-r1-hf:1.5b"]["reasoning"]["mode"], "default")
        self.assertEqual(models["opaque-local:latest"]["reasoning"]["mode"], "toggle")
        for requested_think in (False, True):
            with self.subTest(think=requested_think):
                status, _, raw = self.chat({**self.chat_payload("deepseek-r1-hf:1.5b"),
                                           "think": requested_think})
                self.assertEqual(status, 200)
                self.assertNotIn("think", self.upstream.received[-1])
                self.assertEqual(self.upstream.received[-1]["options"]["num_predict"], 4096)
                self.assertEqual(json.loads(raw.splitlines()[0])["type"], "thinking")
        # Boolean controls are usable even when the model name is unfamiliar.
        for requested_think in (False, True):
            self.chat({**self.chat_payload("opaque-local:latest"), "think": requested_think})
            self.assertEqual(self.upstream.received[-1]["think"], requested_think)
            self.assertEqual(self.upstream.received[-1]["options"]["num_predict"],
                             4096 if requested_think else 1024)

    def test_reasoning_metadata_cache_tracks_model_digest(self):
        self.request(path="/api/models")
        shown = list(self.upstream.shown)
        self.chat()
        self.request(path="/api/models")
        self.assertEqual(self.upstream.shown, shown)
        self.upstream.model_digest = "model-digest-2"
        self.request(path="/api/models")
        self.assertEqual(self.upstream.shown, shown + ["deepseek-r1-hf:1.5b"])

    def test_older_runtime_preserves_existing_qwen_controls(self):
        self.upstream.mode = "no-show"
        self.chat(self.chat_payload())
        self.assertFalse(self.upstream.received[-1]["think"])
        self.chat(self.chat_payload("qwen3-4b-thinking-local:latest"))
        self.assertNotIn("think", self.upstream.received[-1])
        self.assertEqual(self.upstream.received[-1]["options"]["num_predict"], 4096)

    def test_explicit_metadata_overrides_legacy_name_fallback(self):
        self.assertEqual(app.reasoning_profile("qwen3:thinking", {
            "thinking": {"values": [False], "default": False}})["mode"], "none")

    def test_upstream_http_error_stays_http_error(self):
        self.upstream.mode = "http-error"
        status, _, raw = self.chat()
        self.assertEqual(status, 502)
        self.assertIn("insufficient memory", json.loads(raw)["error"])

    def test_bad_and_incomplete_streams_emit_errors(self):
        for mode in ("malformed", "empty"):
            with self.subTest(mode=mode):
                self.upstream.mode = mode
                status, _, raw = self.chat()
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["type"], "error")
        self.upstream.mode = "normal"
        self.upstream.events = [{"error": "GPU memory exhausted"}]
        status, _, raw = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"type": "error", "error": "GPU memory exhausted"})

    def test_ollama_unavailable_is_helpful(self):
        with patch.object(self.server.ollama, "json", side_effect=app.APIError(503, "Ollama is not running.")):
            status, _, raw = self.request()
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(raw)["ollama"]["available"])
            status, _, raw = self.chat()
            self.assertEqual(status, 503)
            self.assertIn("not running", json.loads(raw)["error"])

    def test_generation_lock_prevents_parallel_model_loads(self):
        self.server.generation_lock.acquire()
        try:
            status, _, raw = self.chat()
            self.assertEqual(status, 409)
            self.assertIn("still running", json.loads(raw)["error"])
            self.assertEqual(self.upstream.received, [])
        finally:
            self.server.generation_lock.release()

    def test_client_disconnect_releases_lock_during_model_load(self):
        self.upstream.mode = "delayed"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("POST", "/api/chat", json.dumps(self.chat_payload()),
                           {"Content-Type": "application/json"})
        self.assertTrue(self.upstream.started.wait(1))
        self.assertTrue(self.server.generation_lock.locked())
        connection.close()
        deadline = time.monotonic() + 2
        while self.server.generation_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.server.generation_lock.locked())

    def test_client_reset_releases_lock_during_model_load(self):
        self.upstream.mode = "delayed"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("POST", "/api/chat", json.dumps(self.chat_payload()),
                           {"Content-Type": "application/json"})
        self.assertTrue(self.upstream.started.wait(1))
        self.assertTrue(self.server.generation_lock.locked())
        # A browser abort can reset the connection instead of sending a FIN.
        # Inject that recv error after making the socket readable, avoiding an
        # operating-system-dependent SO_LINGER reset in the regression check.
        with patch.object(socket.socket, "recv", side_effect=ConnectionResetError("Client reset")) as peek:
            connection.close()
            deadline = time.monotonic() + 1
            while self.server.generation_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(peek.called)
            self.assertFalse(self.server.generation_lock.locked())
        self.assertFalse(self.upstream.resume.is_set())

    def test_aborted_client_event_write_ends_cleanly_and_releases_lock(self):
        class AbortedClientHandler(app.ChatHandler):
            def setup(self):
                super().setup()
                write = self.wfile.write

                def disconnected_write(data):
                    if data.startswith(b'{"type":'):
                        raise ConnectionAbortedError(10053, "Client closed its streaming socket")
                    return write(data)

                self.wfile.write = disconnected_write

        # Keep the real request validation, Ollama proxy, and event serializer;
        # fail only the client socket write once the NDJSON stream has begun.
        self.server.RequestHandlerClass = AbortedClientHandler
        with patch.object(self.server, "handle_error") as unhandled:
            status, _, raw = self.chat()
            self.assertEqual(status, 200)
            self.assertEqual(raw, b"")
            self.assertEqual(len(self.upstream.received), 1)
            self.assertFalse(self.server.generation_lock.locked())
            unhandled.assert_not_called()


class ClientRestrictionTest(unittest.TestCase):
    def test_external_upstream_and_listen_addresses_are_rejected(self):
        for url in ("https://127.0.0.1:11434", "http://example.com:11434", "http://localhost:11434",
                    "http://127.0.0.1:11434/remote", "http://user@127.0.0.1:11434"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    app.OllamaClient(url)
        with self.assertRaises(ValueError):
            app.ChatServer(("0.0.0.0", 0))

    def test_real_connection_refusal_is_reported(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        client = app.OllamaClient(f"http://127.0.0.1:{port}")
        with self.assertRaises(app.APIError) as raised:
            client.json("/api/version")
        self.assertEqual(raised.exception.status, 503)
        self.assertIn("not running", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
