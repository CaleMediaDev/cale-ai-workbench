"""Prepared CPU-only bridge controls; no analyzer, network or real user DB."""

import asyncio
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cale_security import bridge


@pytest.fixture
def task(tmp_path, monkeypatch):
    app = tmp_path/'app'; app.mkdir()
    state = app/'LongTasks'; state.mkdir()
    folder = tmp_path/'client'; folder.mkdir()
    (folder/'.local-ai').mkdir()
    (folder/'.local-ai/native-worker.json').write_text('{"status":"running"}', encoding='utf-8')
    (folder/'AGENTS.md').write_text('Inspect the supplied local artifacts only.', encoding='utf-8')
    (folder/'.local-ai/USER-REQUESTS.md').write_text('Keep the full original scope.', encoding='utf-8')
    (folder/'requirements.txt').write_text('neutral-package==1.2.3', encoding='utf-8')
    with sqlite3.connect(state/'projects.db') as connection:
        connection.executescript('CREATE TABLE projects(id TEXT,folder TEXT,brief TEXT,generation INTEGER,status TEXT);'
            'CREATE TABLE requests(project TEXT,action TEXT,status TEXT);')
        connection.execute('INSERT INTO projects VALUES(?,?,?,?,?)',
            ('client', str(folder), 'Inspect my supplied requirements and explain security leads.', 3, 'working'))
    context = ModuleType('project_memory')
    context._VERIFICATION_PROJECT = ContextVar('owned_test_project', default=None)
    token = context._VERIFICATION_PROJECT.set({'id':'client','folder':str(folder)})
    monkeypatch.setitem(sys.modules, 'project_memory', context)
    def registered(value):
        assert Path(value).resolve() == folder.resolve()
        return folder.resolve()
    yield app, folder, registered, context
    context._VERIFICATION_PROJECT.reset(token)


@pytest.fixture
def analysis(monkeypatch):
    import cale_security
    module = ModuleType('cale_security.security_analysis')
    module.AnalysisHost = lambda app_dir, project_folder, capture_scope, cancel: SimpleNamespace(
        app_dir=app_dir, project_folder=project_folder, capture_scope=capture_scope, cancel=cancel)
    calls = []
    def create(folder, description, paths, origins, *, host):
        basis = host.capture_scope()
        calls.append(('engagement', description, tuple(paths), basis))
        return {'engagement_id':'a'*32, 'scope':{'host_basis':basis, 'description':description}}
    def inspect(folder, identifier, *, host):
        calls.append(('inspect', host.capture_scope()))
        return {'status':'static_evidence','scope_sha256':'b'*64,'files':[], 'components':[],
            'coverage':'explicit_files_only','limitations':['Not confirmed vulnerabilities.']}
    def sbom(folder, identifier, *, host):
        calls.append(('sbom', host.capture_scope()))
        return {'bomFormat':'CycloneDX','specVersion':'1.6','components':[
            {'name':'neutral-package','version':'1.2.3','properties':[{'name':'cale:ecosystem','value':'PyPI'}]}],
            'properties':[]}
    def mapping(components, records, snapshot):
        calls.append(('mapping', components, records, snapshot))
        return {'candidates':[{'cve_id':'CVE-2026-12345','confirmed_vulnerability':False}], 'snapshot_id':snapshot}
    module.create_security_engagement = create
    module.inspect_security_artifacts = inspect
    module.build_security_sbom = sbom
    module.map_cve_candidates = mapping
    module.security_backend_status = lambda *, app_dir: {'app_dir':str(app_dir), 'runtime_qualified':False}
    module.analyze_security_binary = lambda *a, **k: pytest.fail('No analyzer is launched by bridge unit tests')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(cale_security, 'security_analysis', module, raising=False)
    return module, calls


