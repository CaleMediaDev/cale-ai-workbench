"""STATIC proposed tests. Not collected/executed until parent review approval."""
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest
from cale_security import security_analysis as security

HOSTS = {}
STAMPS = {}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(security, '_project', lambda folder: tmp_path)
    request = ['a' * 64]
    stamp = {'task_id': 'owned-task', 'owner_generation': 4, 'project_folder': str(tmp_path),
        'instruction_basis': {'brief': 'b'*64, 'rules': None, 'followups': request[0]}}
    def capture():
        return {**stamp, 'instruction_basis': {**stamp['instruction_basis'], 'followups': request[0]}}
    HOSTS[tmp_path] = security.AnalysisHost(tmp_path/'app', tmp_path, capture, lambda: False)
    STAMPS[tmp_path] = stamp
    monkeypatch.setitem(sys.modules, 'task_context', types.SimpleNamespace(request_identity=lambda folder: request[0]))
    return tmp_path, request


def engagement(root, paths):
    return security.create_security_engagement(str(root), 'Inspect these supplied files locally; do not run them.', paths, host=HOSTS[root])['engagement_id']


def test_source_inspection_never_executes_python_and_keeps_raw_hash(project):
    root, _ = project
    data = b'\xef\xbb\xbfraise RuntimeError("MUST NOT RUN")\r\neval(user_text)\r\n'
    (root/'sample.py').write_bytes(data)
    identifier = engagement(root, ['sample.py'])
    report = security.inspect_security_artifacts(str(root), identifier, host=HOSTS[root])
    assert report['files'][0]['sha256'] == hashlib.sha256(data).hexdigest()
    assert report['files'][0]['findings'] == [{'rule': 'dynamic_code', 'line': 2, 'status': 'review_candidate'}]
    assert report['coverage'] == 'explicit_files_only'
    assert (root/'sample.py').read_bytes() == data


@pytest.mark.parametrize('path', ['../outside.py', '/outside.py', 'C:/outside.py', 'file.txt:secret'])
def test_paths_outside_or_alternate_streams_rejected(project, path):
    root, _ = project
    with pytest.raises(ValueError):
        engagement(root, [path])


@pytest.mark.skipif(security.os.name != 'nt', reason='Windows path-anchor semantics')
@pytest.mark.parametrize('path', ['/outside.py', r'\outside.py', r'C:\outside.py', r'C:outside.py', r'\\server\share\outside.py'])
def test_windows_rooted_or_drive_relative_paths_reject_before_any_lookup(project,monkeypatch,path):
    root,_=project
    monkeypatch.setattr(security.Path,'lstat',lambda *args:pytest.fail('Untrusted rooted path must be rejected before lookup'))
    with pytest.raises(ValueError,match='relative artifact path'):
        security._inside(root,path)


def test_changed_request_invalidates_saved_scope(project):
    root, request = project
    (root/'a.py').write_text('x = 1', encoding='utf-8')
    identifier = engagement(root, ['a.py'])
    request[0] = 'b'*64
    with pytest.raises(ValueError, match='stale'):
        security.inspect_security_artifacts(str(root), identifier, host=HOSTS[root])


def test_scope_does_not_authorize_live_site_checks(project):
    root, _ = project
    (root/'a.php').write_text('<?php echo "x";', encoding='utf-8')
    scope = security.create_security_engagement(str(root), 'Inspect my supplied plugin.', ['a.php'], ['https://Example.test'], host=HOSTS[root])
    assert scope['scope']['site_origins'] == ['https://example.test']
    assert scope['scope']['permissions'] == ['local_static_analysis']
    assert scope['scope']['live_checks_authorized'] is False and scope['live_checks_started'] is False
    with pytest.raises(ValueError):
        security._origin('https://name:password@example.test/?token=secret')


def test_npm_lock_exact_dependency_and_python_pin_are_data_only(project):
    root, _ = project
    (root/'package-lock.json').write_text(json.dumps({'packages': {'node_modules/@neutral/lib': {'version': '1.2.3', 'scripts': {'install': 'DO NOT RUN'}}}}))
    (root/'requirements.txt').write_text('neutral-lib==2.4.0\nother>=3\n-r secret.txt\n')
    report = security.inspect_security_artifacts(str(root), engagement(root, ['package-lock.json','requirements.txt']), host=HOSTS[root])
    assert [(x['ecosystem'], x['name'], x['version']) for x in report['components']] == [('npm','@neutral/lib','1.2.3'),('PyPI','neutral-lib','2.4.0')]


