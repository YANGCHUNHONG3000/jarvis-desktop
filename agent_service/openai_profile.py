"""Pinned OpenAI pricing contract; no credentials or network effects here."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re

MODEL = 'gpt-6.1-terra'
API = 'https://api.openai.com/v1'
PRICING = 'https://developers.openai.com/api/docs/models/gpt-6.1-terra.md'
FX = 'https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY'
CONTEXT_LIMIT = 1_050_000
SHORT_INPUT_LIMIT = 272_000
# Conservative CNY/million (also integer micro-CNY/token). Includes base +
# 30m cache-write and regional headroom; never claims precise invoice cost.
INPUT_RATE, OUTPUT_RATE = 30, 60
USD_CNY_CEILING = Decimal('10')


def checked_prices(raw):
    try:
        text = raw.decode('utf-8')
        if f'Model ID: `{MODEL}`' not in text:
            raise ValueError
        section = text.split('### Text tokens\n', 1)[1].split('## Endpoints', 1)[0]
        rows = [line.strip() for line in section.splitlines() if line.strip().startswith('|')]
        if len(rows) != 6 or rows[:2] != ['| Metric | Price | Unit |', '| --- | ---: | --- |']:
            raise ValueError
        matches = []
        for row in rows[2:]:
            match = re.fullmatch(r'\| (Input|Cached input|Cache writes|Output) \| \$(\d+(?:\.\d+)?) \| 1M tokens \|', row)
            if match is None:
                raise ValueError
            matches.append(match.groups())
        rates = {label: Decimal(value) for label, value in matches}
        ceiling = {'Input': Decimal('1'), 'Cached input': Decimal('.05'),
                   'Cache writes': Decimal('1.25'), 'Output': Decimal('5')}
        if len(matches) != 4 or set(rates) != set(ceiling):
            raise ValueError
        if any(not Decimal('0') <= rates[k] <= ceiling[k] for k in rates):
            raise ValueError
        return {'input_usd_per_million': str(rates['Input']),
                'output_usd_per_million': str(rates['Output']),
                'uncached_cny_per_million': str(INPUT_RATE),
                'output_cny_per_million': str(OUTPUT_RATE),
                'usd_cny_ceiling': str(USD_CNY_CEILING),
                'accounting': 'conservative_ceiling_not_invoice'}
    except (ValueError, IndexError, InvalidOperation, UnicodeError):
        raise ValueError('pricing_unrecognized') from None


def checked_fx(raw, *, today=None):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('base') != 'USD'
                or type(value.get('amount')) not in (int, float) or value['amount'] != 1):
            raise ValueError
        day = date.fromisoformat(value['date'])
        today = today or datetime.now(timezone.utc).date()
        raw_rate = value['rates']['CNY']
        if type(raw_rate) not in (int, float):
            raise ValueError
        rate = Decimal(str(raw_rate))
        if not rate.is_finite() or not 0 < rate <= USD_CNY_CEILING or not 0 <= (today - day).days <= 7:
            raise ValueError
        return {'fx_date': day.isoformat(), 'fx_cny_per_usd': str(rate), 'fx_source': FX}
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise ValueError('model_fx_unverified') from None