class Index:
    def __init__(self, record=None):
        self.record = record or {'cveMetadata':{'cveId':'CVE-2026-12345','state':'PUBLISHED'},
            'containers':{'cna':{'descriptions':[{'value':'A candidate description.'}],
                'affected':[{'vendor':'Neutral','product':'neutral-package'}],
                'providerMetadata':{'shortName':'CNA'}, 'metrics':[]}}}
        self.calls = []
        self.revision = 'a'*64
    def status(self):
        return {'source':'CVEProject/cvelistV5','complete':True,'records':1,'states':{'PUBLISHED':1},
            'activated_at':1., 'active_provenance':[{'source_key':self.revision,'kind':'baseline','archive_sha256':'b'*64}],
            'integrity':'Local digest is not a publisher signature.','retention':'Old batches retained.'}
    def lookup(self, identifier):
        assert identifier == 'CVE-2026-12345'
        return {'cve_id':identifier,'state':'PUBLISHED','record':self.record,'record_sha256':bridge._digest(self.record),
            'source':'CVEProject/cvelistV5','batch_id':1}
    def search(self, query, *, limit):
        self.calls.append((query, limit)); return [self.lookup('CVE-2026-12345')]
    def product_candidates(self, vendor, product, *, version, limit):
        return self.search(vendor+' '+product, limit=limit)


def test_build_is_lazy_and_exports_only_handlers_and_schemas(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.sqlite3, 'connect', lambda *a, **k: pytest.fail('Build must not read/create a DB'))
    monkeypatch.setattr(Path, 'resolve', lambda *a, **k: pytest.fail('Build must not resolve/read an app'))
    catalog, handlers = bridge.build_tools(tmp_path/'not-created', lambda value: pytest.fail('Build registration I/O'))
    assert set(catalog) == set(handlers) and len(catalog) == 11
    assert all(callable(value) for value in handlers.values())
    assert all(value['inputSchema']['additionalProperties'] is False for value in catalog.values())
    assert not (tmp_path/'not-created').exists()


def test_public_queries_need_no_project_and_never_confirm_versions(tmp_path, monkeypatch):
    index = Index(); monkeypatch.setattr(bridge, '_readonly_index', lambda app: index)
    _, handlers = bridge.build_tools(tmp_path, lambda value: pytest.fail('Global query must not inspect task'))
    value = handlers['local.cve_product_candidates']('Neutral','neutral-package','1.2.3',limit=10)
    assert value['candidates'][0]['assessment'] == 'CANDIDATE'
    assert value['candidates'][0]['confirmed_vulnerability'] is False
    assert value['candidates'][0]['version_evaluation'] == 'not_version_evaluated'
    assert len(json.dumps(value, ensure_ascii=True)) <= bridge.OUTPUT_CHARS
    with pytest.raises(ValueError): handlers['local.cve_search']('neutral', limit=True)
    with pytest.raises(ValueError): handlers['local.cve_search']('neutral', limit=11)


@pytest.mark.parametrize('pointer', ['$..value','/__class__.__mro__','/bad~2escape','/array/-1','/array/01'])
def test_pointer_does_not_evaluate_or_accept_non_rfc_array_access(pointer):
    with pytest.raises((ValueError, KeyError)):
        bridge._pointer({'array':[0], 'value':1}, pointer)


def test_record_pages_escaped_keys_arrays_and_long_unicode_text(tmp_path, monkeypatch):
    index = Index({'a/b':{'~key':['\u754c'*20000, {'raw':'not automatically expanded'}]}})
    monkeypatch.setattr(bridge, '_readonly_index', lambda app: index)
    _, handlers = bridge.build_tools(tmp_path, lambda value: pytest.fail('Global record needs no task'))
    page = handlers['local.cve_record']('CVE-2026-12345', '/a~1b/~0key', limit=1)
    assert len(page['items']) == 1 and page['next_offset'] == 1
    text = handlers['local.cve_record']('CVE-2026-12345', '/a~1b/~0key/0')
    assert text['next_text_offset'] == len(text['text']) > 0
    assert len(json.dumps(text, ensure_ascii=True)) <= bridge.OUTPUT_CHARS
    assert text['references_fetched'] is False
    assert text['record_sha256'] == bridge._digest(index.record)


def test_query_adapter_does_not_initialize_missing_catalog(tmp_path):
    with pytest.raises(ValueError): bridge._readonly_index(tmp_path)
    assert not (tmp_path/'Knowledge').exists()


def test_existing_query_adapter_is_readonly_and_preserves_hash_percent_paths(tmp_path, monkeypatch):
    from cale_security import cve_intelligence
    app = tmp_path/'owned#percent%'; database = app/'Knowledge/CVE/index.sqlite3'
    cve_intelligence.CVEIndex(database)  # Owned setup only; no external records or network.
    before = database.read_bytes()
    monkeypatch.setattr(cve_intelligence.CVEIndex, '_initialize', lambda self: pytest.fail('Query must not initialize schema'))
    index = bridge._readonly_index(app)
    with index._db() as connection:
        assert connection.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError): connection.execute('CREATE TABLE forbidden(value)')
    assert index.status()['complete'] is False
    assert database.read_bytes() == before


