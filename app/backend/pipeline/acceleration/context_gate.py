"""A manual generation/export gate tied to this campaign's actual commit ownership."""


def campaign(campaign_id):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo
    with derived.connect() as conn:
        return repo.get(conn, campaign_id)


def review_status(item, steps=None):
    """Draft review readiness is separate from complete source coverage or training admission."""
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo, plan as planmod, targets
    result = {'ready': False, 'reason': 'Finish collection checks before context review.',
              'unresolved_sources': int((item.get('summary') or {}).get('unresolved_items') or 0),
              'drafts_only': True}
    if item.get('state') not in ('done', 'partial'):
        return result
    if item['state'] == 'partial':
        plan = item.get('plan') or {}
        wanted = [(target, stage) for target in item.get('ecosystems') or []
                  for stage in targets.stages_for(planmod.target_stages(plan), target)
                  if stage != 'enrich']
        if not wanted or not any(stage == 'mine' for _, stage in wanted):
            return result
        if steps is None:
            with derived.connect() as conn:
                steps = repo.steps(conn, item['campaign_id'])
        if any((steps.get(key) or {}).get('status') not in ('done', 'skipped') for key in wanted):
            return result
    reason = resource_cleanup_reason(item.get('campaign_id'))
    if reason:
        return {**result, 'reason': reason}
    return {**result, 'ready': True, 'reason': None}


def resource_cleanup_reason(cid):
    import json
    import re
    from app.backend import paths
    if isinstance(cid, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', cid):
        path = paths.interim_dir() / 'acceleration' / 'campaigns' / cid / 'performance.json'
        try:
            execution = json.loads(path.read_text(encoding='utf-8')).get('execution') or {}
        except FileNotFoundError:
            execution = {}  # Historical CPU runs may not have a performance file.
        except (OSError, ValueError, AttributeError):
            execution = {'phase': 'unconfirmed'}
        if not isinstance(execution, dict) or (execution and (
                execution.get('phase') != 'released' or execution.get('cleanup_error'))):
            return 'Wait for confirmed campaign resource cleanup before context review.'
    return None


def require_ready(campaign_id, ids):
    if not campaign_id:
        return
    item = campaign(campaign_id)
    if not item:
        raise ValueError('Unknown campaign')
    status = review_status(item)
    if not status['ready']:
        raise ValueError(status['reason'])
    if ids:
        from app.backend.pipeline.db import legacy
        db = legacy.handle()
        rows = list(db['code_pairs'].find({'_id': {'$in': list(ids)}}, {'commit_key': 1}))
        keys = [row.get('commit_key') for row in rows]
        owned = {row['commit_key'] for row in db['fix_commits'].find(
            {'commit_key': {'$in': keys}, 'queue.campaign_id': campaign_id,
             'queue.status': 'mined'}, {'commit_key': 1})}
        if len(rows) != len(set(ids)) or any(row.get('commit_key') not in owned for row in rows):
            raise ValueError('Selected item was not collected from a successfully mined source in this campaign')


def list_items(campaign_id, *, offset=0, limit=100, filters=None):
    from app.backend.pipeline import context_exchange
    from app.backend.pipeline.automatic_evidence import POLICY
    document = context_exchange.default()._campaign(campaign_id) if campaign_id else {'items': {}}
    from .context_filters import with_status
    from app.backend.pipeline.db.codestore_mongo import campaign_review_page
    rows, total, quality = campaign_review_page(campaign_id, offset=offset, limit=limit, filters=with_status(filters, document), with_quality=True)
    return {'items': [{'pair_id': pid, 'pair_key': key, 'file_path': file, 'language': language,
                       'status': 'pending', **document['items'].get(str(pid), {}), **quality.get(pid, {})}
                      for pid, key, file, language in rows], 'total': total, 'offset': max(0, offset),
            'admission_policy': POLICY, 'context_improvement_optional': True,
            # A metadata-only context rating cannot establish source-bound
            # admission. Actual outcomes are recorded by the immutable build.
            'counts': {'automatically_eligible': None, 'insufficient_context': None,
                       'excluded': None, 'matching_candidates': total},
            'counts_estimated': True, 'readiness_evaluated': False}


def item_ids(campaign_id, *, filters=None, selection_limit=None):
    from app.backend.pipeline import context_exchange
    from app.backend.pipeline.db.codestore_mongo import campaign_review_ids
    from .context_filters import with_status
    document = context_exchange.default()._campaign(campaign_id) if campaign_id else {'items': {}}
    ids, total = campaign_review_ids(campaign_id, filters=with_status(filters, document),
                                    selection_limit=selection_limit, with_total=True)
    return {'ids': ids, 'total': total}
