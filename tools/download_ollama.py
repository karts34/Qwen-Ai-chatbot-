"""Download the official portable runtime in resumable, verified chunks."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = ROOT / '.runtime' / 'downloads'
METADATA = ROOT / '.runtime' / 'ollama-release.json'
CHUNK = 32 * 1024 * 1024


def main():
    metadata = json.loads(METADATA.read_text(encoding='utf-8-sig'))
    url, size = metadata['url'], metadata['size']
    expected = metadata.get('digest', '').removeprefix('sha256:')
    if not expected or len(expected) != 64:
        raise RuntimeError('Official release is missing its SHA-256 digest.')
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    archive = DOWNLOADS / 'ollama-windows-amd64.zip'

    def valid_archive():
        if not archive.exists() or archive.stat().st_size != size:
            return False
        with archive.open('rb') as file:
            return hashlib.file_digest(file, 'sha256').hexdigest() == expected

    if valid_archive():
        print('Official runtime download already verified.', flush=True)
        return

    # Reuse complete chunks from an interrupted sequential download.
    partial_size = archive.stat().st_size if archive.exists() else 0
    if partial_size:
        with archive.open('rb') as source:
            for index in range(partial_size // CHUNK):
                part = DOWNLOADS / f'ollama-{index:03}.part'
                if not part.exists() or part.stat().st_size != CHUNK:
                    part.write_bytes(source.read(CHUNK))
                else:
                    source.seek(CHUNK, 1)

    def fetch(index):
        start = index * CHUNK
        end = min(start + CHUNK, size) - 1
        part = DOWNLOADS / f'ollama-{index:03}.part'
        target_size = end - start + 1
        if part.exists() and part.stat().st_size == target_size:
            return index
        for attempt in range(6):
            saved = part.stat().st_size if part.exists() else 0
            if saved > target_size:
                part.unlink()
                saved = 0
            request = urllib.request.Request(url, headers={
                'Range': f'bytes={start + saved}-{end}',
                'User-Agent': 'LocalChat-Setup',
            })
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status != 206:
                        raise RuntimeError('Download server did not honor the byte range.')
                    content_range = response.headers.get('Content-Range', '')
                    if content_range != f'bytes {start + saved}-{end}/{size}':
                        raise RuntimeError('Download server returned an unexpected byte range.')
                    with part.open('ab') as target:
                        while block := response.read(1024 * 1024):
                            target.write(block)
                if part.stat().st_size == target_size:
                    return index
                raise RuntimeError('Incomplete chunk received.')
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError('Download exhausted retries.')

    count = (size + CHUNK - 1) // CHUNK
    print(f'Downloading official Ollama {metadata["version"]} ({size / 1e9:.2f} GB)...', flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(fetch, index) for index in range(count)]
        for completed, job in enumerate(as_completed(jobs), 1):
            job.result()
            print(f'Runtime download: {completed}/{count} chunks', flush=True)
    assembled = DOWNLOADS / 'ollama-windows-amd64.zip.assembling'
    with assembled.open('wb') as destination:
        for index in range(count):
            with (DOWNLOADS / f'ollama-{index:03}.part').open('rb') as source:
                while block := source.read(1024 * 1024):
                    destination.write(block)
    with assembled.open('rb') as file:
        actual = hashlib.file_digest(file, 'sha256').hexdigest()
    if actual != expected:
        raise RuntimeError('Runtime checksum mismatch; no executable will be launched.')
    assembled.replace(archive)
    for index in range(count):
        (DOWNLOADS / f'ollama-{index:03}.part').unlink()
    print('Runtime SHA-256 verified.', flush=True)


if __name__ == '__main__':
    main()
