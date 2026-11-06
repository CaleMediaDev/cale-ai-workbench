"""Local evidence collection; importing this module never starts an analyzer.

Artifact bytes, source text and CVE descriptions are untrusted data. They are
never imported, evaluated, installed, or used as Ghidra scripts/command options.
The toolbox wrapper must bind project_folder to the current registered task.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

VERSION = 2
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 512
MAX_TEXT = 2 * 1024 * 1024
MAX_REPORT = 2 * 1024 * 1024
PACKAGE = Path(__file__).resolve().parent
GHIDRA = 'ghidra_12.1.4_PUBLIC'
JDK = 'jdk-21.0.13+9'
EXPORTER = 'CaleStaticExport.java'
EXPORTER_SHA256 = '38a840f084b2fdd633f294485f5d98c4efaf49415382bf6f0dc26ffea63e7bf1'


@dataclass(frozen=True)
class AnalysisHost:
    """Constructed by the current native task host, NEVER from tool arguments.

    capture_scope must freshly confirm registered task ownership and return
    complete full instruction hashes. Unknown/unreadable scope raises; no file
    reads or task authority are invented by this package to fill missing data.
    """
    app_dir: Path
    project_folder: Path
    capture_scope: Callable[[], dict]
    cancel: Callable[[], bool]


def _host_snapshot(host, root):
    if (not isinstance(host, AnalysisHost) or not callable(host.capture_scope) or not callable(host.cancel)
            or Path(host.project_folder).resolve(strict=True) != root):
        raise ValueError('The current task host must supply its own project context')
    if host.cancel():
        raise InterruptedError('Current task is stopped, paused or superseded')
    value = host.capture_scope()
    if (not isinstance(value, dict) or set(value) != {'task_id', 'owner_generation', 'project_folder', 'instruction_basis'}
            or not isinstance(value.get('task_id'), str) or not 1 <= len(value['task_id']) <= 128
            or type(value.get('owner_generation')) is not int or value['owner_generation'] < 0
            or value.get('project_folder') != str(root)):
        raise ValueError('Current owner identity is unknown')
    basis = value.get('instruction_basis')
    if (not isinstance(basis, dict) or set(basis) != {'brief', 'rules', 'followups'}
            or not isinstance(basis.get('brief'), str) or not re.fullmatch('[0-9a-f]{64}', basis['brief'])
            or any(basis[name] is not None and (not isinstance(basis[name], str) or not re.fullmatch('[0-9a-f]{64}', basis[name])) for name in ('rules','followups'))):
        raise ValueError('Full original brief, rules and followup scope is unknown')
    if host.cancel():
        raise InterruptedError('Current task changed while acquiring scope')
    # Detach callback-owned mutable dictionaries; saved scope is plain data.
    return json.loads(json.dumps(value, allow_nan=False))


def _project(folder):
    # Lazy import: no MCP, account or subprocess initialization on module import.
    from toolbox_mcp import registered_project
    return registered_project(folder)


def _identity(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Expected a plain single-link artifact')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _inside(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('Use an exact relative artifact path')
    item = Path(relative)
    if item.anchor or item.is_absolute() or '..' in item.parts or ':' in relative:
        raise ValueError('Use an exact relative artifact path')
    path = root / item
    for parent in [*reversed(path.parents), path]:
        if parent == root or parent.is_relative_to(root):
            value = parent.lstat()
            if parent.is_symlink() or getattr(value, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Artifact aliases are not supported')
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('Artifact leaves the task')
    _identity(path)
    return path


def _read(path, limit, deadline):
    before = _identity(path)
    if before[2] > limit:
        raise ValueError('Artifact exceeds the selected analysis byte limit')
    digest = hashlib.sha256()
    chunks = []
    total = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
            raise ValueError('Artifact changed before reading')
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Static evidence budget elapsed')
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError('Artifact grew past its byte limit')
            digest.update(chunk)
            chunks.append(chunk)
        ended = os.fstat(stream.fileno())
        if (ended.st_dev, ended.st_ino, ended.st_size, ended.st_mtime_ns) != before:
            raise ValueError('Artifact changed while reading')
    if _identity(path) != before:
        raise ValueError('Artifact path changed while reading')
    return b''.join(chunks), digest.hexdigest(), before


def file_kind(raw):
    """Header classification, not a loader-validity or safety verdict."""
    if raw[:2] == b'MZ' and len(raw) >= 64:
        offset = int.from_bytes(raw[60:64], 'little')
        return 'PE' if offset <= len(raw)-4 and raw[offset:offset+4] == b'PE\0\0' else 'DOS_or_invalid_PE'
    if raw[:4] == b'\x7fELF':
        return 'ELF'
    if raw[:4] in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        return 'MachO_or_fat_candidate'
    if b'\0' in raw[:8192]:
        return 'binary_unknown'
    try:
        raw.decode('utf-8-sig')
        return 'UTF8_text'
    except UnicodeError:
        return 'unknown'


def _origin(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Website scope must contain exact HTTP(S) origins')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = '[' + host + ']'
    port = parsed.port
    return parsed.scheme + '://' + host + (':' + str(port) if port is not None else '')


def create_security_engagement(project_folder, user_request, artifact_paths, site_origins=None, *, host):
    """Save exact supplied scope; this performs no website or artifact analysis."""
    root = _project(project_folder)
    host_basis = _host_snapshot(host, root)
    if not isinstance(user_request, str) or not 1 <= len(user_request.encode('utf-8')) <= 16000:
        raise ValueError('A bounded actual user request is required')
    if not isinstance(artifact_paths, list) or not 1 <= len(artifact_paths) <= MAX_FILES:
        raise ValueError('Choose 1..512 exact supplied artifact files')
    if any(not isinstance(path, str) for path in artifact_paths):
        raise ValueError('Artifact paths must be strings')
    paths = sorted(set(artifact_paths))
    for relative in paths:
        _inside(root, relative)
    origins = site_origins or []
    if not isinstance(origins, list) or len(origins) > 32:
        raise ValueError('Choose at most 32 exact site origins')
    import task_context
    record = {'version': VERSION, 'id': uuid.uuid4().hex, 'request_sha256': task_context.request_identity(root), 'host_basis': host_basis,
        'user_request': user_request, 'artifact_paths': paths, 'site_origins': sorted(set(map(_origin, origins))),
        'permissions': ['local_static_analysis'], 'live_checks_authorized': False}
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > 100000:
        raise ValueError('Engagement scope exceeds its 100000-byte read limit; select fewer artifact paths')
    directory = root / '.local-ai/security/engagements'
    _ensure_directory(root, directory)
    target = directory / (record['id'] + '.json')
    if _host_snapshot(host, root) != host_basis:
        raise ValueError('Current owner/instructions changed before scope publication')
    with target.open('xb') as stream:
        stream.write(encoded)
        stream.flush(); os.fsync(stream.fileno())
    if _host_snapshot(host, root) != host_basis:
        raise ValueError('Created scope is stale; no analysis was started')
    return {'engagement_id': record['id'], 'scope': record, 'live_checks_started': False}


def _safe_directory(root, directory):
    if directory != root and not directory.is_relative_to(root):
        raise ValueError('Analysis directory leaves its task')
    for path in [root, *[p for p in reversed(directory.parents) if p != root and p.is_relative_to(root)], directory]:
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or path.is_symlink() or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Analysis directory alias is unsupported')


def _ensure_directory(root, directory):
    """Reject existing aliases BEFORE making any nested analysis directory."""
    _safe_directory(root, root)
    cursor = root
    for part in directory.relative_to(root).parts:
        cursor = cursor / part
        try:
            cursor.mkdir()
        except FileExistsError:
            pass
        _safe_directory(root, cursor)


def _engagement(root, identifier, host):
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('Unknown engagement')
    path = _inside(root, '.local-ai/security/engagements/' + identifier + '.json')
    raw, digest, _ = _read(path, 100000, time.monotonic()+2)
    value = json.loads(raw)
    import task_context
    if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('id') != identifier
            or value.get('request_sha256') != task_context.request_identity(root)
            or value.get('host_basis') != _host_snapshot(host, root)
            or value.get('permissions') != ['local_static_analysis'] or value.get('live_checks_authorized') is not False
            or not isinstance(value.get('artifact_paths'), list) or not 1 <= len(value['artifact_paths']) <= MAX_FILES):
        raise ValueError('Engagement is stale or malformed; inspect the current request')
    return value, digest


def _source_findings(relative, raw):
    if len(raw) > MAX_TEXT:
        return [], ['source_text_limit']
    text = raw.decode('utf-8-sig', errors='strict')
    findings = []
    if relative.endswith('.py'):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return [], ['python_AST_unavailable']
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func) if isinstance(node.func, (ast.Name, ast.Attribute)) else ''
                rule = ('dynamic_code' if name in {'eval', 'exec', 'builtins.eval', 'builtins.exec'} else
                    'shell_command' if name.startswith('subprocess.') and any(k.arg == 'shell' and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords) else None)
                if rule:
                    findings.append({'rule': rule, 'line': node.lineno, 'status': 'review_candidate'})
                if len(findings) >= 100:
                    return findings, ['finding_cap']
    elif relative.endswith(('.js', '.ts', '.html', '.php')):
        patterns = {'dynamic_code': r'\b(?:eval|Function)\s*\(', 'html_sink': r'\b(?:innerHTML|outerHTML)\s*=',
            'PHP_command': r'\b(?:shell_exec|passthru|system)\s*\(',
            'PHP_deserialization': r'\bunserialize\s*\('}
        for number, line in enumerate(text.splitlines(), 1):
            for rule, pattern in patterns.items():
                if re.search(pattern, line):
                    findings.append({'rule': rule, 'line': number, 'status': 'review_candidate'})
            if len(findings) >= 100:
                return findings[:100], ['finding_cap']
    return findings, []


def _dependencies(relative, raw):
    """Recognized exact declarations only; never run package managers/hooks."""
    if len(raw) > MAX_TEXT:
        return [], ['dependency_manifest_limit']
    components = []
    if Path(relative).name == 'package-lock.json':
        value = json.loads(raw)
        packages = value.get('packages') if isinstance(value, dict) else None
        if not isinstance(packages, dict) or len(packages) > 4096:
            return [], ['unsupported_or_large_npm_lock']
        for key, item in packages.items():
            if key and isinstance(item, dict) and isinstance(item.get('version'), str):
                name = item.get('name') or key.rsplit('node_modules/', 1)[-1]
                if isinstance(name, str) and 0 < len(name) <= 200 and re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,99}', item['version']):
                    components.append({'ecosystem': 'npm', 'name': name, 'version': item['version'], 'evidence': relative, 'kind': 'locked_declaration'})
    elif Path(relative).name.startswith('requirements') and relative.endswith('.txt'):
        # Extras select optional dependencies of the same pinned distribution;
        # they are not separate packages with independently known versions.
        extra = r'[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,198}[A-Za-z0-9])?'
        extras = rf'(?:\[\s*(?:{extra}(?:\s*,\s*{extra})*)?\s*\])?'
        for number, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
            match = re.fullmatch(rf'\s*([A-Za-z0-9][A-Za-z0-9_.-]{{0,199}})\s*{extras}\s*==\s*([A-Za-z0-9][A-Za-z0-9.+_-]{{0,99}})\s*(?:#.*)?', line)
            if match:
                components.append({'ecosystem': 'PyPI', 'name': match[1], 'version': match[2], 'evidence': relative, 'line': number, 'kind': 'pinned_declaration'})
    elif relative.endswith('.php'):
        # Standard WordPress plugin headers are observations, not proof of an
        # installed slug or version. Never infer the plugin's CVE identity here.
        header = raw[:8192].decode('utf-8-sig', errors='replace')
        plugin = re.search(r'(?mi)^\s*(?:/\*+|\*)?\s*Plugin Name:\s*([^\r\n]{1,200})', header)
        version = re.search(r'(?mi)^\s*(?:/\*+|\*)?\s*Version:\s*([0-9][A-Za-z0-9.+_-]{0,99})\s*$', header)
        if plugin and version:
            components.append({'ecosystem': 'WordPress', 'name': plugin[1].strip(), 'version': version[1],
                'evidence': relative, 'kind': 'plugin_header_display_name_not_slug'})
    return components[:4096], ['component_cap'] if len(components) > 4096 else []


def inspect_security_artifacts(project_folder, engagement_id, *, host):
    """Read-only local source/binary inventory, source evidence and dependency list."""
    root = _project(project_folder)
    scope, scope_hash = _engagement(root, engagement_id, host)
    deadline = time.monotonic()+10
    files, components, identities, total = [], [], [], 0
    for relative in scope['artifact_paths']:
        if host.cancel():
            raise InterruptedError('Static inspection cancelled')
        path = _inside(root, relative)
        raw, digest, identity = _read(path, min(MAX_FILE, MAX_TOTAL-total), deadline)
        total += len(raw)
        kind = file_kind(raw)
        findings, limited = [], []
        if kind == 'UTF8_text':
            findings, limited = _source_findings(relative, raw)
            try:
                dependency, reasons = _dependencies(relative, raw)
                components.extend(dependency); limited.extend(reasons)
            except (ValueError, UnicodeError, RecursionError):
                limited.append('dependency_parse_unavailable')
        files.append({'path': relative, 'sha256': digest, 'bytes': len(raw), 'kind': kind, 'findings': findings, 'limitations': limited})
        identities.append((path, identity))
        if len(components) > 4096:
            raise ValueError('Dependency evidence exceeds component limit; select fewer manifests')
    for path, identity in identities:
        if time.monotonic() >= deadline or _identity(path) != identity:
            raise ValueError('Evidence changed or time budget elapsed; no current report')
    if _engagement(root, engagement_id, host)[1] != scope_hash:
        raise ValueError('Engagement changed during inspection')
    report = {'status': 'static_evidence', 'engagement_id': engagement_id, 'scope_sha256': scope_hash,
        'files': files, 'components': components, 'coverage': 'explicit_files_only',
        'limitations': ['Patterns are review leads, not proven vulnerabilities.', 'Dependencies are declarations, not proof of deployed or reachable components.', 'No analyzed code, package hook, website or CVE reference code was executed.']}
    if len(json.dumps(report, ensure_ascii=False).encode('utf-8')) > MAX_REPORT:
        raise ValueError('Report exceeds its byte cap; select fewer artifacts')
    return report


def security_backend_status(*, app_dir):
    """Filesystem status only. Readiness requires a separate qualified actual probe."""
    app = Path(app_dir).resolve(strict=True)
    ghidra = app / 'Toolbox' / GHIDRA
    java = app / 'Toolbox' / JDK
    return {'ghidra_version': '12.1.4', 'ghidra_present': (ghidra/'support/analyzeHeadless.bat').is_file(),
        'jdk_present': (java/'bin/java.exe').is_file(), 'exporter_present': (PACKAGE/EXPORTER).is_file(),
        'runtime_qualified': False, 'installation': 'Use existing verified pinned installer; no download starts here.'}


def prepare_security_backend_install(*, app_dir):
    """On-demand installer hook PLAN; caller coordinates the existing installer."""
    raw, _, _ = _read(Path(app_dir).resolve(strict=True)/'downloads.json', 64000, time.monotonic()+2)
    pins = json.loads(raw)
    return {'status': 'prepared_not_installed', 'components': {key: pins[key] for key in ('ghidra','java')},
        'compatible_pair': 'Ghidra 12.1.4 / JDK 21', 'installer': 'Existing Install-Backend.ps1 verified-download path',
        'starts_download': False, 'starts_analysis': False,
        'note': 'Do not upgrade to Ghidra 12.2 without its separately pinned JDK25 pair and qualification.'}


def _owned_windows_run(command, cwd, environment, log, timeout, cancel):
    """One fixed analyzer family; not a security sandbox or external-service fence."""
    if os.name != 'nt':
        raise ValueError('Headless process containment is currently Windows-only')
    import ctypes
    import win32job
    job = win32job.CreateJobObject(None, 'Local\\CaleSecurity-'+uuid.uuid4().hex)
    child = None
    assigned = False
    deadline = time.monotonic()+timeout
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        with log.open('xb') as output:
            if cancel():
                raise InterruptedError('Analysis cancelled before launch')
            child = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, creationflags=0x08000000 | 0x00000004)
            win32job.AssignProcessToJobObject(job, int(child._handle)); assigned = True
            if cancel() or time.monotonic() >= deadline:
                raise InterruptedError('Analysis cancelled before resume')
            resume = ctypes.WinDLL('ntdll').NtResumeProcess
            resume.argtypes = [ctypes.c_void_p]; resume.restype = ctypes.c_long
            if resume(int(child._handle)) < 0:
                raise RuntimeError('Analyzer could not resume')
            while True:
                if cancel() or time.monotonic() >= deadline:
                    raise InterruptedError('Analysis cancelled or timed out')
                if log.stat().st_size > 4 * 1024 * 1024:
                    raise InterruptedError('Analyzer log exceeded its byte limit')
                try:
                    return child.wait(timeout=min(.25, max(.01, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
    finally:
        cleanup_end = time.monotonic()+10
        try:
            if child is not None and child.poll() is None:
                if assigned:
                    win32job.TerminateJobObject(job, 125)
                else:
                    child.kill()
                child.wait(timeout=max(.01, cleanup_end-time.monotonic()))
            while True:
                pids = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList)
                if type(pids) is not tuple or len(pids) > 4096 or len(set(pids)) != len(pids) or any(type(pid) is not int or pid <= 0 for pid in pids):
                    raise InterruptedError('Analyzer family membership unknown')
                if not pids:
                    break
                win32job.TerminateJobObject(job, 125)
                if time.monotonic() >= cleanup_end:
                    raise InterruptedError('Analyzer family was not observed empty')
                time.sleep(.05)
        except Exception:
            raise InterruptedError('Analyzer cleanup could not prove terminal ownership') from None
        finally:
            try:
                job.Close()
            except Exception:
                raise InterruptedError('Analyzer Job close status is unknown') from None


def analyze_security_binary(project_folder, engagement_id, artifact_path, *, timeout_seconds=120, host):
    """Analyze a copied input with the fixed app exporter, never launch that input."""
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 300:
        raise ValueError('Choose 10..300 analyzer seconds')
    root = _project(project_folder)
    scope, scope_hash = _engagement(root, engagement_id, host)
    def cancel():
        return host.cancel()
    if artifact_path not in scope['artifact_paths']:
        raise ValueError('Artifact is outside this engagement')
    original = _inside(root, artifact_path)
    raw, digest, identity = _read(original, MAX_FILE, time.monotonic()+5)
    kind = file_kind(raw)
    if kind not in {'PE', 'ELF', 'MachO_or_fat_candidate'}:
        raise ValueError('Select a recognized PE, ELF or Mach-O candidate')
    app = Path(host.app_dir).resolve(strict=True)
    ghidra = app/'Toolbox'/GHIDRA
    java = app/'Toolbox'/JDK
    exporter = PACKAGE/EXPORTER
    # No caller-selected Ghidra installation, extensions, script or JVM options.
    utility = ghidra/'Ghidra/Framework/Utility/lib/Utility.jar'
    for item in (utility, java/'bin/java.exe', exporter):
        _identity(item)
    exporter_bytes, exporter_sha, _ = _read(exporter, 64000, time.monotonic()+2)
    if exporter_sha != EXPORTER_SHA256:
        raise ValueError('The fixed application exporter changed')
    job = root/'.local-ai/security/jobs'/uuid.uuid4().hex
    _ensure_directory(root, job)
    # Ghidra ProjectLocator rejects ANY dot-prefixed project-path component.
    # Keep subject/script/report under the task, but its transient Ghidra
    # database goes into an exclusive app-owned nonhidden project directory.
    transient_root = app/'Temp/SecurityAnalysis'
    if any(part.startswith('.') for part in transient_root.parts):
        raise ValueError('Ghidra project storage requires an app path without dot-prefixed components')
    _ensure_directory(app, transient_root)
    transient_project = transient_root/uuid.uuid4().hex
    transient_project.mkdir(exist_ok=False)
    _safe_directory(app, transient_project)
    input_file = job/'input.bin'
    with input_file.open('xb') as output:
        output.write(raw)
    scripts = job/'scripts'; scripts.mkdir()
    with (scripts/EXPORTER).open('xb') as output:
        output.write(exporter_bytes)
    result = job/'report.json'
    # Official launch.bat invokes Utility.jar -> ghidra.Ghidra -> AnalyzeHeadless.
    # Fixed direct Java argv avoids cmd percent/metacharacter expansion entirely.
    command = [str(java/'bin/java.exe'), '-Xmx2G', '-Xshare:off', '-XX:ParallelGCThreads=2', '-XX:CICompilerCount=2',
        '-Djava.system.class.loader=ghidra.GhidraClassLoader', '-Dfile.encoding=UTF8',
        '-Duser.country=US', '-Duser.language=en', '-Djava.awt.headless=true',
        '-Djavax.xml.accessExternalDTD=', '-Djavax.xml.accessExternalSchema=', '-Djavax.xml.accessExternalStylesheet=',
        '--enable-native-access=ALL-UNNAMED', '-Duser.home='+str(job), '-Djava.io.tmpdir='+str(job),
        '-Dapplication.settingsdir='+str(job/'settings'), '-Dapplication.cachedir='+str(job/'cache'),
        '-cp', str(utility), 'ghidra.Ghidra', 'ghidra.app.util.headless.AnalyzeHeadless',
        str(transient_project), 'Analysis', '-import', str(input_file),
        '-readOnly', '-analysisTimeoutPerFile', str(max(1, timeout_seconds-10)), '-max-cpu', '2',
        '-scriptPath', str(scripts), '-postScript', EXPORTER, str(result), '-log', str(job/'ghidra.log')]
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    env.update(JAVA_HOME=str(java), PATH=str(java/'bin'), TEMP=str(job), TMP=str(job), HOME=str(job))
    if _engagement(root, engagement_id, host)[1] != scope_hash:
        raise ValueError('Scope changed before analyzer launch')
    exit_code = _owned_windows_run(command, job, env, job/'runner.log', timeout_seconds, cancel)
    _, after_sha, after_identity = _read(original, MAX_FILE, time.monotonic()+5)
    if after_sha != digest or after_identity != identity or _engagement(root, engagement_id, host)[1] != scope_hash:
        raise ValueError('Artifact or engagement changed during analysis; report is stale')
    try:
        result.lstat()
    except FileNotFoundError:
        # A successful JVM exit does not prove importer/exporter success. Do
        # not call a missing report analyzed, or conceal the actual engine log.
        log = job/'runner.log'
        log_identity = _identity(log)
        with log.open('rb') as stream:
            stream.seek(max(0, log_identity[2]-6000))
            decoded = stream.read(6000).decode('utf-8', errors='replace')
            tail = decoded[-1200:]
            errors = [line for line in decoded.splitlines() if 'ERROR' in line]
            summary = errors[-1][:400] if errors else ''
        if _identity(log) != log_identity:
            raise ValueError('Engine diagnostic changed during confirmation')
        return {'status': 'error', 'engine': 'Ghidra 12.1.4', 'exit_code': exit_code,
            'input_sha256': digest, 'report_path': None,
            'log_path': str(log.relative_to(root)), 'diagnostic_tail': tail, 'diagnostic_summary': summary,
            'reason': 'The engine produced no bound static report; inspect the local log. No analysis success is claimed.',
            'limitations': ['Engine log is bounded untrusted task data, not source execution or vulnerability proof.']}
    output, report_sha, _ = _read(result, MAX_REPORT, time.monotonic()+3)
    report = json.loads(output)
    if not isinstance(report, dict) or report.get('schema') != 1 or report.get('input_sha256') != digest:
        raise ValueError('Analyzer output is not bound to the supplied artifact')
    return {'status': 'partial' if exit_code != 0 or report.get('analysis_timed_out') is not False else 'analyzed',
        'engine': 'Ghidra 12.1.4', 'input_sha256': digest, 'report_sha256': report_sha,
        'report_path': str(result.relative_to(root)), 'summary': {key: report.get(key) for key in ('format', 'language', 'function_count', 'exported_functions', 'functions_limited', 'analysis_timed_out')},
        'limitations': ['Decompilation is approximate; stripped, packed or unsupported code may remain opaque.', 'No target binary or extracted source was executed.', 'Raw decompilation remains in the local report, not automatically uploaded.']}


def map_cve_candidates(components, records, snapshot_id):
    """Pure bounded join to supplied official CVE JSON5 records, never references.

    Caller resolves snapshot_id through the official local intelligence store.
    This function cannot fetch an arbitrary URL or execute a reference/PoC.
    """
    if not isinstance(snapshot_id, str) or not re.fullmatch('[0-9a-f]{64}', snapshot_id):
        raise ValueError('An exact local CVE snapshot digest is required')
    if not isinstance(components, list) or len(components) > 4096 or not isinstance(records, list) or len(records) > 200:
        raise ValueError('Select a bounded component/candidate batch')
    hits = []
    for component in components:
        if not isinstance(component, dict) or not all(isinstance(component.get(k), str) for k in ('name', 'version', 'ecosystem')):
            raise ValueError('Component identity is unknown')
        normalize = lambda name: re.sub(r'[-_.]+', '-', name.lower()) if component['ecosystem'] == 'PyPI' else name.lower()
        for record in records:
            identifier = record.get('cveMetadata', {}).get('cveId') if isinstance(record, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'CVE-\d{4}-\d{4,}', identifier):
                continue
            if record.get('cveMetadata', {}).get('state') != 'PUBLISHED':
                continue
            affected = record.get('containers', {}).get('cna', {}).get('affected', [])
            if not isinstance(affected, list) or len(affected) > 1000:
                continue
            for item in affected:
                if not isinstance(item, dict):
                    continue
                name = item.get('packageName') or item.get('product')
                if not isinstance(name, str) or normalize(name) != normalize(component['name']):
                    continue
                exact = [v for v in item.get('versions', []) if isinstance(v, dict) and v.get('version') == component['version'] and not any(k in v for k in ('lessThan', 'lessThanOrEqual', 'changes'))]
                hits.append({'cve_id': identifier, 'component': component, 'snapshot_id': snapshot_id,
                    'mapping': 'candidate_exact_version' if any(v.get('status') == 'affected' for v in exact) else 'candidate_name_only',
                    'confirmed_vulnerability': False,
                    'reason': 'Product identity/ecosystem, deployment, ranges and applicability require verification; absence is not clearance.'})
                if len(hits) >= 100:
                    return {'candidates': hits, 'limited': True, 'snapshot_id': snapshot_id}
    return {'candidates': hits, 'limited': False, 'snapshot_id': snapshot_id}


def build_security_sbom(project_folder, engagement_id, *, host):
    """CycloneDX document of observed declarations; not an installed-package audit."""
    evidence = inspect_security_artifacts(project_folder, engagement_id, host=host)
    components = []
    for component in evidence['components']:
        item = {'type': 'library', 'name': component['name'], 'version': component['version'],
            'bom-ref': hashlib.sha256(json.dumps(component, sort_keys=True).encode('utf-8')).hexdigest(),
            'properties': [{'name': 'cale:ecosystem', 'value': component['ecosystem']},
                {'name': 'cale:evidence', 'value': component['evidence']},
                {'name': 'cale:observation', 'value': component['kind']}]}
        components.append(item)
    return {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
        'components': components, 'properties': [{'name': 'cale:scope-sha256', 'value': evidence['scope_sha256']},
            {'name': 'cale:coverage', 'value': 'Explicit supplied files; incomplete manifests are not inferred.'}]}