def test_wordpress_header_and_php_sink_are_not_confirmed_vulnerabilities(project):
    root, _ = project
    (root/'plugin.php').write_text('<?php\n/*\nPlugin Name: Neutral Widget\nVersion: 1.2.0\n*/\nunserialize($input);\n')
    report = security.inspect_security_artifacts(str(root), engagement(root, ['plugin.php']), host=HOSTS[root])
    assert report['components'][0]['kind'] == 'plugin_header_display_name_not_slug'
    assert report['files'][0]['findings'][0]['status'] == 'review_candidate'


def test_mid_inventory_source_edit_refuses_current_report(project, monkeypatch):
    root, _ = project
    for name in ('early.py','later.py'):
        (root/name).write_text('x = 1')
    identifier = engagement(root, ['early.py','later.py'])
    original = security._read
    def read(path, *args):
        result = original(path, *args)
        if path.name == 'later.py':
            (root/'early.py').write_text('changed = True')
        return result
    monkeypatch.setattr(security, '_read', read)
    with pytest.raises(ValueError, match='changed'):
        security.inspect_security_artifacts(str(root), identifier, host=HOSTS[root])


@pytest.mark.parametrize('raw,kind', [(b'\x7fELF'+b'\0'*60,'ELF'),(b'\xfe\xed\xfa\xcf'+b'\0'*60,'MachO_or_fat_candidate'),(b'MZ'+b'\0'*62,'DOS_or_invalid_PE'),(b'plain text','UTF8_text')])
def test_magic_reports_only_supported_header_candidates(raw, kind):
    assert security.file_kind(raw) == kind


def test_cve_name_and_exact_version_are_candidates_never_proof():
    component = {'ecosystem': 'PyPI', 'name': 'neutral-lib', 'version': '1.2.0'}
    record = {'cveMetadata': {'cveId': 'CVE-2099-12345','state':'PUBLISHED'}, 'containers': {'cna': {'affected': [{'product':'neutral_lib','versions':[{'version':'1.2.0','status':'affected'}]}], 'references':[{'url':'https://example.test/never-download-poc.py'}]}}}
    result = security.map_cve_candidates([component], [record], 'c'*64)
    assert result['candidates'][0]['mapping'] == 'candidate_exact_version'
    assert result['candidates'][0]['confirmed_vulnerability'] is False
    record['cveMetadata']['state'] = 'REJECTED'
    assert security.map_cve_candidates([component], [record], 'c'*64)['candidates'] == []


def test_version_range_is_not_guessed_or_called_safe():
    record = {'cveMetadata': {'cveId':'CVE-2099-12346','state':'PUBLISHED'}, 'containers': {'cna': {'affected':[{'packageName':'neutral','versions':[{'version':'1.0','lessThan':'2.0','status':'affected'}]}]}}}
    result = security.map_cve_candidates([{'ecosystem':'npm','name':'neutral','version':'1.5'}],[record],'d'*64)
    assert result['candidates'][0]['mapping'] == 'candidate_name_only'
    assert result['candidates'][0]['confirmed_vulnerability'] is False


def test_binary_adapter_uses_fixed_java_argv_and_snapshot_not_original(project, monkeypatch):
    root, _ = project
    app = root/'app'; app.mkdir()
    monkeypatch.setattr(security, 'PACKAGE', app)
    for relative in ['Toolbox/'+security.JDK+'/bin/java.exe','Toolbox/'+security.GHIDRA+'/Ghidra/Framework/Utility/lib/Utility.jar', security.EXPORTER]:
        path = app/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'owned test runtime placeholder')
    monkeypatch.setattr(security,'EXPORTER_SHA256',hashlib.sha256(b'owned test runtime placeholder').hexdigest())
    original = root/'client % & input.exe'
    raw = bytearray(128);raw[:2]=b'MZ';raw[60:64]=(64).to_bytes(4,'little');raw[64:68]=b'PE\0\0'
    original.write_bytes(raw)
    identifier = engagement(root,[original.name]); calls=[]
    def run(command,cwd,environment,log,timeout,cancel):
        calls.append(command)
        assert isinstance(command,list) and command[0].endswith('java.exe')
        assert not any(original.name in value for value in command)
        assert 'cmd.exe' not in ' '.join(command) and '-readOnly' in command
        assert 'JAVA_TOOL_OPTIONS' not in environment and 'HTTP_PROXY' not in environment
        report=Path(command[command.index('-postScript')+2])
        report.write_text(json.dumps({'schema':1,'input_sha256':hashlib.sha256(raw).hexdigest(),'analysis_timed_out':False,'function_count':0,'exported_functions':0}))
        return 0
    monkeypatch.setattr(security,'_owned_windows_run',run)
    result=security.analyze_security_binary(str(root),identifier,original.name,host=HOSTS[root])
    assert len(calls)==1 and result['status']=='analyzed'
    assert original.read_bytes()==raw


