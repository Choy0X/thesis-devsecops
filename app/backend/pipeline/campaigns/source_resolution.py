"""Human source-disposition choices, separate from mined evidence and security review."""
from app.backend.pipeline import util
from app.backend.pipeline.campaigns import plan as planmod, repo, runner, targets
from app.backend.pipeline.db import legacy


def _rows(db, campaign_id):
    return list(db.fix_commits.find({'queue.campaign_id': campaign_id,
        '$or': [{'queue.status': 'failed'}, {'queue.status': 'pending', 'queue.resolution.action': 'retry'}]},
        {'commit_key': 1, 'repo.url': 1, 'sha': 1, 'queue': 1}).sort('commit_key', 1))


def counts(campaign_id, db=None, *, ecosystem=None):
    rows = _rows(db if db is not None else legacy.handle(), campaign_id)
    if ecosystem is not None:
        rows = [r for r in rows if not r['queue'].get('ecosystems') or ecosystem in r['queue']['ecosystems']]
    skipped = sum((r.get('queue', {}).get('resolution') or {}).get('action') == 'skip' for r in rows)
    return {'unresolved': len(rows) - skipped, 'skipped': skipped}


def listing(conn, campaign_id, offset=0, limit=100):
    if not repo.get(conn, campaign_id):
        raise LookupError('Unknown campaign')
    rows = _rows(conn.db, campaign_id)
    page = rows[max(0, offset):max(0, offset) + min(500, max(1, limit))]
    return {'items': [{'commit_key': r['commit_key'], 'url': (r.get('repo') or {}).get('url'),
                      'sha': r.get('sha'), 'reason': r['queue'].get('reason'),
                      'attempts': r['queue'].get('attempts', 0), 'retryable': r['queue'].get('retryable', True),
                      'status': r['queue']['status'], 'resolution': r['queue'].get('resolution')}
                     for r in page], 'total': len(rows), **counts(campaign_id, conn.db)}


def resolve(conn, campaign_id, action, commit_keys):
    from app.backend.pipeline.acceleration.context_gate import resource_cleanup_reason
    if action not in ('retry', 'skip') or not commit_keys or len(commit_keys) > 1000 or len(set(commit_keys)) != len(commit_keys):
        raise ValueError('Select 1 to 1000 distinct source commits and retry or skip')
    db = conn.db
    with db.transaction():
        c = repo.get(conn, campaign_id)
        if not c:
            raise LookupError('Unknown campaign')
        if c['legacy'] or c['state'] not in ('partial', 'failed', 'paused', 'interrupted', 'done'):
            raise RuntimeError('Wait until the campaign is stopped before resolving sources')
        from app.backend import jobs
        job = jobs.get(c.get('job_id')) if c.get('job_id') else None
        if job and job.status == 'running':
            raise RuntimeError('Wait until the campaign job has stopped')
        reason = resource_cleanup_reason(campaign_id)
        if reason:
            raise RuntimeError(reason)
        available = {r['commit_key']: r for r in _rows(db, campaign_id)}
        if not set(commit_keys) <= available.keys():
            raise ValueError('Selected sources changed or do not belong to this campaign; refresh and retry')
        selected = [available[k] for k in commit_keys]
        if action == 'retry':
            # Requeue only while owning the affected ecosystem. A paused retry
            # retains this claim until normal admission starts it or it is closed.
            repo.claim(conn, campaign_id, c['ecosystems'])
        now = util.now_z()
        changed = []
        for row in selected:
            q = row['queue']
            if (q.get('resolution') or {}).get('action') == action and (action == 'skip' or q['status'] == 'pending'):
                continue
            receipt = {'action': action, 'at': now, 'campaign_id': campaign_id,
                       'reason': q.get('reason'), 'attempts': q.get('attempts', 0), 'previous_status': q['status']}
            fields = {'queue.resolution': receipt, 'queue.retryable': action == 'retry'}
            fields['queue.status'] = 'pending' if action == 'retry' else 'failed'
            db.fix_commits.update_one({'_id': row['_id']}, {'$set': fields,
                '$push': {'queue.resolution_history': receipt}})
            changed.append(row['commit_key'])
        stages = repo.steps(conn, campaign_id)
        if action == 'retry' and changed:
            # Recovered evidence must pass all enabled downstream checks again.
            # Prior receipts remain in the append-only source-retry event.
            receipts = {f'{t}:{s}': v for (t, s), v in stages.items()
                        if s in ('mine', 'verify', 'resplit', 'dep_versions', 'build')}
            repo.add_event(conn, campaign_id, 'source_retry_previous_checks', {'steps': receipts})
            affected = set().union(*(set(r['queue'].get('ecosystems') or c['ecosystems']) for r in selected))
            for (target, stage), value in stages.items():
                if (target in affected or target == planmod.TARGET_ALL) and stage in ('mine', 'verify', 'resplit', 'dep_versions', 'build'):
                    repo.put_step(conn, campaign_id, target, stage, 'pending', detail={})
            repo.transition(conn, campaign_id, 'paused', expect=c['state'], error=None, control='pause')
        tally = counts(campaign_id, db)
        # Reconcile mining receipts to authoritative dispositions without deleting
        # their failed counts, reasons, or prior outcomes.
        for target in c['ecosystems']:
            s = repo.steps(conn, campaign_id).get((target, 'mine'))
            if s:
                own = [r for r in _rows(db, campaign_id) if not r['queue'].get('ecosystems') or target in r['queue']['ecosystems']]
                excluded = sum((r['queue'].get('resolution') or {}).get('action') == 'skip' for r in own)
                repo.put_step(conn, campaign_id, target, 'mine', s['status'],
                    detail={**s['detail'], 'unresolved_items': len(own) - excluded, 'skipped_sources': excluded},
                    error=s.get('error'), progress=s.get('progress'))
        stages = repo.steps(conn, campaign_id)
        order = [(t, s) for t in c['ecosystems'] for s in targets.stages_for(planmod.target_stages(c['plan']), t)]
        order += [(planmod.TARGET_ALL, s) for s in planmod.global_stages(c['plan'])]
        fresh = runner._summary(stages, order, 0)
        summary = {**c['summary'], **{k: fresh[k] for k in ('steps', 'targets', 'errors')},
                   'unresolved_items': tally['unresolved'], 'skipped_sources': tally['skipped']}
        repo.update(conn, campaign_id, summary=summary)
        if action == 'skip' and tally['unresolved'] == 0 and all(
                (stages.get(k) or {}).get('status') in ('done', 'skipped') for k in order):
            repo.transition(conn, campaign_id, 'done', expect=c['state'], error=None,
                finished_at=c.get('finished_at') or now,
                progress={**c['progress'], 'percent': 100, 'label': 'Collection finished with explicit source exclusions'})
            repo.release(conn, campaign_id)
        if changed:
            repo.add_event(conn, campaign_id, 'sources_retried' if action == 'retry' else 'sources_skipped',
                           {'commit_keys': changed, **tally, 'security_approval': False})
    return {'ok': True, 'changed': len(changed), 'resume_required': action == 'retry',
            'campaign': repo.get(conn, campaign_id), **tally}
