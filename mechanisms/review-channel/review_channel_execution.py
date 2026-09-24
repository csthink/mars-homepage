"""Shared base: product evidence schema, exact applicability and Definition gate.

No apps import, provider call, authority from Request text, or Registry mutation.
"""
import importlib.util
import json
from pathlib import Path
import review_channel_base as B
import review_channel_contract as C

EXECUTION_KEYS = tuple('port_id mode request_id start_operation_id request_digest execution_ref profile execution_binding reviewer_applicability program_identity input_manifest_sha256 instruction_sha256 reservation_ref physical_observations final_message_sha256 failure_code mapping_id mapping_revision mapping_sha256 author_evidence_refs capability_suggestion'.split())
APPLICABILITY_KEYS = tuple('port_id mode purpose trust_model profile_id profile_version profile_digest native_approval_policy program_identity configuration_revision credential_revision mapping_id mapping_revision mapping_sha256'.split())
PROFILE_FIELDS = tuple('id version digest trustModel purpose programIdentity nativeApprovalPolicy configurationDigest capabilities limitations operations maxContextBytes maxToolCalls maxRunSeconds'.split())
PORT_KEYS = tuple('id mode profile provider model_ref transport effort mapping_id approval_policy applicability capability'.split())
MAPPING_KEYS = tuple('id revision provider model_ref actual_models model_vendor route_vendor allowed_sources'.split())


def require(ok, detail):
    if not ok: raise B.PreflightError('registry-invalid', detail)


def closed(obj, keys):
    return isinstance(obj, dict) and set(obj) == set(keys)


# Exact frozen schema projection; no apps code or external validator dependency.
# The projection identity and every definition are checked against the installed
# Contract by the product suite. Unknown schema keywords fail closed.
_SCHEMA=json.loads(Path(__file__).with_name('review_channel_execution_schema.json').read_text())


def schema_valid(name,value):
    import re
    from datetime import datetime
    def valid(s,v):
        if '$ref' in s:return valid(_SCHEMA['definitions'][s['$ref'].split('/')[-1]],v)
        known={'type','properties','required','additionalProperties','enum','const','allOf','anyOf','if','then','items','minLength','maxLength','minItems','maxItems','minimum','maximum','pattern','format'}
        if set(s)-known:return False
        types={'object':lambda x:isinstance(x,dict),'array':lambda x:isinstance(x,list),'string':lambda x:isinstance(x,str),'integer':lambda x:type(x) is int,'number':lambda x:type(x) in (int,float),'null':lambda x:x is None,'boolean':lambda x:type(x) is bool}
        if 'type' in s and not types[s['type']](v):return False
        if 'enum' in s and v not in s['enum']:return False
        if 'const' in s and v!=s['const']:return False
        if 'allOf' in s and not all(valid(x,v) for x in s['allOf']):return False
        if 'anyOf' in s and not any(valid(x,v) for x in s['anyOf']):return False
        if 'if' in s and valid(s['if'],v) and not valid(s.get('then',{}),v):return False
        if isinstance(v,dict):
            props=s.get('properties',{})
            if not set(s.get('required',[]))<=set(v):return False
            if s.get('additionalProperties') is False and set(v)-set(props):return False
            if not all(valid(props[k],x) for k,x in v.items() if k in props):return False
        if isinstance(v,list):
            if len(v)<s.get('minItems',0) or len(v)>s.get('maxItems',len(v)):return False
            if 'items' in s and not all(valid(s['items'],x) for x in v):return False
        if isinstance(v,str):
            if len(v)<s.get('minLength',0) or len(v)>s.get('maxLength',len(v)):return False
            if 'pattern' in s and re.search(s['pattern'],v) is None:return False
            try:v.encode('utf-8','strict')
            except UnicodeError:return False
            if s.get('format')=='date-time':
                try:
                    if datetime.fromisoformat(v.replace('Z','+00:00')).tzinfo is None:return False
                except ValueError:return False
        if type(v) in (int,float):
            if v<s.get('minimum',v) or v>s.get('maximum',v):return False
        return True
    try:return valid(_SCHEMA['definitions'][name],value)
    except (KeyError,TypeError,ValueError,OverflowError):return False


def program(value):return schema_valid('ProgramIdentity',value)


def profile(value):return schema_valid('ExecutionProfile',value)