def test_scope_excludes_unlisted_binary(project, monkeypatch):
    root,_=project
    for name in ('one.exe','two.exe'):
        (root/name).write_bytes(b'not executable')
    identifier=engagement(root,['one.exe'])
    monkeypatch.setattr(security,'_owned_windows_run',lambda *a: pytest.fail('Must not launch'))
    with pytest.raises(ValueError,match='outside'):
        security.analyze_security_binary(str(root),identifier,'two.exe',host=HOSTS[root])


@pytest.mark.parametrize('assignment_error,unknown_members', [(False,False),(True,False),(False,True)])
def test_job_cleanup_uses_exact_unassigned_child_and_real_tuple_contract(tmp_path,monkeypatch,assignment_error,unknown_members):
    import ctypes
    from unittest.mock import Mock
    state={'exit':None}
    child=types.SimpleNamespace(_handle=123,poll=lambda:state['exit'])
    def wait(timeout):
        state['exit']=0 if not assignment_error else 125
        return state['exit']
    child.wait=Mock(side_effect=wait)
    child.kill=Mock(side_effect=lambda:state.update(exit=125))
    job=types.SimpleNamespace(Close=Mock())
    def query(handle,kind):
        if kind==1:
            return {'BasicLimitInformation':{'LimitFlags':0}}
        return {'ProcessIdList':[]} if unknown_members else ()
    fake=types.SimpleNamespace(CreateJobObject=Mock(return_value=job),QueryInformationJobObject=Mock(side_effect=query),
        SetInformationJobObject=Mock(),AssignProcessToJobObject=Mock(side_effect=OSError('owned assign failure') if assignment_error else None),
        TerminateJobObject=Mock(),JobObjectExtendedLimitInformation=1,JobObjectBasicProcessIdList=2,JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000)
    monkeypatch.setitem(sys.modules,'win32job',fake)
    monkeypatch.setattr(security,'os',types.SimpleNamespace(**{**vars(security.os),'name':'nt'}))
    monkeypatch.setattr(security.subprocess,'Popen',Mock(return_value=child))
    monkeypatch.setattr(ctypes,'WinDLL',lambda name:types.SimpleNamespace(NtResumeProcess=Mock(return_value=0)),raising=False)
    if assignment_error:
        with pytest.raises(OSError):
            security._owned_windows_run(['fixed-owned-java'],tmp_path,{},tmp_path/'log',5,lambda:False)
        child.kill.assert_called_once()
        fake.TerminateJobObject.assert_not_called()
    elif unknown_members:
        with pytest.raises(InterruptedError,match='cleanup could not prove'):
            security._owned_windows_run(['fixed-owned-java'],tmp_path,{},tmp_path/'log',5,lambda:False)
    else:
        assert security._owned_windows_run(['fixed-owned-java'],tmp_path,{},tmp_path/'log',5,lambda:False)==0
    job.Close.assert_called_once()


