"""Install a pinned Hugging Face GGUF with resumable ranges and SHA-256 checks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote

if __package__:
    from .download_models import CHUNK_BYTES, CurlClient, fetch_chunk, response_headers, sha256_file
else:
    from download_models import CHUNK_BYTES, CurlClient, fetch_chunk, response_headers, sha256_file


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".runtime" / "downloads" / "huggingface"
DEFAULT_REPO = "unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF"
DEFAULT_FILENAME = "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf"
ORIGINAL_REPO = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"
PART_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
FILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.gguf\Z", re.IGNORECASE)
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                  *(f"LPT{n}" for n in range(1, 10))}
MAX_METADATA_BYTES = 16 * 1024 * 1024


class BoundedCurlClient(CurlClient):
    """Keep the inherited chunk retry fallback inside a wall-clock bound."""

    def urllib_download(self, url: str, output: Path, headers: Path,
                        *, byte_range: str | None = None, accept: str | None = None) -> tuple[int, str]:
        # fetch_chunk retries this transport on its final attempts. A curl
        # subprocess has an absolute deadline, unlike repeated socket reads.
        return self.download(url, output, headers, byte_range=byte_range,
                             accept=accept, max_time=90)


def validate_repo(value: str) -> str:
    parts = value.split("/")
    if (len(parts) != 2 or any(not PART_PATTERN.fullmatch(part) for part in parts)
            or any(part.endswith(".") or ".." in part for part in parts)):
        raise ValueError("Use a Hugging Face repository in owner/name form.")
    if any(part.split(".", 1)[0].upper() in RESERVED_NAMES for part in parts):
        raise ValueError("Repository contains a reserved Windows path name.")
    return value


def validate_filename(value: str) -> str:
    if (not FILE_PATTERN.fullmatch(value) or ".." in value
            or value.split(".", 1)[0].upper() in RESERVED_NAMES):
        raise ValueError("The model filename must be a plain .gguf basename.")
    return value


def resolve_url(repo: str, revision: str, filename: str) -> str:
    validate_repo(repo)
    if not REVISION_PATTERN.fullmatch(revision):
        raise ValueError("Hugging Face did not return a pinned commit SHA.")
    return f"https://huggingface.co/{repo}/resolve/{revision}/{quote(filename, safe='')}?download=true"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".installing")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_small(client: CurlClient, url: str, destination: Path,
                *, max_bytes: int = MAX_METADATA_BYTES) -> bytes:
    """Fetch public metadata with three bounded requests and no executable code."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    transfer = destination.with_name(destination.name + ".transfer")
    headers = destination.with_name(destination.name + ".headers")
    detail = "The server did not return the requested metadata."
    for attempt in range(3):
        transfer.unlink(missing_ok=True)
        headers.unlink(missing_ok=True)
        code, detail = client.download(url, transfer, headers, max_time=30)
        status, _ = response_headers(headers)
        if transfer.exists() and transfer.stat().st_size > max_bytes:
            transfer.unlink(missing_ok=True)
            headers.unlink(missing_ok=True)
            raise ValueError("Hugging Face metadata exceeds the allowed size.")
        if code == 0 and status == 200 and transfer.exists():
            raw = transfer.read_bytes()
            transfer.replace(destination)
            headers.unlink(missing_ok=True)
            return raw
        if status in (400, 401, 403, 404):
            break
        if attempt < 2:
            time.sleep(attempt + 1)
    transfer.unlink(missing_ok=True)
    headers.unlink(missing_ok=True)
    raise RuntimeError(f"Could not fetch Hugging Face metadata (HTTP {status}): {detail}")


