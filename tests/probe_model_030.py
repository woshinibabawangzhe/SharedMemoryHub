"""本机真实模型探针；设备必须显式选择，不隐式回退。"""
import argparse,hashlib,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
for key,suffix in {'TEMP':'tmp','TMP':'tmp','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
    path=ROOT/'cache'/suffix;path.mkdir(parents=True,exist_ok=True);os.environ[key]=str(path)
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
import torch,numpy as np
from hub.embedder import EmbeddingEngine,PINNED_MODEL_ID,PINNED_MODEL_REVISION,MODEL_DIR

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--device',choices=['cuda','cpu'],required=True);a=p.parse_args()
    out={'version':'0.3.0','evidence_level':'controlled-trial','device_requested':a.device,'python':sys.version,'torch':torch.__version__,
        'cuda_runtime':torch.version.cuda,'cuda_available':torch.cuda.is_available(),'model':PINNED_MODEL_ID,'revision':PINNED_MODEL_REVISION}
    engine=EmbeddingEngine(device=a.device);started=time.monotonic()
    try:
        assert engine.ensure_loaded(),engine.load_error
        vector=engine.encode_one('合成记忆：用户偏好简体中文，并希望引用可核验的资料。',timeout_sec=60)
        out.update(status='已验证',seconds=round(time.monotonic()-started,3),device_actual=str(next(engine._model.parameters()).device),dimension=len(vector),
            norm=float(np.linalg.norm(vector)),finite=bool(np.isfinite(vector).all()))
        assert len(vector)==1024 and out['finite'] and abs(out['norm']-1)<.005
        assert out['device_actual'].startswith(a.device)
        if a.device=='cuda':out.update(gpu=torch.cuda.get_device_name(0),peak_allocated_bytes=torch.cuda.max_memory_allocated())
        out['model_files_sha256']={f.name:hashlib.file_digest(f.open('rb'),'sha256').hexdigest() for f in MODEL_DIR.iterdir() if f.is_file() and f.name in ('model.safetensors','config.json','tokenizer.json','tokenizer_config.json')}
    except Exception as error:
        out.update(status='未验证',error=type(error).__name__+': '+str(error));raise
    finally:
        target=ROOT/f'receipts/v0.3.0-implementation/model-{a.device}.json'
        target.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in out.items() if k!='model_files_sha256'},ensure_ascii=False))
