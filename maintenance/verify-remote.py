import hashlib,json,os
from pathlib import Path
root=Path(os.environ['GITHUB_WORKSPACE'])
rows=json.loads((root/'maintenance/files.json').read_text())
for row in rows:
    oid=row['sha256'];content=Path('.git/lfs/objects')/oid[:2]/oid[2:4]/oid
    h=hashlib.sha256()
    with content.open('rb') as source:
        while block:=source.read(4*1024*1024):h.update(block)
    assert content.stat().st_size==row['bytes'] and h.hexdigest()==oid,row['path']
    print('Fresh LFS download verified:',row['path'],flush=True)
report={'published_commit':'3f230e4a44574ce85c4025c822802cff1f68d0b0','files':[{k:r[k] for k in ('path','sha256','bytes')} for r in rows]}
Path('lfs-readback.json').write_text(json.dumps(report,indent=2)+'\n')
