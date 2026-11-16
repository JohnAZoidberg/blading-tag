import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import pytest
from bladetag.web import Library, Handler, ThreadingHTTPServer


def recording(tmp_path):
    path = tmp_path / 'walk.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('session.json', json.dumps(dict(format_version=1, activity='cycling',
            start_utc='2026-11-16T00:00:00Z', start_elapsed_ns=0, tags=[])))
    return path


def test_import_render_persist_and_cache(tmp_path):
    library = Library(tmp_path / 'library')
    source = recording(tmp_path)
    identity = library.ingest(source)
    assert library.ingest(source) == identity
    library.pool.shutdown(wait=True)
    job = next(iter(library.state['jobs'].values()))
    assert job['status'] == 'ready', job['error']
    assert library.enqueue('cycling-report', [identity]) == job['id']
    page = library.root / 'reports' / job['id'] / 'index.html'
    assert '<html' in page.read_text().lower()
    reopened = Library(library.root)
    assert reopened.snapshot() == library.snapshot()
    reopened.pool.shutdown()


def test_validation_and_restart(tmp_path):
    library = Library(tmp_path / 'library')
    with pytest.raises(ValueError):
        library.enqueue('oops', ['missing'])
    with pytest.raises(ValueError):
        library.enqueue('cycling-report', ['missing'])
    library.state['jobs']['interrupted'] = dict(status='running')
    library.save()
    reopened = Library(library.root)
    assert reopened.state['jobs']['interrupted']['status'] == 'failed'
    library.pool.shutdown()
    reopened.pool.shutdown()


def test_http_upload_and_origin_guard(tmp_path):
    library = Library(tmp_path / 'library')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.library = library
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        assert b'Recording library' in urlopen(base).read()
        data = recording(tmp_path).read_bytes()
        request = Request(base + '/api/upload', data=data,
                          headers={'Content-Type': 'application/zip'})
        identity = json.load(urlopen(request))['id']
        assert identity in json.load(urlopen(base + '/api/library'))['datasets']
        library.pool.shutdown(wait=True)
        job = next(iter(library.state['jobs'].values()))
        response = urlopen(base + '/export/' + job['id'])
        assert 'attachment' in response.headers['Content-Disposition']
        assert response.read() == urlopen(base + '/view/' + job['id']).read()
        with pytest.raises(ValueError, match='walking'):
            library.enqueue('walking-report', [identity])
        rebound = Request(base + '/api/library', headers={'Host': 'example.com'})
        with pytest.raises(HTTPError) as exc:
            urlopen(rebound)
        assert exc.value.code == 403
        hostile = Request(base + '/api/render', data=b'{}', headers={
            'Content-Type': 'application/json', 'Origin': 'https://example.com'})
        with pytest.raises(HTTPError) as exc:
            urlopen(hostile)
        assert exc.value.code == 403
        with pytest.raises(HTTPError):
            urlopen(base + '/view/../../library.json')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        library.pool.shutdown(wait=True)
