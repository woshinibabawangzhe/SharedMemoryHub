"""Check that the shipped help, service, workbench and explicit schema agree."""
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    manifest = json.loads((ROOT / 'deployment-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('candidate_manifest'):
        manifest = json.loads((ROOT / manifest['candidate_manifest']).read_text(encoding='utf-8'))
    help_path = ROOT / manifest['help_source']
    help_text = help_path.read_text(encoding='utf-8')
    expected = (manifest['service_version'], manifest['workbench_version'], str(manifest['schema_version']))
    actual = re.search(r'service=([\d.]+)；workbench=([\d.]+)；schema=(\d+)', help_text)
    if not actual or actual.groups() != expected:
        raise ValueError('help_version_contract_mismatch')
    sources = {
        'service': (ROOT / 'hub/app.py').read_text(encoding='utf-8'),
        'workbench': (ROOT / 'hub/workbench-v0.7.0/workbench.html').read_text(encoding='utf-8'),
        'schema': ast.parse((ROOT / 'hub/db.py').read_text(encoding='utf-8')),
    }
    if f'version="{expected[0]}"' not in sources['service']:
        raise ValueError('service_version_mismatch')
    if expected[1] not in sources['workbench']:
        raise ValueError('workbench_version_mismatch')
    schema = next(ast.literal_eval(node.value) for node in sources['schema'].body
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_VERSION' for t in node.targets))
    if schema != manifest['schema_version']:
        raise ValueError('schema_version_mismatch')
    return {'status': 'verified', 'service': expected[0], 'workbench': expected[1],
            'schema': schema, 'help_source': manifest['help_source'],
            'help_sha256': hashlib.sha256(help_path.read_bytes()).hexdigest(),
            'boundary': 'Version identity only; feature semantics require tests and review.'}


if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False, indent=2))
