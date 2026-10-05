"""Resume official Ollama model downloads in small, SHA-256-verified ranges."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://registry.ollama.ai"
CACHE = ROOT / ".runtime" / "downloads" / "models"
MODEL_ROOT = ROOT / ".models"
CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_MODELS = ("qwen3:1.7b", "llama3.2:1b", "gemma3:1b")
DIGEST_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def parse_model(value: str) -> tuple[str, str]:
    name, separator, tag = value.partition(":")
    tag = tag if separator else "latest"
    if not NAME_PATTERN.fullmatch(name) or not NAME_PATTERN.fullmatch(tag):
        raise ValueError("Use a local library model tag such as qwen3:1.7b.")
    return name, tag


def response_headers(path: Path) -> tuple[int, dict[str, str]]:
    """Return the final HTTP response, after any proxy/registry redirects."""
    status, headers = 0, {}
    if not path.exists():
        return status, headers
    for line in path.read_text(encoding="iso-8859-1").splitlines():
        match = re.match(r"HTTP/\S+\s+(\d{3})", line)
        if match:
            status, headers = int(match[1]), {}
        elif ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers


class CurlClient:
    def __init__(self):
        self.executable = shutil.which("curl.exe") or shutil.which("curl")
        if not self.executable:
            raise RuntimeError("curl.exe is required for resumable model downloads.")
        self._family = "-4"
        self._lock = threading.Lock()

    def download(self, url: str, output: Path, headers: Path, *, byte_range: str | None = None,
                 accept: str | None = None, max_time: int = 120) -> tuple[int, str]:
        with self._lock:
            family = self._family
        command = [self.executable]
        if family:
            command.append(family)
        command += ["--location", "--fail", "--silent", "--show-error",
                    "--connect-timeout", "5", "--max-time", str(max_time),
                    "--speed-time", "30", "--speed-limit", "1024",
                    "--dump-header", str(headers), "--output", str(output)]
        if byte_range:
            command += ["--header", "Range: bytes=" + byte_range]
        if accept:
            command += ["--header", "Accept: " + accept]
        command.append(url)
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=max_time + 15, errors="replace")
            code, detail = completed.returncode, completed.stderr.strip()
        except subprocess.TimeoutExpired:
            code, detail = 28, "Download process exceeded its time limit."
        if family == "-4" and code and (not output.exists() or output.stat().st_size == 0):
            # Some connections reach the registry only over IPv6. Try IPv4
            # first, then let curl choose a reachable address family.
            with self._lock:
                if self._family == "-4":
                    self._family = None
                    print("IPv4 connection failed; allowing curl to select the address family.", flush=True)
        return code, detail[-1000:]

    def urllib_download(self, url: str, output: Path, headers: Path,
                        *, byte_range: str | None = None, accept: str | None = None) -> tuple[int, str]:
        """Last-resort default-family transport, retaining partial response bytes."""
        request_headers = {"User-Agent": "LocalChat-Setup"}
        if byte_range:
            request_headers["Range"] = "bytes=" + byte_range
        if accept:
            request_headers["Accept"] = accept
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                headers.write_text("HTTP/1.1 " + str(response.status) + "\r\n" +
                                   "\r\n".join(f"{key}: {value}" for key, value in response.headers.items()) +
                                   "\r\n\r\n", encoding="iso-8859-1")
                with output.open("wb") as destination:
                    while block := response.read(256 * 1024):
                        destination.write(block)
            return 0, ""
        except (OSError, http.client.HTTPException, ValueError) as error:
            return 1, str(error)[-1000:]


def validate_manifest(raw: bytes) -> tuple[dict, list[dict]]:
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ValueError("Registry returned an invalid model manifest.")
    if not isinstance(data.get("config"), dict) or not isinstance(data.get("layers"), list):
        raise ValueError("Model manifest is missing its config or layers.")
    blobs, known = [], {}
    for descriptor in [data["config"], *data["layers"]]:
        if not isinstance(descriptor, dict):
            raise ValueError("Model manifest contains an invalid blob descriptor.")
        digest, size = descriptor.get("digest"), descriptor.get("size")
        if (not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int) or size <= 0):
            raise ValueError("Model manifest contains an invalid blob digest or size.")
        if digest in known and known[digest] != size:
            raise ValueError("Manifest contains conflicting sizes for the same blob.")
        if digest not in known:
            known[digest] = size
            blobs.append({"digest": digest, "size": size})
    return data, blobs


def load_manifest(client: CurlClient, name: str, tag: str, *, refresh: bool = False) -> tuple[bytes, list[dict]]:
    cached = CACHE / "manifests" / name / (tag + ".json")
    cached.parent.mkdir(parents=True, exist_ok=True)
    if cached.exists() and not refresh:
        raw = cached.read_bytes()
        _, blobs = validate_manifest(raw)
        print(f"Using cached official manifest for {name}:{tag}.", flush=True)
        return raw, blobs
    temporary = cached.with_suffix(".transfer")
    headers = cached.with_suffix(".headers")
    detail = "Unknown manifest download failure."
    for attempt in range(5):
        temporary.unlink(missing_ok=True)
        headers.unlink(missing_ok=True)
        url = f"{REGISTRY}/v2/library/{name}/manifests/{tag}"
        if attempt < 2:
            code, detail = client.download(
                url, temporary, headers,
                accept="application/vnd.docker.distribution.manifest.v2+json", max_time=60)
        else:
            code, detail = client.urllib_download(
                url, temporary, headers, accept="application/vnd.docker.distribution.manifest.v2+json")
        status, _ = response_headers(headers)
        if code == 0 and status == 200 and temporary.exists():
            raw = temporary.read_bytes()
            _, blobs = validate_manifest(raw)
            temporary.replace(cached)
            headers.unlink(missing_ok=True)
            return raw, blobs
        if attempt < 4:
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Could not fetch the official {name}:{tag} manifest: {detail}")


def fetch_chunk(client: CurlClient, url: str, directory: Path, index: int, size: int) -> int:
    start = index * CHUNK_BYTES
    end = min(start + CHUNK_BYTES, size) - 1
    expected = end - start + 1
    part = directory / f"{index:06}.part"
    transfer = directory / f"{index:06}.transfer"
    headers = directory / f"{index:06}.headers"

    def recover_transfer() -> int:
        """Checkpoint bytes from a completed or interrupted curl subprocess."""
        saved = part.stat().st_size if part.exists() else 0
        if not transfer.exists():
            return 0
        received = transfer.stat().st_size
        status, fields = response_headers(headers)
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", fields.get("content-range", ""))
        valid = (status == 206 and match is not None
                 and int(match[1]) == start + saved
                 and int(match[2]) <= end and int(match[2]) >= start + saved
                 and int(match[3]) == size
                 and 0 < received <= int(match[2]) - int(match[1]) + 1
                 and saved + received <= expected)
        # A server may return the whole object with 200 for a tiny blob. Accept
        # it only when it exactly matches that blob's complete advertised size.
        valid |= (status == 200 and start == 0 and saved == 0 and end == size - 1
                  and received == size and size <= CHUNK_BYTES)
        if valid:
            with part.open("ab") as destination, transfer.open("rb") as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
                destination.flush()
        transfer.unlink(missing_ok=True)
        headers.unlink(missing_ok=True)
        return received if valid else 0

    if part.exists() and part.stat().st_size > expected:
        part.unlink()
    recover_transfer()
    detail = "The server did not return a valid byte range."
    for attempt in range(10):
        saved = part.stat().st_size if part.exists() else 0
        if saved == expected:
            return index
        if attempt in (4, 7, 9):
            code, detail = client.urllib_download(url, transfer, headers, byte_range=f"{start + saved}-{end}")
        else:
            code, detail = client.download(url, transfer, headers, byte_range=f"{start + saved}-{end}")
        progress = recover_transfer()
        if part.exists() and part.stat().st_size == expected:
            return index
        if not code and not progress:
            detail = "The server did not honor the requested byte range."
        if attempt < 9:
            time.sleep(0.2 if progress else min(2 ** attempt, 8))
    raise RuntimeError(f"Chunk {index + 1} failed after retries: {detail}")


def install_blob(client: CurlClient, pool: ThreadPoolExecutor, name: str, descriptor: dict) -> None:
    digest, size = descriptor["digest"], descriptor["size"]
    hex_digest = DIGEST_PATTERN.fullmatch(digest)[1]
    destination = MODEL_ROOT / "blobs" / ("sha256-" + hex_digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size == size and sha256_file(destination) == hex_digest:
        print(f"Verified existing blob {hex_digest[:12]} ({size / 1e6:.1f} MB).", flush=True)
        return
    directory = CACHE / ("sha256-" + hex_digest)
    directory.mkdir(parents=True, exist_ok=True)
    count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    print(f"Downloading {name} blob {hex_digest[:12]}: {size / 1e6:.1f} MB in {count} ranges.", flush=True)
    url = f"{REGISTRY}/v2/library/{name}/blobs/{digest}"
    jobs = [pool.submit(fetch_chunk, client, url, directory, index, size) for index in range(count)]
    try:
        for completed, job in enumerate(as_completed(jobs), 1):
            job.result()
            print(f"Blob {hex_digest[:12]}: {completed}/{count} ranges complete", flush=True)
    except BaseException:
        for job in jobs:
            job.cancel()
        raise
    assembled = directory / "verified.blob"
    checksum = hashlib.sha256()
    with assembled.open("wb") as output:
        for index in range(count):
            with (directory / f"{index:06}.part").open("rb") as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
                    checksum.update(block)
    if assembled.stat().st_size != size or checksum.hexdigest() != hex_digest:
        assembled.unlink(missing_ok=True)
        # These are this utility's own cached ranges, never Ollama's partials.
        for index in range(count):
            (directory / f"{index:06}.part").unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch for {digest}; invalid range cache cleared. Rerun to download again.")
    assembled.replace(destination)
    print(f"SHA-256 verified and installed {hex_digest[:12]}.", flush=True)
    for index in range(count):
        (directory / f"{index:06}.part").unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--workers", type=int, default=8, help="Global curl workers, maximum 8.")
    parser.add_argument("--refresh-manifests", action="store_true",
                        help="Fetch current manifests instead of resuming cached versions.")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("Workers must be between 1 and 8.")
    try:
        models = [parse_model(value) for value in args.models]
        client = CurlClient()
        CACHE.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for name, tag in models:
                raw, blobs = load_manifest(client, name, tag, refresh=args.refresh_manifests)
                for descriptor in blobs:
                    install_blob(client, pool, name, descriptor)
                manifest = MODEL_ROOT / "manifests" / "registry.ollama.ai" / "library" / name / tag
                manifest.parent.mkdir(parents=True, exist_ok=True)
                temporary = CACHE / "manifests" / name / (tag + ".installing")
                temporary.write_bytes(raw)
                temporary.replace(manifest)
                print(f"Installed official model {name}:{tag}; every blob passed SHA-256 verification.", flush=True)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Model installation failed: {error}", flush=True)
        print("Rerun this command to resume completed and partial 8 MB ranges.", flush=True)
        return 1
    except KeyboardInterrupt:
        print("Download interrupted. Rerun to resume the cached byte ranges.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