def descriptor_from_api(data: dict, repo: str, filename: str) -> dict:
    if not isinstance(data, dict) or data.get("id") != repo:
        raise ValueError("Hugging Face returned metadata for a different repository.")
    revision = data.get("sha")
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise ValueError("Hugging Face model metadata is missing its commit SHA.")
    siblings = data.get("siblings")
    if not isinstance(siblings, list):
        raise ValueError("Hugging Face model metadata is missing its files.")
    matches = [entry for entry in siblings
               if isinstance(entry, dict) and entry.get("rfilename") == filename]
    if len(matches) != 1 or not isinstance(matches[0].get("lfs"), dict):
        raise ValueError("The selected GGUF is missing its LFS checksum metadata.")
    lfs = matches[0]["lfs"]
    digest, size = lfs.get("sha256"), lfs.get("size")
    if (not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest)
            or isinstance(size, bool) or not isinstance(size, int) or size < 4):
        raise ValueError("Hugging Face returned an invalid GGUF digest or size.")
    return {"format": 1, "repository": repo, "filename": filename,
            "revision": revision, "sha256": digest, "size": size,
            "resolve_url": resolve_url(repo, revision, filename),
            "metadata_url": f"https://huggingface.co/api/models/{repo}?blobs=true",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "provenance": []}


def validate_descriptor(data: dict, repo: str, filename: str) -> dict:
    if (not isinstance(data, dict) or data.get("format") != 1
            or data.get("repository") != repo or data.get("filename") != filename):
        raise ValueError("Cached Hugging Face metadata belongs to another model.")
    revision, digest, size = data.get("revision"), data.get("sha256"), data.get("size")
    if (not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision)
            or not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest)
            or isinstance(size, bool) or not isinstance(size, int) or size < 4
            or data.get("resolve_url") != resolve_url(repo, revision, filename)):
        raise ValueError("Cached Hugging Face metadata has an invalid source or digest.")
    return data


def load_descriptor(client: CurlClient, repo: str, filename: str,
                    *, refresh: bool = False) -> tuple[dict, Path]:
    directory = CACHE / "metadata" / repo
    source = directory / (filename + ".source.json")
    if source.exists() and not refresh:
        data = validate_descriptor(json.loads(source.read_text(encoding="utf-8")), repo, filename)
        print(f"Using pinned Hugging Face commit {data['revision'][:12]}.", flush=True)
        return data, source
    raw = fetch_small(client, f"https://huggingface.co/api/models/{repo}?blobs=true",
                      directory / "model-api.json")
    data = descriptor_from_api(json.loads(raw), repo, filename)
    atomic_json(source, data)
    return data, source


def is_verified(path: Path, descriptor: dict) -> bool:
    if not path.is_file() or path.stat().st_size != descriptor["size"]:
        return False
    with path.open("rb") as source:
        if source.read(4) != b"GGUF":
            return False
    return sha256_file(path) == descriptor["sha256"]


def save_provenance(client: CurlClient, descriptor: dict, source: Path) -> None:
    """Keep model-card and license text, without importing repository code."""
    repo, revision = descriptor["repository"], descriptor["revision"]
    directory = source.parent / revision
    documents = descriptor.setdefault("provenance", [])
    targets = [("README.md", repo, revision, "README.md")]
    if repo == DEFAULT_REPO:
        try:
            original_path = source.parent / "deepseek-original-api.json"
            if original_path.exists():
                original = json.loads(original_path.read_text(encoding="utf-8"))
            else:
                original = json.loads(fetch_small(client,
                    f"https://huggingface.co/api/models/{ORIGINAL_REPO}", original_path))
            original_revision = original.get("sha")
            if (original.get("id") != ORIGINAL_REPO or not isinstance(original_revision, str)
                    or not REVISION_PATTERN.fullmatch(original_revision)):
                raise ValueError("Original DeepSeek metadata did not contain its pinned commit.")
            descriptor["original_repository"] = ORIGINAL_REPO
            descriptor["original_revision"] = original_revision
            targets.append(("LICENSE.deepseek.txt", ORIGINAL_REPO, original_revision, "LICENSE"))
            targets.append(("README.deepseek.md", ORIGINAL_REPO, original_revision, "README.md"))
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Original-model provenance unavailable: {error}", flush=True)
    for local_name, document_repo, document_revision, remote_name in targets:
        path = directory / local_name
        url = resolve_url(document_repo, document_revision, remote_name)
        try:
            raw = path.read_bytes() if path.exists() else fetch_small(client, url, path, max_bytes=1024 * 1024)
            raw.decode("utf-8")
            record = {"filename": local_name, "repository": document_repo,
                      "revision": document_revision, "url": url,
                      "sha256": hashlib.sha256(raw).hexdigest()}
            documents[:] = [item for item in documents if item.get("filename") != local_name]
            documents.append(record)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Optional provenance {local_name} unavailable: {error}", flush=True)
    atomic_json(source, descriptor)