def test_current_host_hashes_full_instructions_and_accepts_description_not_permission(task, analysis):
    app, folder, registered, context = task
    text = 'Neutral retained detail.\n'*10000 + 'Final original user constraint.'
    followup = folder/'.local-ai/USER-REQUESTS.md'
    raw = text.replace('\n', '\r\n').encode('utf-8')
    followup.write_bytes(raw)  # Explicit portable CRLF fixture, not runtime normalization.
    assert hashlib.sha256(raw).hexdigest() != hashlib.sha256(text.encode()).hexdigest()
    _, handlers = bridge.build_tools(app, registered)
    result = handlers['local.security_engagement'](str(folder), 'Summarize the supplied local dependencies.', ['requirements.txt'])
    assert result['permissions'] == ['local_static_analysis'] and result['live_checks_started'] is False
    basis = analysis[1][0][3]['instruction_basis']
    assert basis['followups'] == hashlib.sha256(followup.read_bytes()).hexdigest()
    assert basis['followups'] != hashlib.sha256(text.encode()).hexdigest()
    assert analysis[1][0][1] == 'Summarize the supplied local dependencies.'
    assert 'host' not in bridge.build_tools(app, registered)[0]['local.security_engagement']['inputSchema']['properties']


@pytest.mark.parametrize('action', ['pause','stop','delete','note','resume'])
def test_pending_controls_deny_before_analysis(task, analysis, action):
    app, folder, registered, _ = task
    with sqlite3.connect(app/'LongTasks/projects.db') as connection:
        connection.execute('INSERT INTO requests VALUES(?,?,?)', ('client',action,'pending'))
    _, handlers = bridge.build_tools(app, registered)
    with pytest.raises(InterruptedError): handlers['local.inspect_security_artifacts'](str(folder), 'a'*32)
    assert analysis[1] == []


def test_foreign_context_and_changed_generation_are_not_current_scope(task, analysis):
    app, folder, registered, context = task
    context._VERIFICATION_PROJECT.set({'id':'foreign','folder':str(folder)})
    with pytest.raises(ValueError): bridge._TaskScope(app, folder, registered).capture()
    context._VERIFICATION_PROJECT.set({'id':'client','folder':str(folder)})
    scope = bridge._TaskScope(app, folder, registered); scope.capture()
    with sqlite3.connect(app/'LongTasks/projects.db') as connection:
        connection.execute('UPDATE projects SET generation=4')
    assert scope.cancelled()
    with pytest.raises(InterruptedError): scope.capture()


def test_after_scope_change_discards_analysis_result(task, analysis):
    app, folder, registered, _ = task
    original = analysis[0].inspect_security_artifacts
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        (folder/'.local-ai/USER-REQUESTS.md').write_text('Changed user instruction.', encoding='utf-8')
        return value
    analysis[0].inspect_security_artifacts = changed
    _, handlers = bridge.build_tools(app, registered)
    with pytest.raises(InterruptedError): handlers['local.inspect_security_artifacts'](str(folder),'a'*32)


@pytest.mark.parametrize('raw', ['{"status":"paused"}','{"status":"stopped"}','{"status":1}','{"status":"running","x":1e309}','not JSON'])
def test_unknown_native_controls_cannot_authorize(task, analysis, raw):
    app, folder, registered, _ = task
    (folder/'.local-ai/native-worker.json').write_text(raw, encoding='utf-8')
    with pytest.raises((ValueError, InterruptedError)):
        bridge._TaskScope(app, folder, registered).capture()
    assert analysis[1] == []


def test_mapping_only_joins_host_sbom_and_official_records(task, analysis, monkeypatch):
    app, folder, registered, _ = task
    index = Index(); monkeypatch.setattr(bridge, '_readonly_index', lambda app: index)
    _, handlers = bridge.build_tools(app, registered)
    result = handlers['local.map_security_cves'](str(folder),'a'*32)
    call = next(call for call in analysis[1] if call[0] == 'mapping')
    assert call[1] == [{'name':'neutral-package','version':'1.2.3','ecosystem':'PyPI'}]
    assert call[2] == [index.record] and len(call[3]) == 64
    assert result['confirmed_vulnerability'] is False and len(result['sbom_snapshot_id']) == 64
    with pytest.raises(TypeError): handlers['local.map_security_cves'](str(folder),'a'*32, records=[{}])


