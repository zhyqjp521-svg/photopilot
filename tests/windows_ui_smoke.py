"""Real local-UI import smoke boundary for the packaged Windows EXE.

Starts PhotoPilot's own loopback server, performs the same HTTP requests as the
desktop UI, waits for a real image scan, and then exits even after an error.
No user project or original photo is touched.
"""
from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import sys
import time
from pathlib import Path
from urllib.parse import urlencode


def request(port: int, method: str, path: str, payload=None, host: str = '127.0.0.1'):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        body = None if payload is None else json.dumps(payload)
        headers = {'Host': f'{host}:{port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, data
    finally:
        connection.close()


def raw_request(port: int, method: str, path: str, body: bytes | None = None, *,
                headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        base_headers = {'Host': f'127.0.0.1:{port}'}
        base_headers.update(headers or {})
        connection.request(method, path, body=body, headers=base_headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def json_request(port: int, method: str, path: str, payload=None):
    status, raw = request(port, method, path, payload)
    if status != 200:
        raise RuntimeError(f'{method} {path} returned {status}: {raw[:1000]!r}')
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--photo', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    photo = str(Path(args.photo).resolve())
    evidence = {'photo': photo, 'port': args.port}
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                status, _ = request(args.port, 'GET', '/api/presets')
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('packaged UI server did not start')
            time.sleep(.25)
        evidence['presets'] = len(json_request(args.port, 'GET', '/api/presets')['presets'])
        for host in ('127.0.0.1', 'localhost'):
            status, _ = request(args.port, 'GET', '/api/presets', host=host)
            if status != 200:
                raise RuntimeError(f'packaged UI rejected local Host {host}: {status}')
        status, _ = request(args.port, 'GET', '/api/presets', host='evil.example')
        if status != 403:
            raise RuntimeError(f'packaged UI accepted non-local Host: {status}')
        evidence['host_policy'] = 'loopback-only'
        root_status, root = raw_request(args.port, 'GET', '/')
        if root_status != 200 or b'PhotoPilot' not in root:
            raise RuntimeError(f'packaged UI page failed: {root_status}')
        image = Path(photo).read_bytes()
        batch = 'windows-ui-smoke'
        status, uploaded = raw_request(
            args.port, 'POST', '/api/upload', image,
            headers={'X-Filename': 'ui-import.jpg', 'X-Batch': batch,
                     'Content-Type': mimetypes.guess_type('ui-import.jpg')[0] or 'image/jpeg'})
        if status != 200:
            raise RuntimeError(f'/api/upload returned {status}: {uploaded[:1000]!r}')
        upload_data = json.loads(uploaded)
        uploaded_path = upload_data.get('path')
        if not uploaded_path or not Path(uploaded_path).is_file():
            raise RuntimeError(f'/api/upload did not persist the image: {upload_data}')
        evidence['upload'] = {'path': uploaded_path, 'bytes': Path(uploaded_path).stat().st_size}
        upload_scan = json_request(args.port, 'POST', '/api/scan', {
            'folder': str(Path(uploaded_path).parent), 'paths': [uploaded_path],
            'faces': False, 'ai': False, 'recursive': False})
        deadline = time.monotonic() + 30
        while True:
            upload_status = json_request(
                args.port, 'GET', '/api/scan_status?' + urlencode({'job': upload_scan['job']}))
            if upload_status.get('finished'):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'uploaded image scan did not finish: {upload_status}')
            time.sleep(.25)
        uploaded_final = upload_status.get('final') or []
        if len(uploaded_final) != 1:
            raise RuntimeError(f'uploaded image scan is incomplete: {uploaded_final}')
        evidence['upload_scan'] = {'total': upload_status['total'],
                                   'photos': len(uploaded_final)}
        imported = json_request(args.port, 'POST', '/api/import_paths', {
            'paths': [photo], 'faces': False, 'ai': False, 'recursive': False})
        evidence['import'] = {key: imported.get(key) for key in ('count', 'file_mode', 'folder')}
        if imported.get('count') != 1:
            raise RuntimeError(f'expected one imported image, got {imported.get("count")}')
        job = imported.get('job')
        deadline = time.monotonic() + 30
        while True:
            status_data = json_request(
                args.port, 'GET', '/api/scan_status?' + urlencode({'job': job}))
            if status_data.get('finished'):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'scan did not finish: {status_data}')
            time.sleep(.25)
        photos = status_data.get('final') or []
        evidence['scan'] = {
            'finished': status_data['finished'],
            'total': status_data['total'],
            'photos': len(photos),
            'path': photos[0]['path'] if photos else None,
        }
        if status_data['total'] != 1 or len(photos) != 1:
            raise RuntimeError(f'packaged UI scan result is incomplete: {evidence["scan"]}')
        if Path(photos[0]['path']).resolve() != Path(photo).resolve():
            raise RuntimeError('packaged UI imported a different photo')
        thumb_status, thumb = raw_request(
            args.port, 'GET', '/api/thumb?' + urlencode({'p': photo, 's': 360}))
        if thumb_status != 200 or len(thumb) <= 1000:
            raise RuntimeError(f'packaged UI thumbnail failed: {thumb_status}/{len(thumb)}')
        evidence['thumb'] = {'status': thumb_status, 'bytes': len(thumb)}
        evidence['ok'] = True
        return 0
    except Exception as exc:
        print(f'UI import smoke failed: {exc}', file=sys.stderr, flush=True)
        evidence['error'] = str(exc)
        return 1
    finally:
        Path(args.output).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