def applicability(value):
    return (closed(value,APPLICABILITY_KEYS) and all(B.is_nonempty_str(value[k]) for k in APPLICABILITY_KEYS if k not in ('program_identity','mapping_revision'))
            and value['mode'] in ('embedded','standalone') and value['purpose']=='review' and value['trust_model']=='current-user'
            and value['native_approval_policy'] in ('auto-deny','expected-range-gate','trusted-ui-prompt')
            and program(value['program_identity']) and type(value['mapping_revision']) is int and value['mapping_revision']>0
            and B.hex64(value['profile_digest']) and B.hex64(value['mapping_sha256']))


def execution_problems(value):
    if value is None:return []
    if not closed(value,EXECUTION_KEYS):return ['execution fields']
    errors=[]
    # Never dereference a malformed nested object after reporting its shape.
    if not profile(value['profile']):return ['execution profile']
    if not schema_valid('ReviewerApplicability',value['reviewer_applicability']):return ['execution reviewer applicability']
    if not schema_valid('ExecutionBinding',value['execution_binding']):return ['execution binding']
    def check(ok,what):
        if not ok:errors.append(what)
    check(value['mode'] in ('embedded','standalone') and value['profile']['purpose']=='review','execution mode/purpose')
    check(profile(value['profile']) and program(value['program_identity']),'execution profile')
    check(value['program_identity']==value['profile'].get('programIdentity'),'execution program binding')
    for k in ('port_id','request_id','start_operation_id','reservation_ref','mapping_id'):
        check(B.is_nonempty_str(value[k]),'execution '+k)
    for k in ('request_digest','input_manifest_sha256','instruction_sha256','mapping_sha256'):
        check(B.hex64(value[k]),'execution '+k)
    check(value['execution_ref'] is None or B.is_nonempty_str(value['execution_ref']),'execution ref')
    check(value['final_message_sha256'] is None or B.hex64(value['final_message_sha256']),'execution final hash')
    check(value['failure_code'] is None or value['failure_code'] in C.FAILURE_CODES,'execution failure')
    check(type(value['mapping_revision']) is int and value['mapping_revision']>0,'execution mapping revision')
    check(value['capability_suggestion'] in ('CONFIGURED','CALL_ONLY','REVIEW_ENABLED'),'execution capability')
    check(isinstance(value['author_evidence_refs'],list) and bool(value['author_evidence_refs']) and all(B.is_nonempty_str(x) for x in value['author_evidence_refs']),'execution author evidence')
    ra=value['reviewer_applicability'];eb=value['execution_binding']
    check(closed(ra,('executionPort','purpose','trustModel','profileDigest','configurationRevision','credentialRevision')) and ra.get('executionPort')==value['mode'] and ra.get('purpose')=='review' and ra.get('trustModel')=='current-user' and ra.get('profileDigest')==value['profile'].get('digest'),'execution reviewer applicability')
    check(closed(eb,('profileDigest','agent','model','modelVendor','routeVendor','credentialRef','configurationRevision')) and eb.get('profileDigest')==value['profile'].get('digest') and eb.get('modelVendor') in C.VENDORS and eb.get('configurationRevision')==ra.get('configurationRevision'),'execution binding')
    observations=value['physical_observations']
    check(isinstance(observations,list) and len(observations)<=256,'execution observation limit')
    if isinstance(observations,list):
        for o in observations:
            check(closed(o,('observed_at','physical_execution','evidence_ref','sha256')),'execution observation fields')
            if isinstance(o,dict):
                p=o.get('physical_execution')
                check(schema_valid('PhysicalExecution',p),'execution observation schema')
                check(isinstance(p,dict) and B.sha256_bytes(B.canonical_json(p))==o.get('sha256'),'execution observation digest')
                if schema_valid('PhysicalExecution',p):
                    check(p['executionRef']==value['execution_ref'] and p['requestIdentity']==dict(operationId=value['start_operation_id'],requestDigest=value['request_digest'],profileDigest=value['profile']['digest']),'execution observation identity')
                    check(p['model']==eb['model'] and p['configurationRevision']==eb['configurationRevision'],'execution observation binding')
                    check(o.get('evidence_ref')==p['resultRef'],'execution observation evidence')
    return errors


