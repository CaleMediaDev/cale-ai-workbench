"""Build an inert wheel from an explicit reviewed byte manifest, using stdlib only.

The manifest maps wheel member names to source paths and SHA-256 hashes. The
builder reads bytes; it never imports package code or invokes a build backend.
Tests are relocated to the package namespace by exact, recorded replacements.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

MODULES = {
    'cale_security/cve_intelligence.py',
    'cale_security/security_analysis.py',
    'cale_security/bridge.py',
    'cale_security/CaleStaticExport.java',
    'cale_security/tests/test_cve_intelligence.py',
    'cale_security/tests/test_security_analysis.py',
    'cale_security/tests/test_bridge.py',
}


def build(manifest, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Use an exact three-part version')
    if set(manifest) != MODULES | {'LICENSE'}:
        raise ValueError('The explicit source manifest must contain exactly the reviewed members')
    members = {
        'cale_security/__init__.py': b'"""Local CVE intelligence and static analysis; no import-time work."""\n',
        'cale_security/tests/__init__.py': b'',
    }
    receipt = []
    for name, binding in sorted(manifest.items()):
        if not isinstance(binding, dict) or set(binding) != {'path', 'sha256', 'replacements'}:
            raise ValueError('Every source needs a path, digest and explicit replacement list')
        path = Path(binding['path'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Expected a bounded regular reviewed source file')
        raw = path.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        if before != binding['sha256']:
            raise ValueError('Reviewed source digest changed: '+name)
        for old, new in binding['replacements']:
            old, new = old.encode('utf-8'), new.encode('utf-8')
            if not old or raw.count(old) != 1:
                raise ValueError('A namespace relocation must match exactly once: '+name)
            raw = raw.replace(old, new)
        if not name.startswith('cale_security/tests/') and binding['replacements']:
            raise ValueError('Runtime sources must already have reviewed package imports')
        target = ('cale_security-'+version+'.dist-info/licenses/LICENSE') if name == 'LICENSE' else name
        members[target] = raw
        receipt.append({'member': target, 'source_sha256': before,
                        'packaged_sha256': hashlib.sha256(raw).hexdigest(),
                        'replacements': binding['replacements']})
    info = 'cale_security-'+version+'.dist-info/'
    members[info+'METADATA'] = ('Metadata-Version: 2.3\nName: cale-security\nVersion: '+version+
        '\nSummary: Local CVE intelligence and static artifact analysis for Cale\n'
        'License: MIT\nRequires-Python: >=3.11\n\n').encode()
    members[info+'WHEEL'] = (b'Wheel-Version: 1.0\nGenerator: cale-reviewed-stdlib-builder\n'
                            b'Root-Is-Purelib: true\nTag: py3-none-any\n')
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, raw in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode('ascii')
        writer.writerow([name, 'sha256='+digest, len(raw)])
    writer.writerow([info+'RECORD', '', ''])
    members[info+'RECORD'] = record.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(members.items()):
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError('Invalid wheel member')
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    return output.getvalue(), receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default='0.1.0')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    # A checked-in manifest is portable: its source paths are relative to it,
    # while qualification manifests may use explicit absolute owned paths.
    for binding in manifest.values():
        source = Path(binding['path'])
        if not source.is_absolute():
            binding['path'] = str(args.manifest.resolve().parent/source)
    raw, receipt = build(manifest, args.version)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output/('cale_security-'+args.version+'-py3-none-any.whl')
    with target.open('xb') as stream:
        stream.write(raw)
    result = {'wheel': str(target.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
              'bytes': len(raw), 'sources': receipt, 'package_code_executed': False}
    with (args.output/'build-receipt.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'sources'}))


if __name__ == '__main__':
    main()