def test_offline_sync_never_fetches_or_creates_knowledge(task, analysis, monkeypatch):
    app, folder, registered, _ = task
    offline = ModuleType('pc_offline'); offline.offline = lambda: True
    monkeypatch.setitem(sys.modules, 'pc_offline', offline)
    from cale_security import cve_intelligence
    monkeypatch.setattr(cve_intelligence, 'latest_release', lambda: pytest.fail('Offline sync must not fetch'))
    _, handlers = bridge.build_tools(app, registered)
    with pytest.raises(InterruptedError): asyncio.run(handlers['local.sync_cve_catalog'](str(folder)))
    assert not (app/'Knowledge').exists()


def test_backend_app_dir_is_host_only(task, analysis):
    app, folder, registered, _ = task
    _, handlers = bridge.build_tools(app, registered)
    assert handlers['local.security_backend_status']()['app_dir'] == str(app)
    with pytest.raises(TypeError): handlers['local.security_backend_status'](app_dir=str(folder))


@pytest.mark.parametrize('tool', ['local.cve_search', 'local.cve_product_candidates'])
def test_query_refuses_changed_catalog_snapshot(tmp_path, monkeypatch, tool):
    index = Index()
    search = index.search
    def changed(*args, **kwargs):
        rows = search(*args, **kwargs); index.revision = 'f'*64; return rows
    index.search = changed
    monkeypatch.setattr(bridge, '_readonly_index', lambda app: index)
    _, handlers = bridge.build_tools(tmp_path, lambda value: pytest.fail('No task needed'))
    args = ('neutral',) if tool == 'local.cve_search' else ('Neutral', 'neutral-package')
    with pytest.raises(ValueError, match='snapshot changed'):
        handlers[tool](*args)


@pytest.mark.parametrize('retention_fails', [False, True])
def test_sync_fixed_assets_then_bounded_retention_and_global_progress(task, analysis, monkeypatch, retention_fails):
    app, folder, registered, _ = task
    from cale_security import cve_intelligence
    offline = ModuleType('pc_offline'); offline.offline = lambda: False
    monkeypatch.setitem(sys.modules, 'pc_offline', offline)
    calls = []
    index = Index()
    def sync(asset, cache, *, cancel, progress):
        assert cancel() is False
        Path(cache).mkdir(parents=True, exist_ok=True)
        calls.append(('sync', asset, Path(cache)))
        progress({'phase':'index','records':1})
    def prune(cache, **kwargs):
        calls.append(('prune', Path(cache), kwargs))
        assert kwargs['cancel']() is False
        if retention_fails:
            raise RuntimeError('private-failure-message-not-for-output')
        return {'removed_batches':[1], 'candidates':[{'state':'removed'}], 'unknown_cache_files':0}
    index.sync = sync; index.prune_owned_cache = prune
    monkeypatch.setattr(cve_intelligence, 'latest_release', lambda: {'baseline':'fixed-official-baseline','delta':'fixed-official-delta'})
    monkeypatch.setattr(cve_intelligence, 'CVEIndex', lambda path: index)
    _, handlers = bridge.build_tools(app, registered)
    checkpoint = (folder/'.local-ai/native-worker.json').read_bytes()
    value = asyncio.run(handlers['local.sync_cve_catalog'](str(folder)))
    assert [row[1] for row in calls[:2]] == ['fixed-official-baseline','fixed-official-delta']
    assert calls[2][1] == app/'Knowledge/CVE/archives'
    assert calls[2][2]['dry_run'] is False and calls[2][2]['max_batches'] == 4
    assert value['retention_cleanup']['status'] == ('partial' if retention_fails else 'bounded_pass')
    assert 'private-failure-message' not in json.dumps(value)
    progress = json.loads((app/'Knowledge/CVE/sync-progress.json').read_text())
    assert progress['phase'] == ('complete_retention_partial' if retention_fails else 'complete')
    assert (folder/'.local-ai/native-worker.json').read_bytes() == checkpoint
