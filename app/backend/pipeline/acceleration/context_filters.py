"""Validated selection criteria. These are cost filters, never security approval."""
import re

STATUSES = ('pending', 'queued', 'failed', 'completed', 'needs_context', 'awaiting_external')


def normalize(values=None):
    values = values or {}
    allowed = {'language', 'granularity', 'status', 'search', 'cwe', 'exclude_tests', 'usable_only', 'context_quality', 'draft_quality'}
    if set(values) - allowed:
        raise ValueError('Unsupported context filter')
    out = {k: str(values.get(k) or '').strip() for k in ('language', 'granularity', 'status', 'search', 'cwe', 'context_quality', 'draft_quality')}
    out['language'] = out['language'].lower()
    out['cwe'] = out['cwe'].upper()
    if out['language'] and not re.fullmatch(r'[a-z][a-z0-9_+-]{0,31}', out['language']):
        raise ValueError('Invalid language filter')
    if out['granularity'] not in ('', 'units', 'function', 'hunk', 'file'):
        raise ValueError('Choose functions, hunks, file pairs, or all item types')
    if out['status'] and out['status'] not in STATUSES:
        raise ValueError('Invalid context generation state')
    if out['context_quality'] not in ('', 'needs_work', 'missing', 'weak', 'partial', 'strong'):
        raise ValueError('Invalid source context quality')
    if out['draft_quality'] not in ('', 'needs_work', 'missing', 'weak', 'partial', 'complete'):
        raise ValueError('Invalid model draft quality')
    if out['cwe'] and not re.fullmatch(r'CWE-[0-9]{1,6}', out['cwe']):
        raise ValueError('Enter a CWE identifier such as CWE-89')
    if len(out['search']) > 160 or '\x00' in out['search']:
        raise ValueError('Search must contain at most 160 characters')
    for key in ('exclude_tests', 'usable_only'):
        if key in values and type(values[key]) is not bool:
            raise ValueError('Invalid boolean context filter')
        out[key] = values.get(key, False)
    return out


def with_status(filters, document):
    out = normalize(filters)
    state = out['status']
    if state:
        items = document.get('items') or {}
        if state == 'pending':
            out['_exclude_ids'] = [int(key) for key, value in items.items()
                                   if str(key).isdigit() and value.get('status', 'pending') != 'pending']
        else:
            out['_include_ids'] = [int(key) for key, value in items.items()
                                   if str(key).isdigit() and value.get('status') == state]
    return out
