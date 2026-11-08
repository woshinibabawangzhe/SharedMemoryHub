"""有界分段下载官方 wheel；仅完整 SHA256 匹配后成为可安装文件。"""
import concurrent.futures,hashlib,json,os,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
URL='https://download-r2.pytorch.org/whl/cu128/torch-2.11.0%2Bcu128-cp313-cp313-win_amd64.whl'
SHA='6f367e62fd81b75cdf23ca4b75ced834d2db2cf98d1588ac935bde345de9de23'
SIZE=2753196039


def download():
    import sys
    sys.path.insert(0,str(ROOT))
    from hub.paths import checked_path
    chunks=checked_path(ROOT/'cache/cuda-wheel-segments');chunks.mkdir(exist_ok=True)
    wheelhouse=checked_path(ROOT/'cache/wheels-0.3.0');wheelhouse.mkdir(exist_ok=True)
    output=wheelhouse/'torch-2.11.0+cu128-cp313-cp313-win_amd64.whl'
    if output.exists():
        if hashlib.file_digest(output.open('rb'),'sha256').hexdigest()==SHA:return
        raise RuntimeError('existing_wheel_hash_mismatch')
    block=8*1024*1024
    def fetch(i):
        start=i*block;end=min(SIZE,start+block)-1
        file=chunks/f'{i:04d}.segment'
        if file.exists() and file.stat().st_size==end-start+1:return file
        for attempt in range(3):
            try:
                req=urllib.request.Request(URL,headers={'Range':f'bytes={start}-{end}'})
                with urllib.request.urlopen(req,timeout=25) as response:
                    if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {start}-{end}/{SIZE}':raise RuntimeError('range_response_mismatch')
                    data=response.read(end-start+2)
                if len(data)!=end-start+1:raise RuntimeError('range_size_mismatch')
                tmp=file.with_suffix('.pending');tmp.write_bytes(data);os.replace(tmp,file);return file
            except Exception:
                if attempt==2:raise
    count=(SIZE+block-1)//block
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        futures=[executor.submit(fetch,i) for i in range(count)]
        for done,future in enumerate(concurrent.futures.as_completed(futures),1):
            future.result()
            if done%16==0 or done==count:print(json.dumps({'segments':done,'total':count}),flush=True)
    temporary=output.with_suffix('.assembling')
    with temporary.open('wb') as dst:
        for i in range(count):
            with (chunks/f'{i:04d}.segment').open('rb') as src:
                while data:=src.read(1024*1024):dst.write(data)
    with temporary.open('rb') as src:actual=hashlib.file_digest(src,'sha256').hexdigest()
    if actual!=SHA:raise RuntimeError('official_wheel_sha256_mismatch')
    os.replace(temporary,output)
    print(json.dumps({'status':'verified','sha256':actual,'bytes':SIZE}),flush=True)

if __name__=='__main__':download()
