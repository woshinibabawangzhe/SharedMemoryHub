"""单进程宿主。停止请求通过本实例文件触发 Uvicorn 正常退出。"""
import argparse,json,os,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path, checked_asset


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);p.add_argument('--port',type=int,required=True)
    p.add_argument('--qdrant-port',type=int,default=6333);p.add_argument('--prefix',default='memories');p.add_argument('--device',choices=['cuda','cpu'],default='cuda')
    p.add_argument('--model-dir')
    a=p.parse_args();runtime=checked_path(a.runtime)
    for key,suffix in {'TEMP':'tmp','TMP':'tmp','PIP_CACHE_DIR':'pip','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
        path=checked_path(ROOT/'cache'/suffix);path.mkdir(parents=True,exist_ok=True);os.environ[key]=str(path)
    os.environ.update(SMH_RUNTIME_ROOT=str(runtime),SMH_ORIGIN=f'http://127.0.0.1:{a.port}',SMH_QDRANT_URL=f'http://127.0.0.1:{a.qdrant_port}',
        SMH_COLLECTION_PREFIX=a.prefix,SMH_DEVICE=a.device,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1')
    if a.model_dir:os.environ['SMH_MODEL_DIR']=str(checked_asset(a.model_dir))
    import uvicorn
    from hub.app import create_hub_app
    server=uvicorn.Server(uvicorn.Config(create_hub_app(),host='127.0.0.1',port=a.port,workers=1,access_log=False))
    def watch():
        while not server.should_exit:
            if (runtime/'stop.request').is_file():server.should_exit=True;return
            time.sleep(.25)
    threading.Thread(target=watch,daemon=True).start()
    server.run()

if __name__=='__main__':main()