def verify_ports(obj, repo_root, verify_evidence, profile_checker=None):
    mappings=obj['model_mappings'];ports=obj['execution_ports']
    require(isinstance(mappings,list) and isinstance(ports,list),'product Registry arrays')
    ids=set();byid={}
    for m in mappings:
        require(closed(m,MAPPING_KEYS),'model mapping fields')
        require(B.is_nonempty_str(m['id']) and m['id'] not in ids,'mapping id');ids.add(m['id']);byid[m['id']]=m
        require(type(m['revision']) is int and m['revision']>0,'mapping revision')
        require(m['provider'] in obj['providers'] and m['model_ref'] in obj['providers'][m['provider']]['models'],'mapping provider/model')
        require(isinstance(m['actual_models'],list) and bool(m['actual_models']) and all(B.is_nonempty_str(x) for x in m['actual_models']) and len(set(m['actual_models']))==len(m['actual_models']),'actual model exact set')
        require(m['model_vendor'] in C.VENDORS and m['model_vendor']==obj['providers'][m['provider']]['models'][m['model_ref']]['claimed_vendor'] and (m['route_vendor'] is None or m['route_vendor'] in C.VENDORS),'mapping vendor')
        require(isinstance(m['allowed_sources'],list) and bool(m['allowed_sources']) and all(x in ('protocol-init','protocol-result','adapter-report') for x in m['allowed_sources']),'mapping sources')
    ids=set()
    for p in ports:
        require(closed(p,PORT_KEYS),'execution port fields')
        require(B.is_nonempty_str(p['id']) and p['id'] not in ids,'port id');ids.add(p['id'])
        require(p['mode'] in ('embedded','standalone') and profile(p['profile']),'port mode/profile')
        require(p['mapping_id'] in byid,'port mapping')
        m=byid[p['mapping_id']]
        require(p['provider']==m['provider'] and p['model_ref']==m['model_ref'],'port mapping identity')
        require(p['transport'] in C.TRANSPORTS and p['effort'] in C.EFFORTS and p['approval_policy']==p['profile']['nativeApprovalPolicy'],'port runtime binding')
        a=p['applicability']
        if p['profile']['purpose']=='review':
            require(schema_valid('ReviewerApplicability',a),'review applicability shape')
            require(a['executionPort']==p['mode'] and a['purpose']=='review' and a['trustModel']==p['profile']['trustModel'] and a['profileDigest']==p['profile']['digest'],'review applicability identity')
        else:require(a is None,'non-review applicability')
        cap=p['capability'];require(closed(cap,('status','evidence')) and cap['status'] in ('CONFIGURED','CALL_ONLY','REVIEW_ENABLED'),'product capability')
        if cap['status']=='CONFIGURED':require(cap['evidence'] is None,'configured evidence must be null')
        elif verify_evidence:verify_port_evidence(repo_root,p,m,profile_checker)


def effective_applicability(x):
    p=x['profile'];a=x['reviewer_applicability']
    return dict(port_id=x['port_id'],mode=x['mode'],purpose=p['purpose'],trust_model=p['trustModel'],profile_id=p['id'],profile_version=p['version'],profile_digest=p['digest'],native_approval_policy=p['nativeApprovalPolicy'],program_identity=p['programIdentity'],configuration_revision=a['configurationRevision'],credential_revision=a['credentialRevision'],mapping_id=x['mapping_id'],mapping_revision=x['mapping_revision'],mapping_sha256=x['mapping_sha256'])


