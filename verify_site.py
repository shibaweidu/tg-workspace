"""Local browser and API checks; never reads site credentials."""
import base64
import json
import pathlib
import time
import requests
import sys

root = pathlib.Path(__file__).resolve().parent
out = root / 'output'
out.mkdir(exist_ok=True)
base = 'http://127.0.0.1:8766'
session = requests.Session()
session.trust_env = False
status = session.get(base + '/api/status', timeout=30).json()
print('LOCAL', json.dumps({key: status[key] for key in ('total','groups','channels')}))
for query in ('', '?q=telegram', '?q=%25_%5C', '?type=groups&visibility=private', '?page=999999', '?q=NoSuchRecord_a9b754e1'):
    r = session.get(base + '/api/groups' + query, timeout=30)
    r.raise_for_status()
    data = r.json()
    assert len(data['items']) <= 20
    assert 1 <= data['page'] <= data['pages']
    print('QUERY', query, data['total'], 'page', data['page'])
if "--browser" not in sys.argv:
    raise SystemExit(0)
from tgegg_cdp import Chrome
with Chrome() as chrome:
    target = chrome.call('Target.createTarget', {'url': base})['targetId']
    (out / 'local-target.txt').write_text(target)
with Chrome(target) as chrome:
    chrome.call('Emulation.setDeviceMetricsOverride', {'width':1440,'height':1050,'deviceScaleFactor':1,'mobile':False}, chrome.session)
    time.sleep(3)
    info = chrome.evaluate("({title:document.title,rows:document.querySelectorAll('#tableBody tr').length,text:document.getElementById('resultCount')?.textContent,duplicates:[...document.querySelectorAll('[id]')].map(x=>x.id).filter((v,i,a)=>a.indexOf(v)!==i)})")
    assert info['rows'] > 0 and not info['duplicates'], info
    print('BROWSER', json.dumps(info,ensure_ascii=True))
    shot=chrome.call('Page.captureScreenshot', {'format':'png'}, chrome.session)
    (out / 'local-library.png').write_bytes(base64.b64decode(shot['data']))