@pytest.mark.parametrize('declaration', [
    'requests[security]==2.32.5',
    'requests [security, tests] == 2.32.5 # optional features',
    'requests[security,extra_feature,extra-feature,extra.feature]==2.32.5',
])
def test_exact_requirement_with_extras_remains_base_package_evidence(project, declaration):
    root, _ = project
    (root/'requirements.txt').write_text(declaration+'\n', encoding='utf-8')
    identifier = engagement(root, ['requirements.txt'])
    report = security.inspect_security_artifacts(str(root), identifier, host=HOSTS[root])
    assert report['components'] == [{'ecosystem': 'PyPI', 'name': 'requests',
        'version': '2.32.5', 'evidence': 'requirements.txt', 'line': 1,
        'kind': 'pinned_declaration'}]
    sbom = security.build_security_sbom(str(root), identifier, host=HOSTS[root])
    assert [(row['name'], row['version']) for row in sbom['components']] == [('requests', '2.32.5')]


@pytest.mark.parametrize('declaration', [
    'requests[security]>=2.32.5', 'requests[security]==2.*',
    'requests[security,]==2.32.5', 'requests[security tests]==2.32.5',
    'requests[security]==2.32.5; python_version < "3.12"',
])
def test_extras_do_not_turn_unresolved_declarations_into_exact_evidence(declaration):
    components, _ = security._dependencies('requirements.txt', declaration.encode())
    assert components == []


def test_sbom_preserves_explicit_declared_evidence(project):
    root,_=project
    (root/'requirements.txt').write_text('neutral==1.2.3\n')
    sbom=security.build_security_sbom(str(root),engagement(root,['requirements.txt']),host=HOSTS[root])
    assert sbom['bomFormat']=='CycloneDX' and sbom['specVersion']=='1.6'
    assert sbom['components'][0]['version']=='1.2.3'
    assert any(p['value']=='pinned_declaration' for p in sbom['components'][0]['properties'])


@pytest.mark.parametrize('change', ['brief','rules','owner','task','unknown'])
def test_host_full_instruction_or_owner_change_invalidates_scope(project,change):
    root,_=project
    (root/'a.py').write_text('x=1')
    identifier=engagement(root,['a.py'])
    if change in ('brief','rules'):
        STAMPS[root]['instruction_basis'][change]='f'*64
    elif change=='owner':
        STAMPS[root]['owner_generation']+=1
    elif change=='task':
        STAMPS[root]['task_id']='other-task'
    else:
        STAMPS[root]['instruction_basis']['brief']=None
    with pytest.raises(ValueError):
        security.inspect_security_artifacts(str(root),identifier,host=HOSTS[root])


def test_host_project_cannot_be_supplied_as_another_registered_task(project):
    root,_=project
    (root/'a.py').write_text('x=1')
    other=root/'other';other.mkdir()
    host=security.AnalysisHost(root/'app',other,HOSTS[root].capture_scope,lambda:False)
    with pytest.raises(ValueError,match='own project'):
        security.create_security_engagement(str(root),'Inspect a.py',['a.py'],host=host)


def security_binary_fixture(project,monkeypatch):
    root,_=project
    app=root/'app';app.mkdir()
    monkeypatch.setattr(security,'PACKAGE',app)
    for relative in ['Toolbox/'+security.JDK+'/bin/java.exe','Toolbox/'+security.GHIDRA+'/Ghidra/Framework/Utility/lib/Utility.jar',security.EXPORTER]:
        path=app/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'owned fake runtime')
    monkeypatch.setattr(security,'EXPORTER_SHA256',hashlib.sha256(b'owned fake runtime').hexdigest())
    raw=bytearray(128);raw[:2]=b'MZ';raw[60:64]=(64).to_bytes(4,'little');raw[64:68]=b'PE\0\0'
    (root/'neutral.pe').write_bytes(raw)
    return root,app,bytes(raw),engagement(root,['neutral.pe'])


def test_ghidra_transient_project_is_nonhidden_app_owned_but_input_report_stay_task_owned(project,monkeypatch):
    root,app,raw,identifier=security_binary_fixture(project,monkeypatch)
    calls=[]
    def run(command,cwd,environment,log,timeout,cancel):
        location=Path(command[command.index('ghidra.app.util.headless.AnalyzeHeadless')+1])
        assert location.is_relative_to(app/'Temp/SecurityAnalysis') and location.is_dir()
        assert all(not part.startswith('.') for part in location.parts)
        assert Path(command[command.index('-import')+1]).is_relative_to(root/'.local-ai/security/jobs')
        report=Path(command[command.index('-postScript')+2])
        assert report.is_relative_to(root/'.local-ai/security/jobs')
        assert '-readOnly' in command and not cancel()
        report.write_text(json.dumps({'schema':1,'input_sha256':hashlib.sha256(raw).hexdigest(),'analysis_timed_out':False}))
        calls.append(location)
        return 0
    monkeypatch.setattr(security,'_owned_windows_run',run)
    result=security.analyze_security_binary(str(root),identifier,'neutral.pe',host=HOSTS[root])
    assert result['status']=='analyzed' and len(calls)==1
    assert (root/'neutral.pe').read_bytes()==raw