def verify_port_evidence(root,port,mapping,profile_checker=None):
    import review_evidence as E
    e=port['capability']['evidence'];E.capability_evidence(e)
    require(e['kind']=='archive','product capability must use archive')
    archive=E.configured(root);require(archive is not None,'product archive is not configured')
    d,files=archive.read(e['ref']);raw=files.get(e['receipt_path'])
    require(raw is not None and E.sha(raw)==e['receipt_sha256'],'product receipt identity')
    r=E.strict(raw);x=r.get('execution')
    require(r.get('receipt_schema')==C.RECEIPT_SCHEMA and x is not None and not receipt_common_problems(r) and profile_checker is not None and not profile_checker(r.get('effective_profile')),'product receipt schema')
    require(r.get('evidence_storage')==dict(kind='archive',repository_id=archive.repository_id,round_key=d['round_key']),'product evidence storage')
    for k,v in [('port_id',port['id']),('mode',port['mode']),('profile',port['profile']),('reviewer_applicability',port['applicability']),('mapping_id',mapping['id']),('mapping_revision',mapping['revision']),('mapping_sha256',B.sha256_bytes(B.canonical_json(mapping)))]:require(x[k]==v,'product evidence '+k)
    ep=r['effective_profile'];binding=x['execution_binding']
    for k,v in [('route_provider',port['provider']),('requested_model',port['model_ref']),('transport',port['transport']),('requested_effort',port['effort']),('effective_effort',port['effort']),('claimed_vendor',mapping['model_vendor'])]:require(ep[k]==v,'product evidence profile '+k)
    require(binding['model']==mapping['model_ref'] and binding['modelVendor']==mapping['model_vendor'] and binding['routeVendor']==mapping['route_vendor'],'product evidence model mapping')
    authors=ep['artifact_author']['model_vendors'];require(ep['artifact_author']['human_only']==(not authors) and binding['modelVendor'] not in authors,'product evidence author eligibility')
    require(r['failure_code'] is None and r['call_path_proof']=='PROVEN' and r['profile_binding']=='SUFFICIENT' and x['failure_code'] is None,'product call evidence')
    require(bool(ep['calls']) and all(c['failure'] is None and c['call_path_proof']=='PROVEN' and c['profile_binding']=='SUFFICIENT' for c in ep['calls']),'product physical call summary')
    observations=x['physical_observations'];require(bool(observations),'product physical observation required')
    physical=observations[-1]['physical_execution'];actual=physical['actualBinding']
    require(physical['state']=='completed' and physical['stopReason'] is None and isinstance(physical['exit'],dict) and physical['exit']['code']==0 and physical['exit']['signal'] is None,'product successful exit')
    require(actual is not None and actual['model'] in mapping['actual_models'] and actual['source'] in mapping['allowed_sources'] and bool(actual['observedModels']) and all(model in mapping['actual_models'] for model in actual['observedModels']),'product actual model mapping')
    events_name='reemit-runtime_events.jsonl' if len(ep['calls'])>1 else 'runtime_events.jsonl'
    require(events_name in files,'product reservation history missing')
    events=B.strict_json_load(files[events_name]);require(isinstance(events,dict) and events.get('reservation_ref')==x['reservation_ref'] and events.get('protected') is True and isinstance(events.get('observation_history'),list),'product reservation history identity')
    for index,window in enumerate(events['observation_history'],1):
        require(closed(window,('evidence','observations')) and schema_valid('EvidenceRef',window['evidence']),'product history fields')
        history=window['observations'];ref=window['evidence'];encoded=B.canonical_json(history)
        require(isinstance(history,list) and len(history)==256 and ref['authority']=='runtime' and ref['scopeRef']==physical['scopeRef'] and ref['mediaType']=='application/json' and ref['revision']==str(index) and ref['bytes']==len(encoded) and ref['digest']==B.sha256_bytes(encoded) and ref['resourceHandle']==physical['resultRef']['resourceHandle'] and ref['objectRef']=='execution-observations:'+B.sha256_bytes(x['request_id'].encode()),'product observation history digest')
        historic=dict(x,physical_observations=history);require(not execution_problems(historic),'product observation history facts')
    message='reemit-reviewer_last_message.md' if len(ep['calls'])>1 else 'reviewer_last_message.md'
    require(message in files and B.sha256_bytes(files[message])==x['final_message_sha256'] and bool(files[message].strip()),'product final bytes')
    envelope_name='reemit-response.json' if len(ep['calls'])>1 else 'response.json'
    envelope=files.get(envelope_name);ref=physical['resultRef']
    require(envelope is not None and ref is not None and len(envelope)==ref['bytes'] and B.sha256_bytes(envelope)==ref['digest'],'product result envelope bytes')
    envelope_doc=B.strict_json_load(envelope)
    require(envelope_doc.get('executionRef')==x['execution_ref'] and envelope_doc.get('operationId')==x['start_operation_id'] and envelope_doc.get('profileId')==x['profile']['id'] and envelope_doc.get('profileDigest')==x['profile']['digest'] and envelope_doc.get('actualBinding')==actual and envelope_doc.get('outcome')=='completed','product result envelope identity')
    answer=envelope_doc.get('evidence',{});raw_answer=answer.get('answer')
    require(isinstance(raw_answer,str) and raw_answer.encode()==files[message] and answer.get('answerBytes')==len(files[message]) and answer.get('answerDigest')==x['final_message_sha256'],'product answer identity')
    status=port['capability']['status'];require(x['capability_suggestion']==status,'product capability suggestion')
    if status=='REVIEW_ENABLED':require(r['verdict_validation']=='VALID' and r['verdict_published'] is True and d['kind']=='round' and r['classification']=='completed_with_valid_verdict','product formal evidence')
    else:require(r['mode']=='probe' and r['classification']=='probe_completed' and d['kind']=='attempt' and files[message].strip()==b'PROBE-OK','product probe evidence')
    decision=e['decision'];raw=E.fixed_file(root,decision['commit'],decision['path'])['content'];result=E.result_block(raw)
    require(result['source']==e['ref'],'product formal decision source')
    E.verify_result(root,raw)


