import json
import re

import pytest

from bladetag.session import Session
from bladetag import walking_report as report


def session(path, activity='walking'):
    return Session(path, dict(start_elapsed_ns=0, end_elapsed_ns=20_000_000_000,
                              start_utc='2026-11-16T00:00:00Z', activity=activity), [], None)


def test_report_tagless_and_safe_embedding(tmp_path, monkeypatch):
    source = tmp_path/'walk'
    monkeypatch.setattr(report, 'load_session', lambda path: session(path))
    out = tmp_path/'walking.html'
    title = '</script><script>alert(1)</script>'
    result = report.write_report([source, source], out, title)
    assert len(result['sessions']) == 1
    assert result['sessions'][0]['gait']['tags'] == []
    text = out.read_text()
    assert title not in text
    assert '__WALKING_DATA__' not in text
    assert 'https://' not in text
    embedded = re.search(r'type="application/json">(.*?)</script>', text, re.S).group(1)
    assert json.loads(embedded)['title'] == title
    json.dumps(result, allow_nan=False)


def test_activity_filter_and_explicit_wrong_activity(tmp_path, monkeypatch):
    for name in ['walk', 'ride']:
        folder = tmp_path/name
        folder.mkdir()
        (folder/'session.json').write_text('{}')
    monkeypatch.setattr(report, 'load_session', lambda path: session(path, 'walking' if path.name == 'walk' else 'cycling'))
    result = report.build([tmp_path])
    assert len(result['sessions']) == 1
    assert result['skipped'] == [dict(id='ride', reason='activity: cycling')]
    with pytest.raises(ValueError, match='expected a walking'):
        report.build([tmp_path/'ride'])
