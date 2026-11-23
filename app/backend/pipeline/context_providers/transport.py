"""Fixed HTTPS routes. No SDK retries, redirects, billing or payment protocols."""
import datetime
import email.utils
import json
import re
from dataclasses import dataclass

from .catalog import spec


class ProviderWait(Exception):
    def __init__(self, delay=60):
        self.delay = max(1, min(float(delay), 31 * 86400))
        super().__init__('Provider quota or capacity requires a wait.')


class PaymentBlocked(Exception):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    headers: dict
    body: bytes


def request_spec(profile, key, packet):
    from app.backend.pipeline.context_exchange import EXPERT_PROMPT, canonical
    pid, model = profile['provider_id'], profile['model']
    if model not in spec(pid)['models'] or not spec(pid)['automation_eligible']:
        raise ValueError('Unknown or ineligible free model route.')
    body = {'model': model, 'temperature': 0, 'max_tokens': 2400,
            'messages': [{'role': 'system', 'content': EXPERT_PROMPT}, {'role': 'user', 'content': canonical(packet)}]}
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    urls = {'openrouter_free': 'https://openrouter.ai/api/v1/chat/completions',
            'mistral_free': 'https://api.mistral.ai/v1/chat/completions',
            'zai_flash': 'https://api.z.ai/api/paas/v4/chat/completions',
            'pollinations_classic': 'https://text.pollinations.ai/openai'}
    if pid == 'cloudflare_free':
        account = profile.get('account_id', '')
        if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
            raise ValueError('Invalid Workers Free account ID.')
        url = 'https://api.cloudflare.com/client/v4/accounts/' + account + '/ai/run/' + model
        body.pop('model')
    else:
        url = urls[pid]
    if pid == 'openrouter_free':
        body['provider'] = {'max_price': {'prompt': 0, 'completion': 0}, 'allow_fallbacks': False}
    if pid == 'pollinations_classic':
        headers.pop('Authorization', None)
    return {'url': url, 'headers': headers, 'json': body}


def _delay(headers, default):
    value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
    try:
        return max(default, float(value))
    except (ValueError, TypeError):
        try:
            return max(default, (email.utils.parsedate_to_datetime(value) - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default


def decode_response(provider_id, response):
    if response.status == 402 and provider_id == 'pollinations_classic':
        raise ProviderWait(_delay(response.headers, 30))
    if response.status in (401, 402, 403) or 300 <= response.status < 400:
        raise PaymentBlocked('Provider access, billing or redirect challenge blocked; profile disabled.')
    if response.status == 429 or response.status >= 500:
        raise ProviderWait(_delay(response.headers, 30 if provider_id == 'pollinations_classic' else 60))
    if response.status != 200:
        raise ValueError('Provider rejected the explanation request (HTTP %d).' % response.status)
    value = json.loads(response.body)
    if provider_id == 'cloudflare_free':
        result = value.get('result') or {}
        text = result.get('response')
    else:
        text = value.get('choices', [{}])[0].get('message', {}).get('content')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Provider returned no explanation JSON.')
    if len(text.encode('utf-8')) > 256 * 1024:
        raise ValueError('Provider explanation exceeds the result budget.')
    return text


def send(profile, key, packet):
    import httpx
    request = request_spec(profile, key, packet)
    # trust_env=False also prevents global chat proxy/credential configuration from leaking in.
    with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
        with client.stream('POST', request['url'], headers=request['headers'], json=request['json']) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 512 * 1024:
                    raise ValueError('Provider response exceeds the result budget.')
                chunks.append(chunk)
            return decode_response(profile['provider_id'], ProviderResponse(response.status_code, dict(response.headers), b''.join(chunks)))