def install(client: CurlClient, descriptor: dict, destination: Path, workers: int) -> None:
    size, digest = descriptor["size"], descriptor["sha256"]
    if is_verified(destination, descriptor):
        print(f"Verified existing GGUF: {destination} (offline; no download needed).", flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    directory = CACHE / "ranges" / ("sha256-" + digest)
    directory.mkdir(parents=True, exist_ok=True)
    count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    print(f"Downloading {descriptor['filename']}: {size / 1e6:.1f} MB in {count} ranges.", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(fetch_chunk, client, descriptor["resolve_url"], directory, index, size)
                for index in range(count)]
        try:
            for completed, job in enumerate(as_completed(jobs), 1):
                job.result()
                print(f"Hugging Face GGUF: {completed}/{count} ranges complete", flush=True)
        except BaseException:
            for job in jobs:
                job.cancel()
            raise
    # Assemble alongside the destination so os.replace remains atomic even if
    # --output selects a different volume from the resumable download cache.
    assembled = destination.with_name(destination.name + ".installing")
    checksum = hashlib.sha256()
    with assembled.open("wb") as output:
        for index in range(count):
            with (directory / f"{index:06}.part").open("rb") as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
                    checksum.update(block)
    with assembled.open("rb") as source:
        valid_magic = source.read(4) == b"GGUF"
    if assembled.stat().st_size != size or checksum.hexdigest() != digest or not valid_magic:
        assembled.unlink(missing_ok=True)
        for index in range(count):
            (directory / f"{index:06}.part").unlink(missing_ok=True)
        raise RuntimeError("GGUF size, SHA-256, or file signature failed; invalid range cache cleared.")
    assembled.replace(destination)
    for index in range(count):
        (directory / f"{index:06}.part").unlink(missing_ok=True)
    print(f"SHA-256 and GGUF signature verified: {digest}", flush=True)
    print(f"Installed {destination}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--filename", default=DEFAULT_FILENAME)
    parser.add_argument("--output", type=Path, help="Local .gguf destination; default .models/huggingface/FILENAME.")
    parser.add_argument("--workers", type=int, default=8, help="Download workers, from 1 to 8.")
    parser.add_argument("--refresh-metadata", action="store_true", help="Pin the latest repository commit instead of resuming the cached revision.")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("Workers must be between 1 and 8.")
    try:
        repo, filename = validate_repo(args.repo), validate_filename(args.filename)
        destination = args.output or ROOT / ".models" / "huggingface" / filename
        validate_filename(destination.name)
        destination = destination.resolve()
        # Avoid even initializing the network transport when a cached source
        # describes a complete local installation. This supports offline setup.
        source = CACHE / "metadata" / repo / (filename + ".source.json")
        if source.exists() and not args.refresh_metadata:
            descriptor = validate_descriptor(json.loads(source.read_text(encoding="utf-8")), repo, filename)
            if is_verified(destination, descriptor):
                atomic_json(destination.with_name(destination.name + ".source.json"), descriptor)
                print(f"Verified existing GGUF: {destination} (offline; no download needed).", flush=True)
                return 0
        client = BoundedCurlClient()
        descriptor, source = load_descriptor(client, repo, filename, refresh=args.refresh_metadata)
        save_provenance(client, descriptor, source)
        install(client, descriptor, destination, args.workers)
        atomic_json(destination.with_name(destination.name + ".source.json"), descriptor)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Hugging Face installation failed: {error}", flush=True)
        print("Rerun to resume the same pinned model and its cached 8 MB ranges.", flush=True)
        return 1
    except KeyboardInterrupt:
        print("Download interrupted; rerun to resume cached ranges.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
