import json
import zipfile

from bladetag.session import load_session


def test_optional_hrm_folder_and_zip(tmp_path):
    folder = tmp_path / 'session'
    folder.mkdir()
    meta = {'format_version': 1, 'tags': [], 'start_elapsed_ns': 1,
            'start_utc': '2026-11-14T00:00:00Z'}
    (folder / 'session.json').write_text(json.dumps(meta))
    assert load_session(folder).heart_rate == []
    meta['heart_rate'] = {'file': 'heartrate.csv', 'name': 'Giant HRM'}
    (folder / 'session.json').write_text(json.dumps(meta))
    (folder / 'heartrate.csv').write_text(
        'elapsed_ns,bpm,contact_supported,contact_detected,energy_kj,rr_ticks,raw_hex\n'
        '9007199254740993,71,true,true,,"[854,852]",164756035403\n'
        '9007199254740994,,,,,,104001\n'
        '9007199254740995,0,true,false,,"[]",0400\n')
    rows = load_session(folder).heart_rate
    assert rows[0]['elapsed_ns'] == 9007199254740993
    assert rows[0]['rr_ticks'] == [854, 852]
    assert rows[0]['energy_kj'] is None
    assert rows[1]['bpm'] is None
    assert rows[1]['raw_hex'] == '104001'
    assert rows[2]['bpm'] == 0
    assert rows[2]['contact_detected'] is False
    archive = tmp_path / 'session.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for file in folder.iterdir():
            z.write(file, 'session/' + file.name)
    assert load_session(archive).heart_rate == rows
