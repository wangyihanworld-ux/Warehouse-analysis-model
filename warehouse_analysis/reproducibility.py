"""Local reproducibility manifest; hashes detect changes, not tamper-proof audit."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def write_manifest(output, inputs, configuration):
    root = Path(__file__).resolve().parents[1]
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    source = {str(path.relative_to(root)): digest(path)
              for path in sorted((root/'warehouse_analysis').rglob('*.py'))}
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True)
    manifest = {'python': sys.version, 'git_head': result.stdout.strip() if result.returncode == 0 else None,
                'source_sha256': source,
                'inputs': [{'name': Path(path).name, 'sha256': digest(path)} for path in inputs],
                'configuration': configuration,
                'limitation': 'Keep original inputs alongside this manifest. Hashes are not an immutable audit log.'}
    Path(output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
