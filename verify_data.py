"""Validate final local exports against SQLite and write a compact audit."""
import csv
import json
from library_store import DATA, connect, now, read_status, stats

db = connect()
assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
assert db.execute('SELECT count(*) FROM resources WHERE NOT json_valid(raw_json)').fetchone()[0] == 0
result = {'checked_at': now(), 'database': stats(db), 'files': {}}
for stem, expected in [('groups', result['database']['groups']), ('all_resources', result['database']['total'])]:
    with (DATA / (stem + '.csv')).open(encoding='utf-8-sig', newline='') as handle:
        rows = csv.DictReader(handle)
        fields = rows.fieldnames
        count = sum(1 for row in rows if row['id'])
        assert count == expected, (stem, count, expected)
    with (DATA / (stem + '.json')).open(encoding='utf-8') as handle:
        records = json.load(handle)
    assert len(records) == expected
    assert len({str(row['id']) for row in records}) == expected
    if stem == 'groups':
        assert all(row.get('peer_type') in ('group', 'megagroup', 'supergroup') for row in records)
    for item in records[::max(1, len(records)//100)]:
        stored = db.execute('SELECT raw_json FROM resources WHERE id=?', (str(item['id']),)).fetchone()
        assert json.loads(stored[0]) == item
    result['files'][stem] = {'records': expected, 'csv_fields': fields, 'json_bytes': (DATA / (stem + '.json')).stat().st_size, 'csv_bytes': (DATA / (stem + '.csv')).stat().st_size}
    del records
result['collection'] = read_status()
(DATA / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
db.close()
print(json.dumps({'verified': True, 'groups': result['database']['groups'], 'all_resources': result['database']['total']}, ensure_ascii=True))
