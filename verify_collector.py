"""Exercise pagination, ID deduplication, exports, stop and resume in temporary storage."""
import json
import pathlib
import tempfile
from unittest.mock import patch
import collect_tgegg as collector
import library_store as store


class Source:
    def __init__(self, workers, peer_type=""):
        pass

    def page(self, page, direction='asc', sort='seen_count'):
        ids = {1: ['1', '2'], 2: ['2', '3'], 3: ['4']}.get(page, [])
        return {'items': [{'id': key, 'title': 'group '+key, 'peer_type': 'group', 'extra': {'preserved': True}} for key in ids], 'total': 205, 'page': page}

    def close(self):
        pass


class ExpiredSource(Source):
    def page(self, page, direction='asc', sort='seen_count'):
        if page > 1:
            raise collector.SessionUnavailable('Test expired session')
        return super().page(page, direction, sort)


with tempfile.TemporaryDirectory(prefix='tgegg-check-') as directory:
    root = pathlib.Path(directory)
    with patch.object(store, 'DATA', root), patch.object(store, 'DB', root / 'test.sqlite3'), patch.object(store, 'STATUS', root / 'status.json'), patch.object(collector, 'DATA', root), patch.object(collector, 'Source', Source):
        a = collector.collect(workers=1, max_pages=1)
        assert a['stage'] == 'partial' and a['total'] == 2 and a['pages_done'] == 1
        b = collector.collect(workers=2, max_pages=3)
        assert b['run_id'] == a['run_id'] and b['total'] == 4 and b['pages_done'] == 3
        records = json.loads((root / 'groups.json').read_text(encoding='utf-8'))
        assert len(records) == 4 and all(row['extra']['preserved'] for row in records)
        (root / 'stop.request').touch()
        c = collector.collect(workers=1, fresh=True, max_pages=3)
        assert c['stage'] == 'stopped' and c['pages_done'] == 1
        (root / 'stop.request').unlink()
        with patch.object(collector, 'Source', ExpiredSource):
            try:
                collector.collect(workers=1, fresh=True, max_pages=3)
                raise AssertionError('Expected a session error')
            except collector.SessionUnavailable:
                saved = store.read_status()
                assert saved['stage'] == 'error' and saved['pages_done'] == 1
                assert (root / 'groups.json').exists()
print('PASS: pagination, resume, ID deduplication, extra fields, exports and pause')
