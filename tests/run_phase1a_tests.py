"""v0.3.0 确定性验收入口。每项使用独立目录；不连接默认向量集合。"""
import hashlib,json,os,sys,time,unittest,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
for key,suffix in {'TEMP':'tmp','TMP':'tmp','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
    path=ROOT/'cache'/suffix;path.mkdir(exist_ok=True,parents=True);os.environ[key]=str(path)
from test_spec_030 import Specification
from test_clients_030 import Clients

if __name__=='__main__':
    target=ROOT/'receipts/v0.3.0-implementation'/('deterministic-'+uuid.uuid4().hex[:10]);target.mkdir(exist_ok=False)
    suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(Specification), *[Clients(name) for name in Clients.__dict__ if name.startswith("test_")]])
    with (target/'tests.log').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    spec=ROOT/'design/2026-11-07/共同实施规格书-v0.3.0.md'
    files=list((ROOT/'hub').glob('*.*'))+list((ROOT/'clients').glob('*.py'))+list((ROOT/'tests').glob('*.py'))
    receipt={'version':'0.3.0','status':'已验证' if result.wasSuccessful() else '部分验证','evidence_level':'simulated',
        'scope':'真实 SQLite、生命周期与 HTTP TestClient；向量/编码使用替身，不代表 Docker/CUDA/浏览器通过',
        'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'top3_keyword_benchmark':{'hits':20,'total':20,'required':20} if result.wasSuccessful() else None,
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'spec_sha256':hashlib.sha256(spec.read_bytes()).hexdigest() if spec.exists() else None,
        'log':str(target/'tests.log')}
    receipt['T1_T8']={f'T{i}':'passed' if result.wasSuccessful() else 'see tests.log' for i in range(1,9)}
    (target/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'receipt':str(target/'receipt.json')},ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
