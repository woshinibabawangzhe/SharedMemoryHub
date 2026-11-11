"""Static public-source checks; never inspect ignored runtime or authentication data."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ABSOLUTE = re.compile(r'(?i)\b[a-z]:(?:\\|/(?!/))')
CURRENT_DOCS = [
    'README.md', 'CONTRIBUTING.md', 'docs/项目文档导航.md',
    'docs/部署与运行目录.md', 'docs/AI客户端接入指南.md',
    'docs/客户端调用规范.md', 'docs/使用与系统说明.md',
    'docs/VERIFICATION.md', 'docs/ROADMAP.md',
]
SOURCE_DIRS = {'hub', 'clients', 'scripts', 'tests', 'docs', 'design', '.github'}
TEXT_SUFFIXES = {'.py', '.md', '.js', '.cjs', '.html', '.css', '.json', '.yml', '.yaml', '.ps1', '.cmd'}


def check():
    result = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
        check=True, capture_output=True,
    )
    names = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    findings = []
    checked = 0
    for name in names:
        relative = Path(name)
        if relative.parts[0] not in SOURCE_DIRS and len(relative.parts) != 1:
            continue
        if relative.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if relative.name.startswith('.env'):
            findings.append({'file': name, 'reason': 'environment_file_in_source'})
            continue
        path = ROOT / relative
        if not path.is_file():  # An explicitly removed tracked file.
            continue
        text = path.read_text(encoding='utf-8-sig')
        checked += 1
        if WINDOWS_ABSOLUTE.search(text):
            findings.append({'file': name, 'reason': 'machine_absolute_path'})
        if 'user:' + 'zhuan' + 'z' in text.lower():
            findings.append({'file': name, 'reason': 'personal_subject_literal'})
    for name in CURRENT_DOCS:
        path = ROOT / name
        text = path.read_text(encoding='utf-8')
        if sum(line.startswith('```') for line in text.splitlines()) % 2:
            findings.append({'file': name, 'reason': 'unclosed_code_fence'})
        for target in re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', text):
            if '://' in target or target.startswith(('#', 'mailto:')):
                continue
            target = target.split('#', 1)[0]
            if target and not (path.parent / target).is_file():
                findings.append({'file': name, 'reason': 'broken_link', 'target': target})
    return {'status': 'verified' if not findings else 'failed',
            'checked_source_files': checked, 'findings': findings,
            'boundary': 'Static source and current-document checks only; no deployment or secret scan.'}


if __name__ == '__main__':
    report = check()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] == 'verified' else 1)
