"""Owned tiny archives and fake transports only; no official bulk download."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest

# Installed tests use only the explicit package namespace.
from cale_security import cve_intelligence as cve


def record(number=1, state='PUBLISHED', description='Example Widget flaw'):
    return {'dataType': 'CVE_RECORD', 'dataVersion': '5.1',
            'cveMetadata': {'cveId': f'CVE-2026-{number:04d}', 'state': state},
            'containers': {'cna': {'providerMetadata': {'orgId': 'owned-cna'},
                'descriptions': [{'lang': 'en', 'value': description}],
                'affected': [{'vendor': 'Example', 'product': 'Widget',
                    'versions': [{'version': '1.0', 'lessThan': '2.0', 'status': 'affected'}]}],
                'references': [{'url': 'https://example.invalid/advisory'}],
                'metrics': [{'cvssV3_1': {'baseScore': 7.5}}]},
                'adp': [{'providerMetadata': {'orgId': 'owned-adp'},
                         'descriptions': [{'lang': 'en', 'value': 'ADP enrichment retained'}],
                         'affected': [{'product': 'ADP Widget'}]}]}}


def archive(tmp_path, records, *, kind='baseline', hour='1500', entries=None, suffix='', wrapped=False):
    path = tmp_path / ('fixture-' + kind + hour + suffix + '.zip')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as package:
        for data in records:
            package.writestr('cves/2026/0xxx/' + data['cveMetadata']['cveId'] + '.json', json.dumps(data))
        for name, raw in entries or []:
            package.writestr(name, raw)
    if wrapped:
        inner = path.read_bytes()
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as package:
            package.writestr('cves.zip', inner)
    raw = path.read_bytes()
    name = (('2026-11-06_all_CVEs_at_midnight.zip.zip' if wrapped else
             '2026-11-06_all_CVEs_at_midnight.zip') if kind == 'baseline'
            else f'2026-11-06_delta_CVEs_at_{hour}Z.zip')
    tag = f'cve_2026-11-06_{hour}Z'
    asset = cve.Asset(kind, tag, '2026-11-06', name,
                      f'https://github.com/CVEProject/cvelistV5/releases/download/{tag}/{name}',
                      len(raw), int(hour) + (1 if kind == 'baseline' else 2),
                      '2026-11-06T15:19:12Z', hashlib.sha256(raw).hexdigest())
    return asset, path


def index(tmp_path):
    return cve.CVEIndex(tmp_path / 'knowledge/index.sqlite3')


def ingest(store, asset, path, tmp_path, **kwargs):
    return store.sync_local_archive(asset, path, tmp_path / 'cache', **kwargs)


class Response(io.BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = headers or {}


def fake_download(raw, calls):
    def open_response(request, timeout):
        calls.append((request, timeout))
        start = int(request.get_header('Range', 'bytes=0-').split('=')[1].split('-')[0])
        headers = {'Content-Length': str(len(raw) - start), 'ETag': '"owned-version"'}
        if start:
            headers['Content-Range'] = f'bytes {start}-{len(raw) - 1}/{len(raw)}'
        return Response(raw[start:], 206 if start else 200, headers)
    return open_response


def test_official_verified_metadata_names_and_digests_not_assumed_nested_zip(tmp_path):
    baseline, _ = archive(tmp_path, [record()], wrapped=True)
    delta, _ = archive(tmp_path, [record(2)], kind='delta')
    rows = [{'name': asset.name, 'id': asset.asset_id, 'state': 'uploaded',
             'browser_download_url': asset.url, 'size': asset.size,
             'updated_at': asset.updated_at, 'digest': 'sha256:' + asset.sha256}
            for asset in (baseline, delta)]
    result = cve.select_release_assets({'tag_name': baseline.tag, 'draft': False,
                                       'prerelease': False, 'assets': rows})
    assert result == {'baseline': baseline, 'delta': delta}
    assert result['baseline'].name.endswith('.zip.zip')


@pytest.mark.parametrize('change', ['url', 'tag', 'size', 'digest', 'duplicate', 'missing'])
def test_release_selector_refuses_untrusted_ambiguous_or_incomplete_source(tmp_path, change):
    baseline, _ = archive(tmp_path, [record()])
    delta, _ = archive(tmp_path, [record(2)], kind='delta')
    release = {'tag_name': baseline.tag, 'draft': False, 'prerelease': False,
               'assets': [{'name': item.name, 'id': item.asset_id, 'state': 'uploaded',
                           'browser_download_url': item.url, 'size': item.size,
                           'updated_at': item.updated_at, 'digest': 'sha256:' + item.sha256}
                          for item in (baseline, delta)]}
    if change == 'url':
        release['assets'][0]['browser_download_url'] = baseline.url.replace('github.com', 'example.invalid')
    elif change == 'tag':
        release['tag_name'] = 'unreviewed-release'
    elif change == 'size':
        release['assets'][0]['size'] = True
    elif change == 'digest':
        release['assets'][0]['digest'] = 'sha256:not-a-hash'
    elif change == 'duplicate':
        release['assets'].append(release['assets'][0])
    else:
        release['assets'].pop()
    with pytest.raises(cve.CVEError):
        cve.select_release_assets(release)


def test_all_statuses_full_raw_cna_adp_versions_metrics_and_fts_are_preserved(tmp_path):
    published, rejected = record(), record(2, 'REJECTED')
    rejected['containers']['cna']['rejectedReasons'] = [{'lang': 'en', 'value': 'Duplicate'}]
    asset, path = archive(tmp_path, [published, rejected])
    store = index(tmp_path)
    status = ingest(store, asset, path, tmp_path)
    assert status['complete'] and status['records'] == 2
    assert status['states'] == {'PUBLISHED': 1, 'REJECTED': 1}
    assert store.lookup('CVE-2026-0001')['record'] == published
    assert store.lookup('CVE-2026-0002')['record'] == rejected
    assert store.search('ADP enrichment')[0]['assessment'] == 'candidate_only'
    candidates = store.product_candidates('Example', 'Widget', version='1.5')
    assert len(candidates) == 2
    assert all(item['target_version'] == '1.5' and item['version_evaluation'] == 'not_evaluated' for item in candidates)
    provenance = status['active_provenance'][0]
    assert provenance['archive_sha256'] == asset.sha256 and provenance['cursor'] == provenance['entries'] == 2


def test_cumulative_delta_replaces_prior_delta_not_cumulative_union(tmp_path):
    store = index(tmp_path)
    base, source = archive(tmp_path, [record(1), record(2)])
    ingest(store, base, source, tmp_path)
    delta, source = archive(tmp_path, [record(1, 'REJECTED'), record(3)], kind='delta')
    ingest(store, delta, source, tmp_path)
    assert store.status()['records'] == 3
    assert store.lookup('CVE-2026-0001')['state'] == 'REJECTED'
    newer, source = archive(tmp_path, [record(2, description='Latest cumulative edit')], kind='delta', hour='1600')
    ingest(store, newer, source, tmp_path)
    assert store.status()['records'] == 2
    assert store.lookup('CVE-2026-0001')['state'] == 'PUBLISHED'
    assert store.lookup('CVE-2026-0003') is None
    assert store.search('Latest cumulative')[0]['cve_id'] == 'CVE-2026-0002'


def test_interrupted_stream_resumes_range_exact_bytes_without_exposing_partial(tmp_path):
    asset, path = archive(tmp_path, [record(description='x' * (cve.CHUNK_BYTES * 3))])
    store, calls = index(tmp_path), []
    def interrupt(event):
        if event['phase'] == 'download':
            raise InterruptedError('owned checkpoint')
    with pytest.raises(InterruptedError):
        store.sync(asset, tmp_path / 'cache', opener=fake_download(path.read_bytes(), calls), progress=interrupt)
    assert not store.status()['complete'] and store.lookup('CVE-2026-0001') is None
    part = tmp_path / 'cache' / (asset.key + '.part')
    checkpoint = part.stat().st_size
    assert checkpoint == cve.CHUNK_BYTES
    status = store.sync(asset, tmp_path / 'cache', opener=fake_download(path.read_bytes(), calls))
    assert status['records'] == 1
    assert calls[1][0].get_header('Range') == f'bytes={checkpoint}-'
    assert calls[1][0].get_header('If-range') == '"owned-version"'
    assert hashlib.sha256(part.read_bytes()).hexdigest() == asset.sha256


def test_index_restart_resumes_atomic_record_cursor_and_no_duplicate_fts(tmp_path, monkeypatch):
    monkeypatch.setattr(cve, 'CHECKPOINT_RECORDS', 1)
    asset, path = archive(tmp_path, [record(1), record(2), record(3)])
    store = index(tmp_path)
    def interrupt(event):
        if event['phase'] == 'index' and event['entries_done'] == 1:
            raise InterruptedError('owned index checkpoint')
    with pytest.raises(InterruptedError):
        ingest(store, asset, path, tmp_path, progress=interrupt)
    assert store.status()['records'] == 0 and store.status()['recent_batches'][0]['records'] == 1
    restarted = cve.CVEIndex(store.path)
    status = ingest(restarted, asset, path, tmp_path)
    assert status['records'] == 3
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 3
        assert connection.execute('SELECT COUNT(*) FROM search_fts').fetchone()[0] == 3


def test_failed_delta_never_replaces_visible_baseline_and_retry_keeps_provenance(tmp_path):
    store = index(tmp_path)
    baseline, path = archive(tmp_path, [record()])
    ingest(store, baseline, path, tmp_path)
    bad, path = archive(tmp_path, [], kind='delta', entries=[('CVE-2026-0002.json', b'{broken')])
    with pytest.raises(cve.CVEError):
        ingest(store, bad, path, tmp_path)
    status = store.status()
    assert status['complete'] and status['records'] == 1
    assert status['recent_batches'][0]['state'] == 'partial'
    assert status['active_provenance'][0]['archive_sha256'] == baseline.sha256


@pytest.mark.parametrize('kind', ['digest', 'traversal', 'duplicate', 'disguised', 'bomb', 'empty'])
def test_bad_archive_is_not_partially_credited_or_extracted(tmp_path, monkeypatch, kind):
    store = index(tmp_path)
    records, entries = [record()], []
    if kind == 'traversal':
        entries = [('../outside-control.txt', b'never written')]
    elif kind == 'duplicate':
        records.append(record())
    elif kind == 'disguised':
        entries = [('unexpected.json', json.dumps(record(3)))]
    elif kind == 'empty':
        records = []
        entries = [('README.txt', b'no records')]
    asset, path = archive(tmp_path, records, entries=entries)
    if kind == 'digest':
        asset = replace(asset, sha256='0' * 64)
    if kind == 'bomb':
        monkeypatch.setattr(cve, 'MAX_RECORD_BYTES', 64)
    with pytest.raises((cve.CVEError, sqlite3.IntegrityError)):
        ingest(store, asset, path, tmp_path)
    assert not store.status()['complete'] and store.status()['records'] == 0
    assert not (tmp_path / 'outside-control.txt').exists()


def test_delta_requires_same_day_baseline_and_cannot_roll_back_snapshot(tmp_path):
    store = index(tmp_path)
    delta, path = archive(tmp_path, [record()], kind='delta')
    with pytest.raises(cve.CVEError, match='delta_baseline_required'):
        ingest(store, delta, path, tmp_path)
    base, path = archive(tmp_path, [record()])
    ingest(store, base, path, tmp_path)
    newer, path = archive(tmp_path, [record(2)], kind='delta', hour='1600')
    ingest(store, newer, path, tmp_path)
    older, path = archive(tmp_path, [record(3)], kind='delta', hour='1400')
    with pytest.raises(cve.CVEError, match='snapshot_rollback'):
        ingest(store, older, path, tmp_path)
    assert store.lookup('CVE-2026-0002') is not None and store.lookup('CVE-2026-0003') is None


def test_same_daily_digest_across_hourly_release_reuses_baseline_and_keeps_delta(tmp_path):
    store = index(tmp_path)
    base, path = archive(tmp_path, [record()])
    ingest(store, base, path, tmp_path)
    delta, path = archive(tmp_path, [record(2)], kind='delta')
    ingest(store, delta, path, tmp_path)
    new_tag = 'cve_2026-11-06_1600Z'
    same = replace(base, tag=new_tag, url=base.url.replace(base.tag, new_tag), asset_id=99999)
    def forbidden(*args, **kwargs):
        raise AssertionError('unchanged daily baseline must not download/reindex')
    status = store.sync(same, tmp_path / 'cache', opener=forbidden)
    assert status['records'] == 2 and len(status['recent_batches']) == 2


@pytest.mark.parametrize('problem', ['wrong_range', 'changed_etag', 'encoding', 'overflow'])
def test_resume_and_download_transport_integrity_fail_closed(tmp_path, problem):
    asset, path = archive(tmp_path, [record()])
    store = index(tmp_path)
    cache = tmp_path / 'cache'
    cache.mkdir()
    if problem in ('wrong_range', 'changed_etag'):
        batch = store._batch(asset)
        store._update(batch['id'], etag='"old"')
        (cache / (asset.key + '.part')).write_bytes(path.read_bytes()[:10])
    def response(request, timeout):
        raw = path.read_bytes()
        if problem == 'wrong_range':
            return Response(raw, 200)
        if problem == 'changed_etag':
            return Response(raw[10:], 206, {'Content-Range': f'bytes 10-{len(raw)-1}/{len(raw)}', 'ETag': '"new"'})
        if problem == 'encoding':
            return Response(raw, 200, {'Content-Encoding': 'gzip'})
        return Response(raw + b'extra')
    with pytest.raises(cve.CVEError):
        store.sync(asset, cache, opener=response)
    assert not store.status()['complete']


def test_bounded_release_parse_and_no_nonfinite_json_authority():
    for raw in (b'{"x":NaN}', b'{"x":1e309}', b'{"x":Infinity}'):
        with pytest.raises(cve.CVEError):
            cve._json(raw, 1024)
    with pytest.raises(cve.CVEError, match='json_size'):
        cve._json(b' ' * 20, 10)


def test_no_auth_or_unapproved_redirects_or_remote_scheme():
    redirect = cve._Redirects()
    for url in ('http://github.com/a', 'https://github.com.evil.invalid/a',
                'https://user:secret@github.com/a', 'https://example.invalid/a'):
        with pytest.raises(cve.CVEError, match='redirect_origin'):
            redirect.redirect_request(None, None, 302, '', {}, url)


def test_search_is_bounded_literal_data_and_never_executes_reference_or_record_text(tmp_path):
    store = index(tmp_path)
    malicious = record(description="__import__('os').system('never execute') Widget")
    asset, path = archive(tmp_path, [malicious])
    ingest(store, asset, path, tmp_path)
    assert store.lookup('CVE-2026-0001')['record'] == malicious
    assert store.search('" OR * Widget') == []
    for query, limit in [('', 1), ('x' * 513, 1), ('Widget', True), ('Widget', 101)]:
        with pytest.raises(cve.CVEError):
            store.search(query, limit=limit)


def test_exclusive_sync_lock_and_insufficient_disk_never_claim_complete(tmp_path, monkeypatch):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record()])
    lock = store.path.with_name(store.path.name + '.sync.lock')
    with cve._sync_lock(lock):
        with pytest.raises(cve.CVEError, match='sync_owned_elsewhere'):
            ingest(store, asset, path, tmp_path)
    from types import SimpleNamespace
    monkeypatch.setattr(cve.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=1))
    with pytest.raises(cve.CVEError, match='disk_space'):
        ingest(store, asset, path, tmp_path)
    assert not store.status()['complete']


def test_ancillary_entries_counted_without_extract_and_stored_digest_tamper_refused(tmp_path):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record()], entries=[('README.txt', b'data-only ancillary')])
    status = ingest(store, asset, path, tmp_path)
    assert status['recent_batches'][0]['skipped'] == 1
    assert not (tmp_path / 'README.txt').exists()
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE documents SET digest=?", ('0' * 64,))
    with pytest.raises(cve.CVEError, match='stored_record_digest'):
        store.lookup('CVE-2026-0001')


def test_official_specific_double_zip_baseline_streamed_and_provenance_linked(tmp_path):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record()], wrapped=True)
    events = []
    status = ingest(store, asset, path, tmp_path, progress=events.append)
    provenance = status['active_provenance'][0]
    inner = tmp_path / 'cache' / (asset.key + '.cves.zip')
    assert status['records'] == 1 and store.lookup('CVE-2026-0001') is not None
    assert provenance['archive_sha256'] == asset.sha256
    assert provenance['inner_sha256'] == hashlib.sha256(inner.read_bytes()).hexdigest()
    assert provenance['inner_bytes'] == inner.stat().st_size
    assert any(event['phase'] == 'baseline_unwrap' for event in events)
    assert not (tmp_path / 'cves.zip').exists()


def test_interrupted_specific_inner_stream_resumes_without_duplicate_records(tmp_path):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record(description='x' * (cve.CHUNK_BYTES * 3))], wrapped=True)
    def interrupt(event):
        if event['phase'] == 'baseline_unwrap':
            raise InterruptedError('owned inner checkpoint')
    with pytest.raises(InterruptedError):
        ingest(store, asset, path, tmp_path, progress=interrupt)
    assert not store.status()['complete']
    inner = tmp_path / 'cache' / (asset.key + '.cves.zip')
    assert inner.stat().st_size == cve.CHUNK_BYTES
    status = ingest(cve.CVEIndex(store.path), asset, path, tmp_path)
    assert status['records'] == 1


@pytest.mark.parametrize('name', ['another.zip', '../cves.zip', 'cves.zip/'])
def test_arbitrary_nested_zip_never_accepted_as_official_wrapper(tmp_path, name):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record()], wrapped=True)
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr(name, b'not an accepted member')
    asset = replace(asset, size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(cve.CVEError, match='baseline_wrapper_shape'):
        ingest(store, asset, path, tmp_path)
    assert not store.status()['complete']


def test_external_local_archive_without_official_digest_is_not_labeled_official(tmp_path):
    store = index(tmp_path)
    asset, path = archive(tmp_path, [record()])
    with pytest.raises(cve.CVEError, match='local_archive_upstream_digest_required'):
        ingest(store, replace(asset, sha256=None), path, tmp_path)
    assert not store.status()['complete']


def test_verified_empty_cumulative_delta_is_complete_but_never_empty_baseline(tmp_path):
    store = index(tmp_path)
    base, path = archive(tmp_path, [record()])
    ingest(store, base, path, tmp_path)
    earlier, path = archive(tmp_path, [record(2)], kind='delta')
    ingest(store, earlier, path, tmp_path)
    empty, path = archive(tmp_path, [], kind='delta', hour='1600')
    status = ingest(store, empty, path, tmp_path)
    assert status['complete'] and status['records'] == 1
    assert store.lookup('CVE-2026-0002') is None
    assert status['recent_batches'][0]['records'] == 0 and status['recent_batches'][0]['state'] == 'complete'


@pytest.mark.parametrize('metadata_path', ['cves/delta.json', 'cves/deltaLog.json'])
def test_official_large_history_metadata_preserves_all_cve_counts_and_provenance(tmp_path, metadata_path):
    # Genuine >16MiB official-path counterexample, not a manufactured parser error.
    history = json.dumps([{'note': 'x' * (cve.MAX_RECORD_BYTES + 1)}]).encode()
    asset, path = archive(tmp_path, [record(), record(2, 'REJECTED')], entries=[(metadata_path, history)])
    store = index(tmp_path)
    status = ingest(store, asset, path, tmp_path)
    assert status['complete'] and status['records'] == 2
    assert status['states'] == {'PUBLISHED': 1, 'REJECTED': 1}
    assert cve.MAX_RECORD_BYTES == 16 * 1024**2
    assert store.lookup('CVE-2026-0001')['record'] == record()
    assert store.lookup('CVE-2026-0002')['state'] == 'REJECTED'
    metadata = status['active_provenance'][0]['ancillary_metadata']
    assert metadata == [{'path': metadata_path, 'sha256': hashlib.sha256(history).hexdigest(),
                         'bytes': len(history), 'root_kind': 'array', 'nodes': 3}]
    assert status['active_provenance'][0]['skipped'] == 1


def test_known_history_metadata_has_separate_node_budget_for_actual_feed_shape(tmp_path):
    # Observed official deltaLog has643991 values-only nodes, above CVE500k cap.
    history = json.dumps([None] * 650000).encode()
    asset, path = archive(tmp_path, [record()], entries=[('cves/deltaLog.json', history)])
    store = index(tmp_path)
    status = ingest(store, asset, path, tmp_path)
    assert status['records'] == 1
    assert status['active_provenance'][0]['ancillary_metadata'][0]['nodes'] == 650001


@pytest.mark.parametrize('fault', ['nonfinite', 'overflow', 'concealed_root', 'concealed_nested',
                                  'depth', 'nodes', 'bytes'])
def test_known_metadata_cannot_hide_records_or_escape_bounds(tmp_path, monkeypatch, fault):
    if fault == 'nonfinite':
        raw = b'[NaN]'
    elif fault == 'overflow':
        raw = b'[1e309]'
    elif fault == 'concealed_root':
        raw = json.dumps(record(9)).encode()
    elif fault == 'concealed_nested':
        raw = json.dumps({'history': [record(9)]}).encode()
    elif fault == 'depth':
        raw = ('[' * 34 + '0' + ']' * 34).encode()
    elif fault == 'nodes':
        monkeypatch.setattr(cve, 'MAX_METADATA_NODES', 10)
        raw = json.dumps([None] * 11).encode()
    else:
        monkeypatch.setattr(cve, 'MAX_METADATA_BYTES', 1024)
        raw = json.dumps(['x' * 1025]).encode()
    asset, path = archive(tmp_path, [record()], entries=[('cves/deltaLog.json', raw)])
    store = index(tmp_path)
    with pytest.raises(cve.CVEError):
        ingest(store, asset, path, tmp_path)
    assert not store.status()['complete'] and store.status()['records'] == 0


@pytest.mark.parametrize('metadata_path', ['deltaLog.json', 'untrusted/deltaLog.json'])
def test_larger_metadata_exception_requires_exact_official_path(tmp_path, metadata_path):
    history = json.dumps(['x' * (cve.MAX_RECORD_BYTES + 1)]).encode()
    asset, path = archive(tmp_path, [record()], entries=[(metadata_path, history)])
    store = index(tmp_path)
    with pytest.raises(cve.CVEError, match='zip_expansion_bounds'):
        ingest(store, asset, path, tmp_path)
    assert not store.status()['complete']


def inactive_store(tmp_path, *, wrapped=False):
    store = index(tmp_path)
    old, path = archive(tmp_path, [record(description='Old snapshot')], wrapped=wrapped)
    ingest(store, old, path, tmp_path)
    current, path = archive(tmp_path, [record(description='Current snapshot')], hour='1600', suffix='current')
    ingest(store, current, path, tmp_path)
    return store, old, current


@pytest.mark.skipif(sys.platform != 'win32', reason='Held-object Windows GC only; other platforms decline')
def test_retention_previews_then_removes_only_inactive_complete_managed_bytes(tmp_path):
    store, old, current = inactive_store(tmp_path, wrapped=True)
    unknown = tmp_path / 'cache/owner-note.txt'
    unknown.write_bytes(b'preserve arbitrary bytes')
    before = store.lookup('CVE-2026-0001')
    preview = store.prune_owned_cache(tmp_path / 'cache')
    assert preview['dry_run'] and preview['removed_batches'] == []
    assert preview['candidates'][0]['state'] == 'verified_preview'
    assert (tmp_path / 'cache' / (old.key + '.part')).exists()
    result = store.prune_owned_cache(tmp_path / 'cache', dry_run=False)
    assert len(result['removed_batches']) == 1 and result['unknown_cache_files'] == 1
    assert not (tmp_path / 'cache' / (old.key + '.part')).exists()
    assert not (tmp_path / 'cache' / (old.key + '.cves.zip')).exists()
    assert (tmp_path / 'cache' / (current.key + '.part')).exists()
    assert unknown.read_bytes() == b'preserve arbitrary bytes'
    assert store.lookup('CVE-2026-0001') == before
    assert len(store.status()['recent_batches']) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM search_fts').fetchone()[0] == 1
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows held-file sharing behavior')
@pytest.mark.parametrize('obstruction', ['busy_reader', 'wrong_bytes', 'hardlink'])
def test_retention_preserves_unknown_changed_or_busy_cache_and_provenance(tmp_path, obstruction):
    store, old, _current = inactive_store(tmp_path)
    target = tmp_path / 'cache' / (old.key + '.part')
    outside = tmp_path / 'outside-control'
    reader = None
    if obstruction == 'busy_reader':
        reader = target.open('rb')
    elif obstruction == 'wrong_bytes':
        target.write_bytes(b'changed bytes')
    else:
        outside.write_bytes(target.read_bytes())
        target.unlink()
        import os
        os.link(outside, target)
    before = target.read_bytes()
    try:
        result = store.prune_owned_cache(tmp_path / 'cache', dry_run=False)
        assert result['removed_batches'] == []
        assert target.read_bytes() == before and len(store.status()['recent_batches']) == 2
        if obstruction == 'hardlink':
            assert outside.read_bytes() == before
    finally:
        if reader is not None:
            reader.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows held-directory and file rights')
def test_retention_held_objects_prevent_parent_swap_and_write_during_hash(tmp_path):
    store, old, _current = inactive_store(tmp_path)
    target = tmp_path / 'cache' / (old.key + '.part')
    attempted = []
    def cancel_check():
        if not attempted:
            with pytest.raises(PermissionError):
                with target.open('wb'):
                    pass
            attempted.append(True)
        return False
    with cve._held_cache_directories(tmp_path / 'cache') as api:
        with pytest.raises(PermissionError):
            (tmp_path / 'cache').rename(tmp_path / 'moved-cache')
        result = cve._remove_verified_cache_file(api, target, old.size, old.sha256,
                                                 dry_run=True, cancel=cancel_check)
    assert result == 'verified_preview' and attempted == [True]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == old.sha256


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows atomic handle deletion')
def test_retention_interrupted_after_first_file_keeps_db_provenance_and_resumes(tmp_path, monkeypatch):
    store, old, _current = inactive_store(tmp_path, wrapped=True)
    real = cve._remove_verified_cache_file
    calls = []
    def interrupt(api, path, size, digest, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise InterruptedError('owned injected interruption between managed file removals')
        return real(api, path, size, digest, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(cve, '_remove_verified_cache_file', interrupt)
        with pytest.raises(InterruptedError):
            store.prune_owned_cache(tmp_path / 'cache', dry_run=False)
    assert not (tmp_path / 'cache' / (old.key + '.part')).exists()
    assert (tmp_path / 'cache' / (old.key + '.cves.zip')).exists()
    assert len(store.status()['recent_batches']) == 2
    assert store.lookup('CVE-2026-0001')['record']['containers']['cna']['descriptions'][0]['value'] == 'Current snapshot'
    restarted = cve.CVEIndex(store.path)
    result = restarted.prune_owned_cache(tmp_path / 'cache', dry_run=False)
    assert len(result['removed_batches']) == 1 and len(restarted.status()['recent_batches']) == 1


def test_partial_batch_admission_is_bounded_and_existing_stage_can_resume(tmp_path):
    store = index(tmp_path)
    stages = []
    for hour in ('1500', '1600', '1700', '1800'):
        asset, path = archive(tmp_path, [record()], hour=hour, suffix=hour)
        stages.append((asset, path))
        with pytest.raises(InterruptedError):
            ingest(store, asset, path, tmp_path, cancel=lambda: True)
    extra, path = archive(tmp_path, [record()], hour='1900', suffix='extra')
    with pytest.raises(cve.CVEError, match='partial_batch_limit_resume_existing'):
        ingest(store, extra, path, tmp_path)
    assert len(store.status()['recent_batches']) == 4
    asset, path = stages[0]
    assert ingest(store, asset, path, tmp_path)['records'] == 1


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows qualified GC implementation')
def test_retention_never_removes_active_delta_or_partial_generation(tmp_path):
    store, _old, current = inactive_store(tmp_path)
    delta, path = archive(tmp_path, [record(2)], kind='delta', hour='1600')
    ingest(store, delta, path, tmp_path)
    partial, path = archive(tmp_path, [record(3)], kind='delta', hour='1700')
    with pytest.raises(InterruptedError):
        ingest(store, partial, path, tmp_path, cancel=lambda: True)
    before = store.status()
    result = store.prune_owned_cache(tmp_path / 'cache', dry_run=False)
    assert len(result['removed_batches']) == 1
    after = store.status()
    assert before['records'] == after['records'] == 2
    assert {row['source_key'] for row in after['recent_batches']} == {current.key, delta.key, partial.key}
    assert (tmp_path / 'cache' / (current.key + '.part')).exists()
    assert (tmp_path / 'cache' / (delta.key + '.part')).exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows qualified GC implementation')
def test_retention_inventory_overflow_refuses_before_deleting_any_known_bytes(tmp_path):
    store, old, _current = inactive_store(tmp_path)
    for i in range(4097):
        (tmp_path / 'cache' / ('unknown-' + str(i))).write_bytes(b'preserve')
    with pytest.raises(cve.CVEError, match='retention_cache_inventory_bound'):
        store.prune_owned_cache(tmp_path / 'cache', dry_run=False)
    assert (tmp_path / 'cache' / (old.key + '.part')).exists()
    assert len(store.status()['recent_batches']) == 2


@pytest.mark.parametrize('options', [{'max_batches': 0}, {'max_batches': 5}, {'keep_inactive': -1},
                                    {'keep_inactive': True}, {'dry_run': 1}])
def test_retention_options_are_strictly_bounded(tmp_path, options):
    store = index(tmp_path)
    with pytest.raises(cve.CVEError, match='retention_bounds'):
        store.prune_owned_cache(tmp_path / 'cache', **options)



def _covering_state_plan(store):
    with store._db() as connection:
        return [row[3] for row in connection.execute('EXPLAIN QUERY PLAN SELECT state,COUNT(*) n FROM ('
            + store._visible() + ') GROUP BY state')]


def test_visible_state_count_plan_uses_state_covering_index(tmp_path):
    store = index(tmp_path)
    baseline, path = archive(tmp_path, [record(1),record(2,state='REJECTED')])
    ingest(store, baseline, path, tmp_path)
    # A same-day cumulative delta, not another baseline.
    delta, path = archive(tmp_path, [record(1,state='REJECTED'),record(3)], kind='delta')
    ingest(store, delta, path, tmp_path)
    plan = _covering_state_plan(store)
    outer = [step for step in plan if 'SEARCH d ' in step or 'SCAN d ' in step]
    assert outer and all('USING COVERING INDEX document_batch_cve_state' in step for step in outer), plan
    assert store.status()['states'] == {'PUBLISHED':1,'REJECTED':2}
    assert store.status()['records'] == 3


@pytest.mark.parametrize('override', ['PUBLISHED','REJECTED','RESERVED'])
def test_covering_index_preserves_full_state_counts_and_delta_override(tmp_path, override):
    store = index(tmp_path)
    baseline, path = archive(tmp_path, [record(1),record(2,state='REJECTED'),record(3,state='RESERVED')])
    ingest(store, baseline, path, tmp_path)
    delta, path = archive(tmp_path, [record(1,state=override),record(4,state='PUBLISHED')], kind='delta')
    ingest(store, delta, path, tmp_path)
    expected = {'REJECTED':1,'RESERVED':1,'PUBLISHED':1}
    expected[override] = expected.get(override,0)+1
    assert store.status()['states'] == expected
    assert store.status()['records'] == 4
    assert store.lookup('CVE-2026-0001')['state'] == override
    assert store.lookup('CVE-2026-0003')['state'] == 'RESERVED'


def test_existing_v1_database_index_migration_is_idempotent_and_data_preserving(tmp_path):
    store = index(tmp_path)
    baseline, path = archive(tmp_path, [record(1),record(2,state='REJECTED')])
    ingest(store, baseline, path, tmp_path)
    delta, path = archive(tmp_path, [record(1,state='REJECTED'),record(3)], kind='delta')
    ingest(store, delta, path, tmp_path)
    # Simulate exact legacy schema: old code had both other indexes but no
    # state-covering index. This is owned TEMP setup, not a real migration.
    with store._db() as connection:
        connection.execute('DROP INDEX IF EXISTS document_batch_cve_state')
        before = [tuple(row) for row in connection.execute('SELECT * FROM documents ORDER BY id')]
        batches = [tuple(row) for row in connection.execute('SELECT * FROM batches ORDER BY id')]
        active = [tuple(row) for row in connection.execute('SELECT * FROM active')]
        fts = [tuple(row) for row in connection.execute('SELECT rowid,cve_id,facets FROM search_fts ORDER BY rowid')]
    old_status = store.status()
    migrated = cve.CVEIndex(store.path)
    again = cve.CVEIndex(store.path)
    with again._db() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM documents ORDER BY id')] == before
        assert [tuple(row) for row in connection.execute('SELECT * FROM batches ORDER BY id')] == batches
        assert [tuple(row) for row in connection.execute('SELECT * FROM active')] == active
        assert [tuple(row) for row in connection.execute('SELECT rowid,cve_id,facets FROM search_fts ORDER BY rowid')] == fts
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='document_batch_cve_state'").fetchone()[0] == 1
        assert [row[2] for row in connection.execute('PRAGMA index_info(document_batch_cve_state)')] == ['batch_id','cve_id','state']
    status = migrated.status()
    for key in ('source','complete','activated_at','records','states','active_provenance','freshness','integrity','retention'):
        assert status[key] == old_status[key]
    assert migrated.lookup('CVE-2026-0001')['state'] == 'REJECTED'
    assert migrated.search('Widget',limit=10) == store.search('Widget',limit=10)
