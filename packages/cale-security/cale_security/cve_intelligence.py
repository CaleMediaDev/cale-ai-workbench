"""Official CVEProject data mirror. Nothing in a CVE record is executable.

The caller chooses the local database/cache. A complete baseline plus the latest
*cumulative since midnight* delta is visible atomically; an incomplete batch is
never searchable. Keyword/product results are candidates, not vulnerability
confirmation. Bulk data is external to the application distribution.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import struct
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile
import zlib


RELEASES_URL = 'https://api.github.com/repos/CVEProject/cvelistV5/releases/latest'
SOURCE = 'CVEProject/cvelistV5'
MAX_API_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_RECORD_BYTES = 16 * 1024**2
MAX_METADATA_BYTES = 64 * 1024**2
MAX_METADATA_NODES = 2_000_000
OFFICIAL_METADATA = frozenset(('cves/delta.json', 'cves/deltaLog.json'))
MAX_DIRECTORY_BYTES = 128 * 1024**2
MAX_ENTRIES = 1_000_000
MAX_EXPANDED_BYTES = 12 * 1024**3
CHUNK_BYTES = 256 * 1024
CHECKPOINT_RECORDS = 100
MAX_PARTIAL_BATCHES = 4
CVE_ID = re.compile(r'CVE-[0-9]{4}-[0-9]{4,19}\Z')
TAG = re.compile(r'cve_([0-9]{4}-[0-9]{2}-[0-9]{2})_[0-9]{4}Z\Z')


class CVEError(ValueError):
    """Bounded data/source failure. Raw upstream messages are not propagated."""


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _finite_float(value):
    number = float(value)
    if not (-float('inf') < number < float('inf')):
        raise CVEError('nonfinite_json')
    return number


def _json(raw, limit, *, node_limit=500_000, depth_limit=128):
    if len(raw) > limit:
        raise CVEError('json_size')
    try:
        value = json.loads(raw, parse_float=_finite_float,
                           parse_constant=lambda _value: (_ for _ in ()).throw(CVEError('nonfinite_json')))
        pending = [(value, 0)]
        nodes = 0
        while pending:
            current, depth = pending.pop()
            nodes += 1
            if depth > depth_limit or nodes > node_limit:
                raise CVEError('json_structure')
            if isinstance(current, dict):
                pending.extend((child, depth + 1) for child in current.values())
            elif isinstance(current, list):
                pending.extend((child, depth + 1) for child in current)
        return value
    except CVEError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise CVEError('invalid_json') from None


def _official_asset_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    url = urlsplit(value)
    return (url.scheme == 'https' and url.netloc == 'github.com'
            and url.path.startswith('/CVEProject/cvelistV5/releases/download/')
            and not url.query and not url.fragment and not url.username)


@dataclass(frozen=True)
class Asset:
    kind: str
    tag: str
    baseline_date: str
    name: str
    url: str
    size: int
    asset_id: int
    updated_at: str
    sha256: str | None = None

    def validate(self):
        match = TAG.fullmatch(self.tag) if isinstance(self.tag, str) else None
        if (self.kind not in ('baseline', 'delta') or not match
                or match.group(1) != self.baseline_date
                or not _official_asset_url(self.url)
                or urlsplit(self.url).path != f'/CVEProject/cvelistV5/releases/download/{self.tag}/{self.name}'
                or not _integer(self.size, 1, MAX_ARCHIVE_BYTES)
                or not _integer(self.asset_id, 1, 2**63 - 1)
                or not isinstance(self.updated_at, str)
                or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', self.updated_at)):
            raise CVEError('asset_identity')
        prefix = re.escape(self.baseline_date) + '_'
        expected = prefix + (r'all_CVEs_at_midnight\.zip(?:\.zip)?' if self.kind == 'baseline'
                             else 'delta_CVEs_at_' + re.escape(self.tag[-5:]) + r'\.zip')
        if not re.fullmatch(expected, self.name):
            raise CVEError('asset_name')
        if self.sha256 is not None and not re.fullmatch('[a-f0-9]{64}', self.sha256):
            raise CVEError('asset_digest')
        return self

    @property
    def key(self):
        self.validate()
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def select_release_assets(release):
    """Select exact official ZIP assets; reject ambiguous or incomplete metadata.

    No checksum is invented when GitHub's digest field is absent. The download
    still records a local digest, which is integrity evidence, not a signature.
    """
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise CVEError('release_status')
    tag = release.get('tag_name')
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    assets = release.get('assets')
    if not match or not isinstance(assets, list) or len(assets) > 32:
        raise CVEError('release_metadata')
    selected = {}
    for row in assets:
        if not isinstance(row, dict):
            raise CVEError('release_asset')
        name = row.get('name', '')
        prefix = re.escape(match.group(1)) + '_'
        kind = ('baseline' if isinstance(name, str) and re.fullmatch(prefix + r'all_CVEs_at_midnight\.zip(?:\.zip)?', name) else
                'delta' if isinstance(name, str) and re.fullmatch(prefix + r'delta_CVEs_at_[0-9]{4}Z\.zip', name) else None)
        if kind is None:
            continue
        if kind in selected or row.get('state') != 'uploaded':
            raise CVEError('ambiguous_asset')
        digest = row.get('digest')
        if digest is not None:
            if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
                raise CVEError('upstream_digest')
            digest = digest[7:]
        selected[kind] = Asset(kind, tag, match.group(1), name,
                               row.get('browser_download_url'), row.get('size'),
                               row.get('id'), row.get('updated_at'), digest).validate()
    if set(selected) != {'baseline', 'delta'}:
        raise CVEError('missing_release_asset')
    return selected


def _allowed_download(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and not parsed.fragment
            and (parsed.netloc in ('github.com', 'api.github.com', 'release-assets.githubusercontent.com',
                                   'objects.githubusercontent.com') or
                 parsed.netloc == 'github-releases.githubusercontent.com'))


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not _allowed_download(newurl):
            raise CVEError('redirect_origin')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _open(request, timeout=30):
    if not _allowed_download(request.full_url):
        raise CVEError('request_origin')
    return build_opener(ProxyHandler({}), _Redirects()).open(request, timeout=timeout)


def latest_release(*, opener=None):
    """Fetch bounded public release metadata. No credentials or accounts used."""
    with (opener or _open)(Request(RELEASES_URL, headers={'Accept': 'application/vnd.github+json',
                                                       'User-Agent': 'Cale-CVE-data-mirror'}), timeout=15) as response:
        if response.status != 200:
            raise CVEError('release_http')
        raw = response.read(MAX_API_BYTES + 1)
    return select_release_assets(_json(raw, MAX_API_BYTES))


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise InterruptedError('CVE sync cancelled')


def _plain(path, *, allow_missing=False):
    """Ordinary containment guard; not a sandbox against a hostile same user."""
    path = Path(path).absolute()
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise CVEError('cache_missing') from None
        if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CVEError('cache_link')
        if item == path and item.is_file() and info.st_nlink != 1:
            raise CVEError('cache_hardlink')
    return path


@contextmanager
def _sync_lock(path):
    path = _plain(path, allow_missing=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CVEError('sync_owned_elsewhere') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _zip_bounds(path):
    """Bound central-directory allocation before ZipFile materializes entries."""
    size = path.stat().st_size
    with path.open('rb') as handle:
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
        offset = tail.rfind(b'PK\x05\x06')
        if offset < 0 or offset + 22 > len(tail):
            raise CVEError('zip_directory')
        _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from('<4s4H2LH', tail, offset)
        eocd = size - len(tail) + offset
        if disk or cd_disk or disk_count != count or offset + 22 + comment != len(tail):
            raise CVEError('zip_multidisk_or_trailing')
        if count == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff:
            handle.seek(eocd - 20)
            locator = handle.read(20)
            if len(locator) != 20:
                raise CVEError('zip64_locator')
            signature, disk, location, disks = struct.unpack('<4sLQL', locator)
            if signature != b'PK\x06\x07' or disk or disks != 1 or location >= eocd:
                raise CVEError('zip64_locator')
            handle.seek(location)
            raw = handle.read(56)
            if len(raw) != 56:
                raise CVEError('zip64_directory')
            signature, record_size, _made, _needed, disk, cd_disk, disk_count, count, cd_size, cd_offset = struct.unpack('<4sQ2H2L4Q', raw)
            if signature != b'PK\x06\x06' or record_size < 44 or disk or cd_disk or disk_count != count:
                raise CVEError('zip64_directory')
        if not 0 <= count <= MAX_ENTRIES or cd_size > MAX_DIRECTORY_BYTES or cd_offset + cd_size > size:
            raise CVEError('zip_directory_bounds')
    return count


def _record(raw, filename):
    data = _json(raw, MAX_RECORD_BYTES)
    if not isinstance(data, dict) or data.get('dataType') != 'CVE_RECORD':
        raise CVEError('record_type')
    metadata = data.get('cveMetadata')
    containers = data.get('containers', {})
    if not isinstance(metadata, dict) or not isinstance(containers, dict):
        raise CVEError('record_shape')
    cve_id, state = metadata.get('cveId'), metadata.get('state')
    if not isinstance(cve_id, str) or not CVE_ID.fullmatch(cve_id) or cve_id != filename[:-5]:
        raise CVEError('record_id')
    if not isinstance(state, str) or not 0 < len(state) <= 64:
        raise CVEError('record_state')
    cna, adp = containers.get('cna'), containers.get('adp', [])
    if (cna is not None and not isinstance(cna, dict)) or not isinstance(adp, list) or any(not isinstance(x, dict) for x in adp):
        raise CVEError('container_shape')
    providers = []
    for role, container in ([('CNA', cna)] if cna is not None else []) + [('ADP', item) for item in adp]:
        providers.append({'role': role, **{key: container[key] for key in
                         ('providerMetadata', 'title', 'descriptions', 'affected', 'references', 'metrics',
                          'problemTypes', 'rejectedReasons', 'replacedBy') if key in container}})
    facets = json.dumps(providers, ensure_ascii=False, separators=(',', ':'))
    return cve_id, state, facets, data


def _metadata(raw):
    """Validate the two known history documents without treating them as CVEs.

    Their raw bytes remain in the pinned archive. The index retains provenance
    and structural summary, not a second 38MB history copy or imported facts.
    """
    data = _json(raw, MAX_METADATA_BYTES, node_limit=MAX_METADATA_NODES, depth_limit=32)
    if not isinstance(data, (dict, list)):
        raise CVEError('metadata_shape')
    pending, nodes = [data], 0
    while pending:
        current = pending.pop()
        nodes += 1
        if isinstance(current, dict):
            if current.get('dataType') == 'CVE_RECORD':
                raise CVEError('concealed_cve_record')
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return ('object' if isinstance(data, dict) else 'array'), nodes


@contextmanager
def _held_cache_directories(cache):
    """Windows no-delete directory handles stabilize managed-cache ancestry.

    File GC deliberately declines on other platforms until equivalent atomic
    held-object deletion is qualified. It never falls back to path unlink.
    """
    if os.name != 'nt':
        raise CVEError('gc_platform_unqualified')
    import ctypes
    from ctypes import wintypes as W
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    kernel.GetFileInformationByHandleEx.restype = W.BOOL
    kernel.SetFileInformationByHandle.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    kernel.SetFileInformationByHandle.restype = W.BOOL
    class Attributes(ctypes.Structure):
        _fields_ = [('attributes', W.DWORD), ('tag', W.DWORD)]
    def attributes(handle):
        info = Attributes()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400:
            raise CVEError('gc_reparse')
    handles = []
    try:
        for directory in [*reversed(cache.parents), cache]:
            _plain(directory)
            handle = kernel.CreateFileW('\\\\?\\' + str(directory), 0x80000000, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            attributes(handle)
        yield kernel, attributes
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _remove_verified_cache_file(api, path, size, digest, *, dry_run, cancel):
    """Hash under a no-write/no-delete handle; delete THAT handle only."""
    import ctypes
    import msvcrt
    path = _plain(path, allow_missing=True)
    try:
        path.lstat()
    except FileNotFoundError:
        return 'missing'
    kernel, attributes = api
    handle = kernel.CreateFileW('\\\\?\\' + str(path), 0x80010000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return 'busy_or_unavailable'
    fd = None
    try:
        attributes(handle)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        with os.fdopen(fd, 'rb') as stream:
            fd = -1  # stream now owns the Win32 handle.
            before = os.fstat(stream.fileno())
            if before.st_nlink != 1 or before.st_size != size:
                return 'identity_mismatch'
            actual = hashlib.sha256()
            while chunk := stream.read(CHUNK_BYTES):
                _check_cancel(cancel)
                actual.update(chunk)
            after = os.fstat(stream.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or after.st_nlink != 1 or actual.hexdigest() != digest):
                return 'digest_or_identity_mismatch'
            _check_cancel(cancel)
            if dry_run:
                return 'verified_preview'
            class Disposition(ctypes.Structure):
                _fields_ = [('delete', ctypes.c_ubyte)]
            disposition = Disposition(1)
            if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                return 'busy_or_unavailable'
        return 'deleted'
    finally:
        if fd is None:
            kernel.CloseHandle(handle)
        elif fd != -1:
            os.close(fd)


class CVEIndex:
    """SQLite/FTS5 mirror, with visible complete batches and resumable staging.

    Disk space is caller-owned. Old complete/staging batches are retained, not
    silently pruned. Status exposes their storage footprint for later explicit
    retention decisions. Readers cannot treat a partial sync as complete.
    """

    def __init__(self, database):
        self.path = _plain(database, allow_missing=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._db() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise CVEError('database_version')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
                    asset TEXT NOT NULL, kind TEXT NOT NULL, baseline_date TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'partial', downloaded INTEGER NOT NULL DEFAULT 0,
                    archive_sha256 TEXT, etag TEXT, modified TEXT,
                    inner_sha256 TEXT, inner_bytes INTEGER,
                    cursor INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0, entries INTEGER,
                    started REAL NOT NULL, completed REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
                    cve_id TEXT NOT NULL, state TEXT NOT NULL, digest TEXT NOT NULL,
                    raw_zlib BLOB NOT NULL, facets TEXT NOT NULL,
                    UNIQUE(batch_id,cve_id));
                CREATE INDEX IF NOT EXISTS document_cve ON documents(cve_id,batch_id);
                CREATE INDEX IF NOT EXISTS document_batch_cve_state ON documents(batch_id,cve_id,state);
                CREATE TABLE IF NOT EXISTS ancillary_metadata (
                    batch_id INTEGER NOT NULL REFERENCES batches(id), path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, root_kind TEXT NOT NULL,
                    nodes INTEGER NOT NULL, PRIMARY KEY(batch_id,path));
                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(cve_id, facets);
                CREATE TABLE IF NOT EXISTS active (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    baseline INTEGER NOT NULL REFERENCES batches(id),
                    delta INTEGER REFERENCES batches(id), activated REAL NOT NULL);
                PRAGMA user_version=1;
            ''')

    def _batch(self, asset):
        with self._db() as connection:
            known = connection.execute('SELECT * FROM batches WHERE source_key=?', (asset.key,)).fetchone()
            if known is not None:
                return dict(known)
            if connection.execute("SELECT COUNT(*) FROM batches WHERE state='partial'").fetchone()[0] >= MAX_PARTIAL_BATCHES:
                raise CVEError('partial_batch_limit_resume_existing')
            connection.execute('INSERT OR IGNORE INTO batches(source_key,asset,kind,baseline_date,started) VALUES(?,?,?,?,?)',
                               (asset.key, json.dumps(asdict(asset), sort_keys=True), asset.kind, asset.baseline_date, time.time()))
            return dict(connection.execute('SELECT * FROM batches WHERE source_key=?', (asset.key,)).fetchone())

    def _update(self, batch_id, **values):
        allowed = {'downloaded', 'archive_sha256', 'etag', 'modified', 'entries', 'error', 'inner_sha256', 'inner_bytes'}
        if not values or not set(values) <= allowed:
            raise CVEError('internal_update')
        with self._db() as connection:
            connection.execute('UPDATE batches SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                               (*values.values(), batch_id))

    def _download(self, asset, batch, cache, opener, cancel, progress):
        part = _plain(cache / (asset.key + '.part'), allow_missing=True)
        _check_cancel(cancel)
        # File bytes, not a possibly stale database counter, govern Range resume.
        offset = part.stat().st_size if part.exists() else 0
        if offset > asset.size:
            raise CVEError('cached_size')
        digest = hashlib.sha256()
        if offset:
            with part.open('rb') as handle:
                while chunk := handle.read(CHUNK_BYTES):
                    _check_cancel(cancel)
                    digest.update(chunk)
        if offset < asset.size:
            headers = {'User-Agent': 'Cale-CVE-data-mirror', 'Accept-Encoding': 'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
                if batch['etag'] or batch['modified']:
                    headers['If-Range'] = batch['etag'] or batch['modified']
            with opener(Request(asset.url, headers=headers), timeout=30) as response:
                if response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise CVEError('encoded_archive')
                if offset:
                    expected = f'bytes {offset}-{asset.size - 1}/{asset.size}'
                    if response.status != 206 or response.headers.get('Content-Range') != expected:
                        raise CVEError('resume_response_changed')
                    if batch['etag'] and response.headers.get('ETag') != batch['etag']:
                        raise CVEError('resume_identity_changed')
                elif response.status != 200:
                    raise CVEError('download_http')
                if response.headers.get('Content-Length') not in (None, str(asset.size - offset)):
                    raise CVEError('download_length')
                self._update(batch['id'], etag=response.headers.get('ETag'), modified=response.headers.get('Last-Modified'))
                with part.open('ab') as handle:
                    while chunk := response.read(CHUNK_BYTES):
                        _check_cancel(cancel)
                        if offset + len(chunk) > asset.size:
                            raise CVEError('download_overflow')
                        handle.write(chunk)
                        digest.update(chunk)
                        offset += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                        self._update(batch['id'], downloaded=offset)
                        if progress:
                            progress({'phase': 'download', 'source_key': asset.key, 'bytes_done': offset, 'bytes_total': asset.size})
        if offset != asset.size:
            raise CVEError('download_incomplete')
        actual = digest.hexdigest()
        if asset.sha256 is not None and actual != asset.sha256:
            raise CVEError('download_digest')
        if batch['archive_sha256'] is not None and actual != batch['archive_sha256']:
            raise CVEError('cache_changed')
        self._update(batch['id'], downloaded=offset, archive_sha256=actual)
        return part

    def _stage_local(self, asset, source, cache, cancel, progress):
        source = _plain(source)
        target = _plain(cache / (asset.key + '.part'), allow_missing=True)
        if source == target:
            return
        before = source.stat()
        if not source.is_file() or before.st_size != asset.size:
            raise CVEError('local_archive_size')
        offset = target.stat().st_size if target.exists() else 0
        if offset > asset.size:
            raise CVEError('cached_size')
        # Compare the existing prefix before resuming a local-copy checkpoint.
        with source.open('rb') as reader:
            opened = os.fstat(reader.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                raise CVEError('local_archive_changed')
            if offset:
                with target.open('rb') as existing:
                    remaining = offset
                    while remaining:
                        _check_cancel(cancel)
                        size = min(CHUNK_BYTES, remaining)
                        if reader.read(size) != existing.read(size):
                            raise CVEError('local_archive_prefix')
                        remaining -= size
            with target.open('ab') as writer:
                while chunk := reader.read(CHUNK_BYTES):
                    _check_cancel(cancel)
                    if offset + len(chunk) > asset.size:
                        raise CVEError('local_archive_changed')
                    writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                    offset += len(chunk)
                    if progress:
                        progress({'phase': 'local_copy', 'source_key': asset.key,
                                  'bytes_done': offset, 'bytes_total': asset.size})
            after = os.fstat(reader.fileno())
        current = source.stat()
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise CVEError('local_archive_changed')

    def sync_local_archive(self, asset, path, cache_root, *, cancel=None, progress=None):
        """Ingest an already downloaded official asset; never opens the network.

        Copies to owned content-checked cache in bounded chunks so an interrupted
        index can resume independently of the external download's lifetime.
        """
        if asset.sha256 is None:
            raise CVEError('local_archive_upstream_digest_required')
        def no_network(*_args, **_kwargs):
            raise CVEError('local_archive_incomplete')
        return self.sync(asset, cache_root, opener=no_network, cancel=cancel,
                         progress=progress, _local_archive=path)

    def _official_inner(self, asset, archive, batch, cache, cancel, progress):
        """The observed official double-ZIP baseline has ONE cves.zip member.

        This is a specific format adapter, not recursive ZIP extraction. The
        fixed cache filename derives from the pinned outer asset identity.
        Re-reading the outer member checks its full CRC even after interruption.
        """
        if asset.kind != 'baseline' or not asset.name.endswith('.zip.zip'):
            return archive
        if _zip_bounds(archive) != 1:
            raise CVEError('baseline_wrapper_shape')
        inner = _plain(cache / (asset.key + '.cves.zip'), allow_missing=True)
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            info = entries[0]
            if (info.filename != 'cves.zip' or info.is_dir() or info.flag_bits & 1
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                    or not 0 < info.file_size <= MAX_ARCHIVE_BYTES):
                raise CVEError('baseline_wrapper_shape')
            offset = inner.stat().st_size if inner.exists() else 0
            if offset > info.file_size:
                raise CVEError('inner_cache_size')
            if shutil.disk_usage(cache).free < info.file_size - offset + 64 * 1024**2:
                raise CVEError('inner_disk_space')
            digest = hashlib.sha256()
            written = 0
            with package.open(info) as source:
                if offset:
                    with inner.open('rb') as previous:
                        remaining = offset
                        while remaining:
                            _check_cancel(cancel)
                            length = min(CHUNK_BYTES, remaining)
                            chunk = source.read(length)
                            if len(chunk) != length or chunk != previous.read(length):
                                raise CVEError('inner_cache_changed')
                            digest.update(chunk)
                            written += len(chunk)
                            remaining -= len(chunk)
                with inner.open('ab') as target:
                    while chunk := source.read(CHUNK_BYTES):
                        _check_cancel(cancel)
                        if written + len(chunk) > info.file_size:
                            raise CVEError('inner_size')
                        target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                        digest.update(chunk)
                        written += len(chunk)
                        if progress:
                            progress({'phase': 'baseline_unwrap', 'source_key': asset.key,
                                      'bytes_done': written, 'bytes_total': info.file_size})
                # Reaching EOF above verifies the outer member CRC in ZipFile.
            if written != info.file_size:
                raise CVEError('inner_incomplete')
            actual = digest.hexdigest()
            if batch['inner_sha256'] is not None and batch['inner_sha256'] != actual:
                raise CVEError('inner_cache_changed')
            self._update(batch['id'], inner_sha256=actual, inner_bytes=written)
        return inner

    def sync(self, asset, cache_root, *, opener=None, cancel=None, progress=None, _local_archive=None):
        """Resume one pinned asset. Baseline first, then same-day cumulative delta.

        Cancellation/network/parse errors retain owned bytes and committed index
        checkpoints. No ZIP member is extracted. No omitted malformed records are
        accepted: their batch remains partial, and old visible data stays intact.
        """
        asset.validate()
        cache = _plain(cache_root, allow_missing=True)
        cache.mkdir(parents=True, exist_ok=True)
        with _sync_lock(self.path.with_name(self.path.name + '.sync.lock')):
            if asset.kind == 'baseline' and asset.sha256 is not None:
                with self._db() as connection:
                    existing = connection.execute('SELECT b.* FROM active a JOIN batches b ON b.id=a.baseline').fetchone()
                    if (existing is not None and existing['baseline_date'] == asset.baseline_date
                            and existing['archive_sha256'] == asset.sha256):
                        return self.status()
            batch = self._batch(asset)
            try:
                if batch['state'] == 'complete':
                    return self.status()
                if shutil.disk_usage(cache).free < max(0, asset.size - batch['downloaded']) + 64 * 1024**2:
                    raise CVEError('download_disk_space')
                if _local_archive is not None:
                    self._stage_local(asset, _local_archive, cache, cancel, progress)
                archive = self._download(asset, batch, cache, opener or _open, cancel, progress)
                archive = self._official_inner(asset, archive, batch, cache, cancel, progress)
                entry_count = _zip_bounds(archive)
                with zipfile.ZipFile(archive) as package:
                    infos = package.infolist()
                    if len(infos) != entry_count:
                        raise CVEError('zip_entry_count')
                    expanded = 0
                    for info in infos:
                        name = PurePosixPath(info.filename)
                        if (len(info.filename) > 512 or '\\' in info.filename or name.is_absolute()
                                or '..' in name.parts or ':' in info.filename or info.flag_bits & 1
                                or (info.external_attr >> 16) & 0o170000 == 0o120000):
                            raise CVEError('zip_member')
                        expanded += info.file_size
                        member_limit = MAX_METADATA_BYTES if info.filename in OFFICIAL_METADATA else MAX_RECORD_BYTES
                        if info.file_size > member_limit or expanded > MAX_EXPANDED_BYTES:
                            raise CVEError('zip_expansion_bounds')
                    if shutil.disk_usage(self.path.parent).free < expanded * 2 + 64 * 1024**2:
                        raise CVEError('index_disk_space')
                    self._update(batch['id'], entries=entry_count, error=None)
                    self._index(package, infos, asset, batch, cancel, progress)
                self._activate(asset, batch['id'])
            except InterruptedError:
                self._update(batch['id'], error='cancelled')
                raise
            except (CVEError, OSError, zipfile.BadZipFile, sqlite3.Error):
                self._update(batch['id'], error='sync_incomplete')
                raise
        return self.status()

    def _index(self, package, infos, asset, batch, cancel, progress):
        connection = self._connect()
        records, skipped = batch['records'], batch['skipped']
        try:
            for position, info in enumerate(infos):
                if position < batch['cursor']:
                    continue
                _check_cancel(cancel)
                filename = PurePosixPath(info.filename).name
                if not info.is_dir() and filename.lower().startswith('cve-') and filename.lower().endswith('.json'):
                    with package.open(info) as handle:
                        raw = handle.read(MAX_RECORD_BYTES + 1)
                    cve_id, state, facets, _data = _record(raw, filename)
                    cursor = connection.execute('INSERT INTO documents(batch_id,cve_id,state,digest,raw_zlib,facets) VALUES(?,?,?,?,?,?)',
                                                (batch['id'], cve_id, state, hashlib.sha256(raw).hexdigest(), zlib.compress(raw), facets))
                    connection.execute('INSERT INTO search_fts(rowid,cve_id,facets) VALUES(?,?,?)', (cursor.lastrowid, cve_id, facets))
                    records += 1
                elif info.filename in OFFICIAL_METADATA and not info.is_dir():
                    with package.open(info) as handle:
                        raw = handle.read(MAX_METADATA_BYTES + 1)
                    root_kind, nodes = _metadata(raw)
                    connection.execute('INSERT INTO ancillary_metadata VALUES(?,?,?,?,?,?)',
                                       (batch['id'], info.filename, hashlib.sha256(raw).hexdigest(),
                                        len(raw), root_kind, nodes))
                    skipped += 1
                else:
                    if not info.is_dir() and filename.lower().endswith('.json'):
                        with package.open(info) as handle:
                            other = _json(handle.read(MAX_RECORD_BYTES + 1), MAX_RECORD_BYTES)
                        if isinstance(other, dict) and other.get('dataType') == 'CVE_RECORD':
                            raise CVEError('unrecognized_cve_filename')
                    skipped += 1
                connection.execute('UPDATE batches SET cursor=?,records=?,skipped=? WHERE id=?',
                                   (position + 1, records, skipped, batch['id']))
                if (position + 1) % CHECKPOINT_RECORDS == 0 or position + 1 == len(infos):
                    connection.commit()
                    if progress:
                        progress({'phase': 'index', 'source_key': asset.key, 'entries_done': position + 1,
                                  'entries_total': len(infos), 'records': records, 'ancillary_entries': skipped})
            if records == 0 and asset.kind == 'baseline':
                raise CVEError('empty_cve_archive')
        finally:
            connection.close()  # Uncommitted records/cursor roll back together.

    def _activate(self, asset, batch_id):
        with self._db() as connection:
            _active = connection.execute('SELECT a.*, b.baseline_date FROM active a JOIN batches b ON b.id=a.baseline').fetchone()
            row = connection.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if row['cursor'] != row['entries'] or (asset.kind == 'baseline' and row['records'] <= 0):
                raise CVEError('index_incomplete')
            if asset.kind == 'delta':
                if _active is None or _active['baseline_date'] != asset.baseline_date:
                    raise CVEError('delta_baseline_required')
                if _active['delta'] is not None:
                    previous = connection.execute('SELECT asset FROM batches WHERE id=?', (_active['delta'],)).fetchone()
                    if json.loads(previous['asset'])['tag'] > asset.tag:
                        raise CVEError('snapshot_rollback')
                baseline, delta = _active['baseline'], batch_id
            else:
                if _active is not None and _active['baseline_date'] > asset.baseline_date:
                    raise CVEError('snapshot_rollback')
                baseline, delta = batch_id, None
            connection.execute("UPDATE batches SET state='complete',completed=?,error=NULL WHERE id=?", (time.time(), batch_id))
            connection.execute('INSERT INTO active VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET baseline=excluded.baseline,delta=excluded.delta,activated=excluded.activated',
                               (baseline, delta, time.time()))

    @staticmethod
    def _visible():
        return '''SELECT d.* FROM documents d JOIN active a ON a.singleton=1
                  WHERE (d.batch_id=a.delta OR (d.batch_id=a.baseline AND NOT EXISTS
                    (SELECT 1 FROM documents newer WHERE newer.batch_id=a.delta AND newer.cve_id=d.cve_id)))'''

    @staticmethod
    def _decode(row):
        inflater = zlib.decompressobj()
        raw = inflater.decompress(row['raw_zlib'], MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES or not inflater.eof or inflater.unused_data:
            raise CVEError('stored_record_bounds')
        if hashlib.sha256(raw).hexdigest() != row['digest']:
            raise CVEError('stored_record_digest')
        return {'cve_id': row['cve_id'], 'state': row['state'], 'record': _json(raw, MAX_RECORD_BYTES),
                'record_sha256': row['digest'], 'source': SOURCE, 'batch_id': row['batch_id']}

    def lookup(self, cve_id):
        if not isinstance(cve_id, str) or not CVE_ID.fullmatch(cve_id):
            raise CVEError('lookup_id')
        with self._db() as connection:
            row = connection.execute('SELECT * FROM (' + self._visible() + ') WHERE cve_id=?', (cve_id,)).fetchone()
            return None if row is None else self._decode(row)

    def search(self, query, *, limit=20):
        if not isinstance(query, str) or not 0 < len(query) <= 512 or not _integer(limit, 1, 100):
            raise CVEError('search_bounds')
        terms = re.findall(r'\w+', query, re.UNICODE)
        if not terms or len(terms) > 32:
            raise CVEError('search_terms')
        expression = ' AND '.join('"' + term.replace('"', '""') + '"' for term in terms)
        with self._db() as connection:
            rows = connection.execute('SELECT visible.* FROM (' + self._visible() + ''') visible
                JOIN search_fts f ON f.rowid=visible.id WHERE search_fts MATCH ?
                ORDER BY bm25(search_fts),visible.cve_id LIMIT ?''', (expression, limit)).fetchall()
            return [{**self._decode(row), 'assessment': 'candidate_only',
                     'warning': 'Keyword overlap does not confirm an affected deployed product or version.'} for row in rows]

    def product_candidates(self, vendor, product, *, version=None, limit=20):
        if not all(isinstance(value, str) and 0 < len(value) <= 128 for value in (vendor, product)):
            raise CVEError('product_bounds')
        if version is not None and (not isinstance(version, str) or len(version) > 128):
            raise CVEError('version_bounds')
        # Version is displayed as an unverified target, never used as a proof of
        # inclusive/exclusive range semantics, package identity or deployment.
        return [{**row, 'target_version': version, 'version_evaluation': 'not_evaluated'}
                for row in self.search(vendor + ' ' + product, limit=limit)]

    def prune_owned_cache(self, cache_root, *, keep_inactive=0, max_batches=1, dry_run=True, cancel=None):
        """Bounded managed PUBLIC-data retention, never invoked implicitly.

        Host autosync may call dry_run=False AFTER successful sync once this
        operation is qualified. Active baseline/delta and ALL partial batches
        are always preserved. Unknown/busy/changed cache files are retained.
        Deleting SQL rows frees reusable pages, not an automatic file-shrinking
        VACUUM. Current readers retain SQLite's normal transaction semantics.
        """
        if (not _integer(keep_inactive, 0, 4) or not _integer(max_batches, 1, 4)
                or type(dry_run) is not bool):
            raise CVEError('retention_bounds')
        cache = _plain(cache_root)
        report = {'dry_run': dry_run, 'removed_batches': [], 'candidates': [], 'unknown_cache_files': 0,
                  'preserved': 'active baseline/delta and all partial batches',
                  'storage': 'freed SQL pages reusable; no automatic VACUUM'}
        with _sync_lock(self.path.with_name(self.path.name + '.sync.lock')):
            with self._db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                rows = connection.execute('SELECT * FROM batches ORDER BY id DESC LIMIT 4097').fetchall()
                if len(rows) > 4096:
                    raise CVEError('retention_batch_inventory_bound')
                active = connection.execute('SELECT baseline,delta FROM active').fetchone()
                protected = set() if active is None else {active['baseline'], active['delta']}
                inactive = [row for row in rows if row['state'] == 'complete' and row['id'] not in protected]
                selected = list(reversed(inactive[keep_inactive:]))[:max_batches]
                known_names = {row['source_key'] + suffix for row in rows for suffix in ('.part', '.cves.zip')}
                with os.scandir(cache) as inventory:
                    for count, entry in enumerate(inventory, 1):
                        if count > 4096:
                            raise CVEError('retention_cache_inventory_bound')
                        if entry.name not in known_names:
                            report['unknown_cache_files'] += 1
                with _held_cache_directories(cache) as api:
                    for row in selected:
                        _check_cancel(cancel)
                        item = {'batch_id': row['id'], 'source_key': row['source_key'], 'files': [], 'state': 'preserved'}
                        report['candidates'].append(item)
                        try:
                            asset = Asset(**_json(row['asset'].encode(), 16384)).validate()
                            if (asset.key != row['source_key'] or row['cursor'] != row['entries']
                                    or (asset.sha256 is not None and asset.sha256 != row['archive_sha256'])):
                                raise CVEError('retention_provenance')
                            expected = [(asset.key + '.part', asset.size, row['archive_sha256'])]
                            if asset.kind == 'baseline' and asset.name.endswith('.zip.zip'):
                                expected.append((asset.key + '.cves.zip', row['inner_bytes'], row['inner_sha256']))
                            if any(not _integer(size, 1, MAX_ARCHIVE_BYTES) or not isinstance(digest, str)
                                   or re.fullmatch('[a-f0-9]{64}', digest) is None for _name, size, digest in expected):
                                raise CVEError('retention_provenance')
                            for name, size, digest in expected:
                                outcome = _remove_verified_cache_file(api, cache / name, size, digest,
                                                                      dry_run=dry_run, cancel=cancel)
                                item['files'].append({'name': name, 'result': outcome})
                                if outcome not in ('missing', 'deleted', 'verified_preview'):
                                    break
                            if len(item['files']) != len(expected) or any(
                                    file['result'] not in ('missing', 'deleted', 'verified_preview') for file in item['files']):
                                continue
                            if dry_run:
                                item['state'] = 'verified_preview'
                                continue
                            connection.execute('DELETE FROM search_fts WHERE rowid IN (SELECT id FROM documents WHERE batch_id=?)', (row['id'],))
                            connection.execute('DELETE FROM documents WHERE batch_id=?', (row['id'],))
                            connection.execute('DELETE FROM ancillary_metadata WHERE batch_id=?', (row['id'],))
                            connection.execute('DELETE FROM batches WHERE id=?', (row['id'],))
                            item['state'] = 'removed'
                            report['removed_batches'].append(row['id'])
                        except InterruptedError:
                            raise
                        except (CVEError, OSError, TypeError, KeyError):
                            item['state'] = 'unrecognized_or_unavailable_preserved'
                report['reusable_pages'] = connection.execute('PRAGMA freelist_count').fetchone()[0]
        return report

    def status(self):
        with self._db() as connection:
            connection.execute('BEGIN')
            active = connection.execute('SELECT * FROM active').fetchone()
            visible = connection.execute('SELECT state,COUNT(*) n FROM (' + self._visible() + ') GROUP BY state').fetchall()
            batches = [dict(row) for row in connection.execute('SELECT id,source_key,asset,kind,baseline_date,state,downloaded,archive_sha256,inner_sha256,inner_bytes,cursor,records,skipped,entries,started,completed,error FROM batches ORDER BY id DESC LIMIT 64')]
            provenance = [] if active is None else [dict(row) for row in connection.execute(
                'SELECT * FROM batches WHERE id IN (?,?)', (active['baseline'], active['delta']))]
            for batch in provenance:
                batch['ancillary_metadata'] = [dict(row) for row in connection.execute(
                    'SELECT path,sha256,bytes,root_kind,nodes FROM ancillary_metadata WHERE batch_id=? ORDER BY path',
                    (batch['id'],))]
            return {'source': SOURCE, 'complete': active is not None,
                    'activated_at': None if active is None else active['activated'],
                    'records': sum(row['n'] for row in visible), 'states': {row['state']: row['n'] for row in visible},
                    'active_provenance': provenance, 'recent_batches': batches,
                    'freshness': 'last_completed_upstream_snapshot_only; not a live vulnerability verdict',
                    'integrity': 'GitHub HTTPS; upstream sha256 when provided; local SHA256 is not a publisher signature',
                    'retention': 'old batches retained; no automatic deletion',
                    'database_bytes': self.path.stat().st_size}
