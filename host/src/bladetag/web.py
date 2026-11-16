"""Local, persistent recording library. Run with python -m bladetag.web."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit
import zipfile

KINDS = {'walking-report', 'cycling-report', 'report', 'walking3d', 'cycling3d', 'viz3d', 'overview'}
DEFAULTS = {'walking': 'walking-report', 'cycling': 'cycling-report', 'skating': 'report'}


class Library:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.file = self.root / 'library.json'
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {'datasets': {}, 'jobs': {}}
        for job in self.state['jobs'].values():
            if job['status'] in ('queued', 'running'):
                job.update(status='failed', error='Server stopped during analysis. Retry to continue.')
        self.save()

    def save(self):
        with self.lock:
            tmp = self.file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state, indent=2))
            tmp.replace(self.file)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def ingest(self, source, name=None):
        from .session import load_session
        source = Path(source)
        # Store folders as ZIPs; never extract uploaded paths into the filesystem.
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            archive = Path(temp) / 'recording.zip'
            if source.is_dir():
                if not (source / 'session.json').is_file():
                    raise ValueError('Choose a session folder containing session.json')
                with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                    for p in sorted(source.rglob('*')):
                        if p.is_file() and not p.is_symlink():
                            z.write(p, p.relative_to(source))
            else:
                shutil.copyfile(source, archive)
            if not zipfile.is_zipfile(archive):
                raise ValueError('Upload a session ZIP export')
            with zipfile.ZipFile(archive) as z:
                if sum(i.file_size for i in z.infolist()) > 2 * 1024**3:
                    raise ValueError('Uncompressed session exceeds 2 GiB')
            session = load_session(archive)
            identity = hashlib.sha256(archive.read_bytes()).hexdigest()[:24]
            with self.lock:
                if identity in self.state['datasets']:
                    return identity
                folder = self.root / 'datasets'
                folder.mkdir(exist_ok=True)
                shutil.copyfile(archive, folder / f'{identity}.zip')
                self.state['datasets'][identity] = dict(id=identity, name=name or source.name,
                    activity=session.activity, start=session.meta.get('start_utc', ''),
                    gear=session.meta.get('gear', ''), tags=len(session.tags))
                self.save()
            kind = DEFAULTS.get(session.activity)
            if kind:
                self.enqueue(kind, [identity])
            return identity

    def enqueue(self, kind, ids):
        if kind not in KINDS or not isinstance(ids, list) or not ids:
            raise ValueError('Choose a report and at least one recording')
        ids = sorted(set(ids))
        with self.lock:
            if any(i not in self.state['datasets'] for i in ids):
                raise ValueError('Unknown recording')
            if kind not in ('report', 'walking-report', 'viz3d') and len(ids) != 1:
                raise ValueError('This view needs exactly one recording')
            expected = {'report': 'skating', 'walking-report': 'walking', 'walking3d': 'walking',
                        'cycling-report': 'cycling', 'cycling3d': 'cycling', 'viz3d': 'skating'}.get(kind)
            if expected and any(self.state['datasets'][i]['activity'] != expected for i in ids):
                raise ValueError(f'This view needs {expected} recordings')
            key = hashlib.sha256(json.dumps([kind, ids]).encode()).hexdigest()[:24]
            previous = self.state['jobs'].get(key)
            if previous and previous['status'] != 'failed':
                return key
            self.state['jobs'][key] = dict(id=key, kind=kind, datasets=ids, status='queued', error='')
            self.save()
            self.pool.submit(self.render, key)
            return key

    def render(self, key):
        with self.lock:
            job = self.state['jobs'][key]
            job['status'] = 'running'
            self.save()
        folder = self.root / 'reports' / key
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / 'index.html'
        paths = [str(self.root / 'datasets' / f'{i}.zip') for i in job['datasets']]
        command = [sys.executable, '-m', 'bladetag.cli']
        command += ['explore', *paths, '--html', str(output)] if job['kind'] == 'overview' else [job['kind'], *paths, '-o', str(output)]
        try:
            if job['kind'] == 'overview':
                from .session import load_session
                tags = load_session(paths[0]).tags
                if tags:
                    command += ['--tag', tags[0].meta.address]
            with (folder / 'analysis.log').open('w') as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
            if result.returncode or not output.is_file():
                raise ValueError((folder / 'analysis.log').read_text()[-4000:] or 'Analysis produced no page')
            if job['kind'] == 'viz3d':
                output.write_text(offline_skating(output.read_text()))
            with self.lock:
                job['status'] = 'ready'
                self.save()
        except Exception as exc:
            with self.lock:
                job.update(status='failed', error=str(exc))
                self.save()


def offline_skating(page):
    """Embed pinned renderer dependencies; no network is needed to view/export."""
    import re
    for name in ('three.min.js', 'OrbitControls.js'):
        url = ('https://cdn.jsdelivr.net/npm/three@0.147.0/' +
               ('build/' if name == 'three.min.js' else 'examples/js/controls/') + name)
        script = resources.files('bladetag').joinpath('vendor', name).read_text()
        script = script.replace('</script', '<\\/script')
        page = page.replace(f'<script src="{url}"></script>', '<script>' + script + '</script>')
    page = re.sub(r'<link[^>]*https://fonts.googleapis.com[^>]*>\n?', '', page)
    license_text = resources.files('bladetag').joinpath('vendor', 'THREE-LICENSE.txt').read_text()
    return page.replace('</head>', '<!-- Three.js license\n' + license_text + '\n-->\n</head>')


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data, mime='application/json', download=False):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="bladetag-report.html"')
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                            f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {'error': 'Use the localhost address'})
        path = urlsplit(self.path).path
        if path == '/':
            return self.respond(200, resources.files('bladetag').joinpath('web.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/api/library':
            return self.respond(200, self.server.library.snapshot())
        parts = path.strip('/').split('/')
        if len(parts) == 2 and parts[0] in ('view', 'export'):
            job = self.server.library.snapshot()['jobs'].get(parts[1])
            if job and job['status'] == 'ready':
                data = (self.server.library.root / 'reports' / job['id'] / 'index.html').read_bytes()
                return self.respond(200, data, 'text/html; charset=utf-8', parts[0] == 'export')
        self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        # JSON/custom upload headers plus Origin validation prevent cross-site writes.
        if not self.valid_host():
            return self.respond(403, {'error': 'Use the localhost address'})
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{self.headers.get("Host")}':
            return self.respond(403, {'error': 'Cross-origin request rejected'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 512 * 1024**2:
                raise ValueError('Request must be between 1 byte and 512 MiB')
            path = urlsplit(self.path).path
            if path == '/api/upload':
                if self.headers.get('Content-Type') != 'application/zip':
                    raise ValueError('Expected application/zip')
                with tempfile.TemporaryDirectory(dir=self.server.library.root) as temp:
                    file = Path(temp) / 'upload.zip'
                    remaining = length
                    with file.open('wb') as dest:
                        while remaining:
                            chunk = self.rfile.read(min(1024**2, remaining))
                            if not chunk:
                                raise ValueError('Incomplete upload')
                            dest.write(chunk)
                            remaining -= len(chunk)
                    identity = self.server.library.ingest(file, unquote(self.headers.get('X-Filename', 'Recording')))
                    return self.respond(200, {'id': identity})
            if self.headers.get('Content-Type') != 'application/json' or length > 65536:
                raise ValueError('Expected a small JSON request')
            body = json.loads(self.rfile.read(length))
            if path == '/api/render':
                return self.respond(200, {'id': self.server.library.enqueue(body['kind'], body['datasets'])})
            self.respond(404, {'error': 'Not found'})
        except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
            self.respond(400, {'error': str(exc)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', default='~/.local/share/bladetag', help='Managed recordings and cached reports')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--import', dest='imports', nargs='+', default=[], help='Session folders, ZIPs, or a parent folder')
    args = parser.parse_args(argv)
    library = Library(args.library)
    for item in args.imports:
        path = Path(item)
        candidates = [path] if path.is_file() or (path / 'session.json').exists() else sorted(p for p in path.iterdir() if (p / 'session.json').exists() or p.suffix.lower() == '.zip')
        for candidate in candidates:
            try:
                library.ingest(candidate)
            except Exception as exc:
                print(f'Could not import {candidate}: {exc}', file=sys.stderr)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.library = library
    print(f'Bladetag library: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        library.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
