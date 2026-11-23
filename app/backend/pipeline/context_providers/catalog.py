"""Curated free explanation routes, checked against official sources on 2026-10-27.

Free inference, account eligibility, and reuse of model output are separate facts.
Unknown reuse eligibility fails closed even when inference itself is free.
"""
from copy import deepcopy

VERIFIED_AT = '2026-10-27'


def _entry(identity, name, models, quota, restrictions, urls, reason, *, usable=True, key=True, interval=1, daily=None):
    return dict(id=identity, name=name, models=models, free_verified=True,
        eligibility='requires_confirmation' if usable else 'blocked_unknown_terms',
        quota=quota, restrictions=restrictions, official_urls=urls, verified_at=VERIFIED_AT,
        reason=reason, automation_eligible=usable, requires_key=key, interval_seconds=interval,
        daily_requests=daily)


PROVIDERS = {
    p['id']: p for p in [
        _entry('openrouter_free', 'OpenRouter free variants',
            ['nvidia/nemotron-3-ultra-550b-a55b:free'],
            '50 requests/day without purchased credits; provider capacity also applies.',
            ['Only exact :free model variants with zero prompt/completion price.',
             'Confirm provider/model output reuse terms; no paid routing or fallback.'],
            ['https://openrouter.ai/pricing', 'https://openrouter.ai/docs/api-reference/limits',
             'https://openrouter.ai/docs/features/provider-routing', 'https://openrouter.ai/api/v1/models',
             'https://openrouter.ai/terms'],
            'Free variants only; route enforces max_price zero and disables fallback.', interval=4, daily=50),
        _entry('cloudflare_free', 'Cloudflare Workers AI Free',
            ['@cf/zai-org/glm-4.7-flash', '@cf/google/gemma-4-26b-a4b-it', '@cf/nvidia/nemotron-3-120b-a12b'],
            '10,000 neurons/day on Workers Free; resets at 00:00 UTC.',
            ['Requires Workers Free account, never Workers Paid or AI Gateway prepaid credits.',
             'Some catalog models are paid-only and excluded.', 'Confirm selected model license and output reuse.'],
            ['https://developers.cloudflare.com/workers-ai/platform/pricing/',
             'https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/',
             'https://www.cloudflare.com/service-specific-terms-application-services/'],
            'Allowlisted Workers Free models only; account ID and Free-plan confirmation required.', interval=6),
        _entry('mistral_free', 'Mistral Free', ['mistral-small-latest'],
            'Free account quotas apply to requests/second, tokens/minute and tokens/month.',
            ['Requires Studio Free mode API key, with no paid plan or billing.',
             'Free mode is intended for evaluation/prototyping; confirm this research use and output reuse.',
             'Free requests may be used to improve provider models.'],
            ['https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key',
             'https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them',
             'https://legal.mistral.ai/terms'],
            'Free mode requires no credit card; account/model quotas remain enforced by provider.', interval=6),
        _entry('zai_flash', 'Z.ai Flash', ['glm-4.7-flash', 'glm-4.5-flash'],
            'Free Flash variants; provider concurrency and rate limits apply.',
            ['Only GLM-4.7-Flash and GLM-4.5-Flash; other GLM models may be paid.',
             'Confirm account access and output reuse terms.'],
            ['https://docs.z.ai/guides/overview/pricing', 'https://docs.z.ai/guides/llm/glm-4.7',
             'https://z.ai/legal/terms-of-service'],
            'Only exact named free Flash routes are permitted.', interval=6),
        _entry('pollinations_classic', 'Pollinations Classic', ['openai'],
            'At most one request per IP every 30 seconds; shared IP limits can be stricter.',
            ['Classic POST text.pollinations.ai/openai only, never paid gen or x402.',
             'HTTP 402 with Retry-After is a wait; payment challenges are never accepted.',
             'Confirm public-input and model output reuse terms.'],
            ['https://github.com/pollinations/pollinations/blob/main/APIDOCS.md',
             'https://github.com/pollinations/pollinations', 'https://pollinations.ai/terms'],
            'Classic free route only; no wallet, billing SDK, payment headers or paid fallback.', key=False, interval=30),
        _entry('groq_free', 'Groq Free', ['llama-3.3-70b-versatile'],
            'Free tier has per-model request/token limits.',
            ['Competing-product/model-development restrictions make training-data automation eligibility uncertain.'],
            ['https://console.groq.com/docs/rate-limits', 'https://groq.com/terms-of-use/'],
            'Dataset-output reuse eligibility is unresolved; explanation automation is disabled.', usable=False),
        _entry('cohere_eval', 'Cohere evaluation', ['command-a-03-2025'],
            'Evaluation keys: 1,000 API calls/month, plus per-model request limits.',
            ['Evaluation/research scope and output/model-development restrictions require separate eligibility review.'],
            ['https://docs.cohere.com/docs/rate-limits', 'https://cohere.com/terms-of-use'],
            'Evaluation is free but permission for this dataset-output reuse is unresolved; disabled.', usable=False),
    ]
}


def catalog():
    return {'providers': deepcopy(list(PROVIDERS.values())), 'excluded': [
        {'name': 'GitHub Models', 'reason': 'Retired July 30, 2026; not an available permanent free route.'},
        {'name': 'Hugging Face', 'reason': 'Free model credits removed; not a permanent free allowance.'},
        {'name': 'Cerebras', 'reason': '$5 trial credit is not a permanent free tier.'},
        {'name': 'SiliconFlow global', 'reason': '$1 starter credit is not a permanent free tier.'},
        {'name': 'SambaNova', 'reason': 'Payment method required; excluded from free automation.'},
    ]}


def spec(provider_id):
    if provider_id not in PROVIDERS:
        raise ValueError('Unknown free explanation provider.')
    return PROVIDERS[provider_id]