def check_definition(root,request,candidate_bytes=None):
    if request['stage']!='task' or request['task_record'] is None:return
    target='tasks/'+request['task_record']+'/'+request['task_record']+'.md'
    if request['inputs']['candidates'] != [target]:raise B.PreflightError('definition-structure-invalid','Definition path differs from task identity')
    if candidate_bytes is None or target not in candidate_bytes:raise B.PreflightError('definition-structure-invalid','exact sealed candidate bytes required')
    raw=candidate_bytes[target]
    inventory=Path(root)/'records/governance/task-artifact-schema/HarnessPlane_Task_Tree_Alignment_Inventory_v1.json'
    inventory_raw=inventory.read_bytes() if inventory.is_file() else b''
    if B.sha256_bytes(inventory_raw)=='455f89b8242718d59d4c1133a99d190f43fc61106a0aa072b484cd30511f4f39':
        item=json.loads(inventory_raw).get('task_record_summaries',{}).get(request['task_record'],{})
        definition=item.get('definition',{});ident=definition.get('identity',{})
        if item.get('profile')=='legacy-v0' and definition.get('path')==target and ident.get('bytes')==len(raw) and ident.get('sha256')==B.sha256_bytes(raw):return
    path=Path(root)/'mechanisms/artifact-templates/artifact_lint.py'
    if not path.is_file():raise B.PreflightError('definition-structure-invalid','current task validator unavailable')
    spec=importlib.util.spec_from_file_location('hp_task_artifact_lint',path);mod=importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    if mod.lint_file('task',str(Path(root)/target),data=raw)['state']!='PASS':
        raise B.PreflightError('definition-structure-invalid','current task structure validation failed')


def receipt_key_problems(r):
    """Physical member dispatch by receipt_schema (design: schema dispatcher decides the closed set; newer members never
    flow back into older versions). v4 and archive-v1 v3 keep their closed sets. legacy-git v2 keeps its original read
    semantics: the committed population was written across several eras with differing optional members
    (decisions_sha256 / standard_questions_effective / route facts), so v2 is accepted when every frozen reference
    member (C.RECEIPT_REF_FIELDS) is present and no member of a newer physical version is present; no closed set."""
    if not isinstance(r,dict) or r.get('receipt_schema') not in C.RECEIPT_READ_SCHEMAS:return ['Receipt schema']
    v=r['receipt_schema']
    if v==C.RECEIPT_SCHEMA:return [] if set(r)==set(C.RECEIPT_KEYS) else ['Receipt fields']
    if v=='review-channel-receipt/v3':return [] if set(r)==set(C.LEGACY_RECEIPT_KEYS+('evidence_storage',)) else ['Receipt fields']
    newer=tuple(k for k in C.RECEIPT_KEYS if k not in C.LEGACY_RECEIPT_KEYS)
    if any(k not in r for k in C.RECEIPT_REF_FIELDS) or any(k in r for k in newer):return ['Receipt fields']
    return []


def receipt_common_problems(r):
    errors=receipt_key_problems(r)
    if errors:return errors
    v=r['receipt_schema']
    if v!=C.RECEIPT_SCHEMA:
        if r['classification'] not in C.LEGACY_CLASSIFICATIONS or (r.get('failure_code') is not None and r['failure_code'] not in C.LEGACY_FAILURE_CODES):return ['legacy Receipt enum']
        if r.get('effective_profile') is not None and (not isinstance(r['effective_profile'],dict) or r['effective_profile'].get('profile_schema')!='review-channel-effective-profile/v3'):return ['legacy Profile version']
    storage=r.get('evidence_storage')
    if storage is not None and not (closed(storage,('kind','repository_id','round_key')) and storage['kind']=='archive' and all(B.is_nonempty_str(storage[k]) for k in ('repository_id','round_key'))):return ['Receipt storage']
    if v=='review-channel-receipt/v3' and storage is None:return ['archive Receipt storage']
    if v==C.RECEIPT_SCHEMA:
        if r.get('effective_profile') is not None and (not isinstance(r['effective_profile'],dict) or r['effective_profile'].get('profile_schema')!=C.PROFILE_SCHEMA):return ['Profile version']
        errors=execution_problems(r['execution'])
        if r['execution'] is not None and not errors:
            ep=r.get('effective_profile')
            if not isinstance(ep,dict) or ep.get('execution_applicability')!=effective_applicability(r['execution']):errors.append('Receipt/Profile scope')
        return errors
    return []
