"""Explicit local-model security review alongside repository tools.

Every attempted file, omission and raw generation is retained. Scanner silence
does not corroborate no-finding. Unverified model/tool decisions enter review.
"""
from __future__ import annotations
import hashlib
import json
import pathlib
from app.backend.pipeline import hybrid_security, llm_client
from app.backend.pipeline.scan import project_code
from training.finetune import serialization as sz
from training.finetune import contracts as ct


def execution():
    from app.backend.pipeline.gateway import client
    try: return (client.status() or {}).get('slots', {}).get('llm') or {}
    except Exception:  # ignored: optional execution telemetry must not prevent repository tool analysis.
        return {'state': 'unavailable'}


def run(project, tool_findings, *, max_files=32, progress=lambda **kw: None):
    if not 1 <= max_files <= 1000: raise ValueError('Local model review supports 1 to 1000 files per scan.')
    resolved = llm_client.resolved()
    if resolved.engine != 'local':
        raise ValueError('Hybrid security review requires a local model selected in Settings. Source is not sent to a remote provider.')
    client, model = llm_client.configured(priority='batch', timeout=180)
    client = client.with_options(max_retries=0)
    identity = {'configured_model': getattr(resolved, 'model_label', None),
                'configured_file': getattr(resolved, 'local_model_file', None), 'request_alias': model}
    if identity['configured_file'] and pathlib.Path(identity['configured_file']).is_file():
        from training.finetune.notebook_downloads import digest
        progress(state='model_identity', label='Hashing the installed local model for review provenance')
        file = pathlib.Path(identity['configured_file'])
        identity.update(sha256=digest(file), bytes=file.stat().st_size)
    candidates, omissions, raw, decisions = [], [], [], []
    for folder_index, folder in enumerate(project['folders']):
        from app.backend.pipeline.scan.sandbox_run import _slug
        prefix = f"{folder_index + 1}-{_slug(pathlib.Path(folder['path']).name)}/" if len(project['folders']) > 1 else ''
        files, skipped, cut = project_code.walk(folder['path'])
        omissions.append({'folder': folder['path'], 'walk_skipped': skipped, 'walk_cut_short': cut})
        for rel, path, language in files:
            if language not in ('JavaScript', 'TypeScript'): continue
            candidates.append((prefix + rel, pathlib.Path(path), language))
    for index, (rel, path, language) in enumerate(candidates):
        if index >= max_files:
            omissions.append({'file': rel, 'reason': 'explicit_model_file_limit'}); continue
        source = None; generation = None; response = None
        progress(state='model_review', label=f'Local model review: {rel}', processed=index, total=min(len(candidates), max_files))
        try:
            if path.is_symlink() or path.stat().st_size > 12000:
                raise ValueError('Source exceeds the model review evidence budget or is a symlink; no code was truncated.')
            source_bytes = path.read_bytes()
            if len(source_bytes) > 12000: raise ValueError('Source grew beyond the evidence budget; no code was truncated.')
            source = source_bytes.decode('utf-8', errors='strict')
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            context = sz.render_context({'path': rel, 'language': language, 'code': source}, [])
            generation = client.chat.completions.create(model=model, temperature=0, max_tokens=1024,
                messages=[{'role': 'system', 'content': sz.CHAT_SYSTEM},
                          {'role': 'user', 'content': ct.TASK_TEXT['discovery'] + '\n\nSupplied source context:\n' + context}]).choices[0].message.content or ''
            response = json.loads(generation)
            problems = ct.validate_response('discovery', response)
            if problems: raise ValueError('Invalid discovery response: ' + '; '.join(problems))
            evidence = response.get('evidence')
            if not isinstance(evidence, list) or any(not isinstance(item, dict) or not isinstance(item.get('quote'), str) or item['quote'] not in source for item in evidence):
                raise ValueError('The model cited unsupported source evidence.')
            if response['status'] == 'finding' and (not response.get('findings') or not evidence):
                raise ValueError('A vulnerability response requires findings and exact source evidence.')
            if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
                raise ValueError('Source changed during the scan; repeat against a stable snapshot.')
            prediction = {'finding': hybrid_security.POS, 'no_finding': hybrid_security.NEG,
                          'insufficient_context': hybrid_security.UNKNOWN}[response['status']]
            assessment = {'status': 'answered', 'prediction': prediction}
        except Exception as error:
            assessment = {'status': 'inconclusive', 'prediction': hybrid_security.UNKNOWN, 'error': str(error)}
        tools = [{'state': 'candidate', 'scope': rel, 'finding_id': finding['id'], 'tool': finding.get('tool')}
                 for finding in tool_findings if finding['file'] == rel]
        combined = hybrid_security.decide(assessment, tools)
        decisions.append({'file': rel, 'language': language, 'model': assessment, 'combined': combined,
                          'scope': 'Supplied file only; dependency and caller behavior require review.', 'tool_candidates': tools})
        raw.append({'file': rel, 'source_sha256': hashlib.sha256(source.encode()).hexdigest() if source is not None else None,
                    'generation': generation, 'response': response, 'model': model, 'model_identity': identity,
                    'execution': execution()})
    return {'schema': 'local-hybrid-scan/1', 'model': model, 'model_identity': identity, 'provider': 'local', 'decisions': decisions,
            'raw_generations': raw, 'omissions': omissions, 'files_total': len(candidates),
            'review_referrals': sum(row['combined']['review_required'] for row in decisions), 'deployment_ready': False,
            'repository_safe': False, 'security_accuracy': 'unknown_without_reviewed_ground_truth'}