@pytest.mark.parametrize('exit_code',[0,1])
def test_missing_ghidra_report_returns_bounded_actual_error_even_when_exit_zero(project,monkeypatch,exit_code):
    root,app,raw,identifier=security_binary_fixture(project,monkeypatch)
    def run(command,cwd,environment,log,timeout,cancel):
        log.write_bytes(b'owned verbose startup\n'*500+b'ERROR owned engine rejected loader\n')
        return exit_code
    monkeypatch.setattr(security,'_owned_windows_run',run)
    result=security.analyze_security_binary(str(root),identifier,'neutral.pe',host=HOSTS[root])
    assert result['status']=='error' and result['exit_code']==exit_code and result['report_path'] is None
    assert len(result['diagnostic_tail'])<=1200 and 'owned engine rejected loader' in result['diagnostic_tail']
    assert result['diagnostic_summary']=='ERROR owned engine rejected loader'
    assert (root/result['log_path']).is_file()
    assert 'report_sha256' not in result and (root/'neutral.pe').read_bytes()==raw


def test_scope_cancel_after_analyzer_never_returns_report_or_error_as_current_evidence(project,monkeypatch):
    root,app,raw,identifier=security_binary_fixture(project,monkeypatch)
    cancelled=[False];previous=HOSTS[root]
    host=security.AnalysisHost(previous.app_dir,previous.project_folder,previous.capture_scope,lambda:cancelled[0])
    def run(command,cwd,environment,log,timeout,cancel):
        log.write_bytes(b'owned cancelled fixture')
        cancelled[0]=True
        assert cancel()
        return 0
    monkeypatch.setattr(security,'_owned_windows_run',run)
    with pytest.raises(InterruptedError):
        security.analyze_security_binary(str(root),identifier,'neutral.pe',host=host)
    assert (root/'neutral.pe').read_bytes()==raw


def test_oversized_serialized_scope_is_refused_before_any_publication(project,monkeypatch):
    root,_=project
    # This control isolates the aggregate byte contract. Existing real path
    # rejection/owned-file tests remain unchanged and are not bypassed there.
    checked=[]
    def validated_only_for_byte_budget(folder,relative):
        checked.append(relative)
        return folder/relative
    monkeypatch.setattr(security,'_inside',validated_only_for_byte_budget)
    paths=[f'{number:03d}_'+('x'*240)+'.py' for number in range(512)]
    with pytest.raises(ValueError,match='100000-byte read limit'):
        security.create_security_engagement(str(root),'Inspect these supplied files.',paths,host=HOSTS[root])
    assert len(checked)==512
    assert not (root/'.local-ai').exists()
    assert list(root.iterdir())==[]


def test_bounded_unicode_CRLF_scope_description_raw_roundtrip_remains_usable(project):
    root,_=project
    (root/'source.py').write_bytes(b'value=1\r\n')
    description='Inspect caf\u00e9 and \U0001f30a source.\r\nKeep this literal second line.\r\n'
    result=security.create_security_engagement(str(root),description,['source.py'],host=HOSTS[root])
    path=root/'.local-ai/security/engagements'/(result['engagement_id']+'.json')
    raw=path.read_bytes()
    assert len(raw)<=100000 and b'caf\xc3\xa9' in raw and '\U0001f30a'.encode('utf-8') in raw
    assert json.loads(raw)['user_request']==description
    assert result['scope']['user_request']==description
    scope,digest=security._engagement(root,result['engagement_id'],HOSTS[root])
    assert scope['user_request']==description and digest==hashlib.sha256(raw).hexdigest()
    assert security.inspect_security_artifacts(str(root),result['engagement_id'],host=HOSTS[root])['files'][0]['sha256']==hashlib.sha256(b'value=1\r\n').hexdigest()
