import hashlib,json,lzma
from pathlib import Path
rows=json.loads(Path('maintenance/files.json').read_text())
for row in rows:
    oid=row['sha256'];target=Path('.git/lfs/objects')/oid[:2]/oid[2:4]/oid
    target.parent.mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256();size=0
    with lzma.open(row['transport'],'rb') as source,target.open('wb') as output:
        while block:=source.read(4*1024*1024):
            output.write(block);h.update(block);size+=len(block)
    assert size==row['bytes'] and h.hexdigest()==oid,row['path']
    print('Exact original bytes verified:',row['path'],size,oid,flush=True)
