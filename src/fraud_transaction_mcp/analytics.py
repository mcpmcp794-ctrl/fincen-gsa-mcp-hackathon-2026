"""Database-independent, read-only credit-card transaction analysis.

Backend contract: .query(sql, params) -> list[dict]. SQL uses ? placeholders.
No evaluation labels are ever read by this module.
"""
import re
from datetime import datetime, timedelta

GROUPS = {
    'merchant': 'merchant_id', 'mcc': 'mcc', 'chip': 'use_chip',
    'state': 'merchant_state', 'user': 'user_id', 'card': 'card_key',
    'month': 'SUBSTR(transaction_time,1,7)'
}
FIELDS = ('transaction_id','user_id','card_id','card_key','transaction_time',
          'amount','use_chip','merchant_id','merchant_city','merchant_state',
          'zip_code','mcc','Errors','source_file','source_row_number')


def _validate_id(value, label, max_len=160):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f'{label} must be a nonempty string <= {max_len} characters')
    return value.strip()


def _limit(value, max_value=100):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= max_value:
        raise ValueError(f'limit must be an integer between 1 and {max_value}')
    return value


def _dates(start=None, end=None):
    for label, value in (('start', start), ('end', end)):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError(f'{label} must be YYYY-MM-DD')
            datetime.strptime(value, '%Y-%m-%d')
    if start and end and start > end:
        raise ValueError('start cannot be after end')
    return start, end


def _date_where(start=None, end=None):
    start, end = _dates(start, end)
    clauses, params = [], []
    if start:
        clauses.append('transaction_time >= ?')
        params.append(start + ' 00:00:00')
    if end:
        clauses.append('transaction_time < ?')
        params.append((datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'))
    return clauses, params


def _table(backend):
    name = getattr(backend, 'transactions', 'transactions')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}', name):
        raise ValueError('Unsafe configured table identifier')
    return name


def search_transactions(backend, user_id=None, card_key=None, merchant_id=None,
                        start=None, end=None, min_amount=None, max_amount=None, limit=30):
    limit = _limit(limit, 200)
    clauses, params = _date_where(start, end)
    for field, value in (('user_id', user_id), ('card_key', card_key), ('merchant_id', merchant_id)):
        if value is not None:
            clauses.append(field + ' = ?')
            params.append(_validate_id(value, field))
    for field, value, op in (('min_amount', min_amount, '>='), ('max_amount', max_amount, '<=')):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e12:
                raise ValueError(f'Invalid {field}')
            clauses.append('amount ' + op + ' ?')
            params.append(float(value))
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise ValueError('min_amount cannot exceed max_amount')
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    rows = backend.query(
        f'SELECT {",".join(FIELDS)} FROM {_table(backend)}{where} '
        'ORDER BY transaction_time DESC, transaction_id LIMIT ?', params + [limit + 1])
    return {'records': rows[:limit], 'returned': min(len(rows), limit),
            'truncated': len(rows) > limit, 'applied_filters': {
                'user_id': user_id, 'card_key': card_key, 'merchant_id': merchant_id,
                'start': start, 'end': end, 'min_amount': min_amount, 'max_amount': max_amount},
            'source': f'Synthetic credit-card transactions from {_table(backend)}',
            'note': 'Amounts are nominal card purchase amounts; refunds or negative values are retained.'}


def summarize_transactions(backend, group_by='merchant', start=None, end=None, limit=20):
    if group_by not in GROUPS:
        raise ValueError('Unsupported group_by')
    limit = _limit(limit, 100)
    clauses, params = _date_where(start, end)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    expr = GROUPS[group_by]
    rows = backend.query(
        f'SELECT {expr} AS group_value, COUNT(*) AS transaction_count, '
        f'SUM(amount) AS net_amount, SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) AS positive_amount, '
        f'SUM(CASE WHEN amount<0 THEN amount ELSE 0 END) AS negative_amount, '
        f'COUNT(DISTINCT card_key) AS unique_cards '
        f'FROM {_table(backend)}{where} GROUP BY {expr} '
        'ORDER BY transaction_count DESC, group_value LIMIT ?', params + [limit])
    return {'group_by': group_by, 'records': rows, 'returned': len(rows),
            'start': start, 'end': end,
            'note': 'Amounts are in dataset nominal units; negative values are retained. No bank fund flows are inferred.'}


def get_cardholder_profile(backend, user_id, card_id=None, limit=30):
    user_id = _validate_id(user_id, 'user_id')
    limit = _limit(limit, 100)
    clauses = ['user_id=?']
    params = [user_id]
    if card_id is not None:
        clauses.append('card_id=?')
        params.append(_validate_id(str(card_id), 'card_id'))
    where = ' AND '.join(clauses)
    t = _table(backend)
    stats = backend.query(
        f'SELECT COUNT(*) AS transaction_count, COUNT(DISTINCT card_key) AS cards, '
        'COUNT(DISTINCT merchant_id) AS merchants, SUM(amount) AS net_spend, '
        'SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) AS purchases, '
        'SUM(CASE WHEN amount<0 THEN amount ELSE 0 END) AS refunds_or_credits, '
        f'MIN(transaction_time) AS first_activity, MAX(transaction_time) AS last_activity FROM {t} WHERE {where}', params)
    merchants = backend.query(
        f'SELECT merchant_id, COUNT(*) AS transaction_count, SUM(amount) AS net_spend '
        f'FROM {t} WHERE {where} GROUP BY merchant_id ORDER BY transaction_count DESC, merchant_id LIMIT ?', params+[limit])
    recent = backend.query(
        f'SELECT {",".join(FIELDS)} FROM {t} WHERE {where} '
        'ORDER BY transaction_time DESC, transaction_id LIMIT ?', params+[limit])
    return {'user_id': user_id, 'card_id': card_id, 'statistics': stats[0] if stats else {},
            'top_merchants': merchants, 'recent_transactions': recent,
            'note': 'Cardholder purchases and refunds; no account balances or interbank transfer provenance.'}


def merchant_relationships(backend, user_id, max_merchants=10, max_related_users=30):
    """Bounded customer-merchant co-occurrence network; NOT a fund-flow trace."""
    user_id = _validate_id(user_id, 'user_id')
    max_merchants = _limit(max_merchants, 20)
    max_related_users = _limit(max_related_users, 50)
    t = _table(backend)
    merchants = backend.query(
        f'SELECT merchant_id, COUNT(*) AS purchase_count, SUM(amount) AS net_spend '
        f'FROM {t} WHERE user_id=? GROUP BY merchant_id '
        'ORDER BY purchase_count DESC, merchant_id LIMIT ?', [user_id,max_merchants])
    if not merchants:
        return {'seed_user': user_id, 'merchants': [], 'related_users': [], 'truncated': False,
                'interpretation': 'No matching user transactions.'}
    names = [str(m['merchant_id']) for m in merchants]
    placeholders = ','.join('?' for _ in names)
    rows = backend.query(
        f'SELECT user_id, COUNT(DISTINCT merchant_id) AS shared_merchants, '
        f'COUNT(*) AS shared_merchant_transactions FROM {t} '
        f'WHERE user_id <> ? AND merchant_id IN ({placeholders}) '
        'GROUP BY user_id ORDER BY shared_merchants DESC, shared_merchant_transactions DESC, user_id LIMIT ?',
        [user_id] + names + [max_related_users + 1])
    return {'seed_user': user_id, 'merchants': merchants, 'related_users': rows[:max_related_users],
            'truncated': len(rows)>max_related_users,
            'interpretation': 'Users share merchants, not necessarily funds, identities, coordination, or fraud.'}


def review_signals(transaction, amount_threshold=500.0):
    """Illustrative deterministic review cues, not a trained fraud model."""
    amount = float(transaction.get('amount') or 0)
    chip = str(transaction.get('use_chip') or '').lower()
    errors = str(transaction.get('Errors') or '').strip()
    flags = []
    if amount >= amount_threshold:
        flags.append({'code':'HIGH_AMOUNT', 'explanation':f'Purchase amount >= {amount_threshold:.2f}'})
    if 'online' in chip:
        flags.append({'code':'ONLINE_PURCHASE', 'explanation':'Purchase channel is online; contextual cue only'})
    if errors and errors.lower() not in ('none','nan'):
        flags.append({'code':'ERROR_RECORDED', 'explanation':'Source record contains an error indicator'})
    return flags


def generate_transaction_report(backend, transaction_id, related_limit=15):
    transaction_id = _validate_id(transaction_id, 'transaction_id')
    related_limit = _limit(related_limit, 50)
    t = _table(backend)
    matches = backend.query(f'SELECT {",".join(FIELDS)} FROM {t} WHERE transaction_id=? LIMIT 1', [transaction_id])
    if not matches:
        return {'transaction_id': transaction_id, 'found': False, 'status':'not_found'}
    tx = matches[0]
    profile = get_cardholder_profile(backend, str(tx['user_id']), str(tx['card_id']), related_limit)
    signals = review_signals(tx)
    return {'transaction_id': transaction_id, 'found': True,
            'transaction': tx, 'card_profile': profile, 'review_signals': signals,
            'source_evidence': {'source_file': tx['source_file'], 'source_row_number': tx['source_row_number']},
            'status':'human_review_required',
            'limitations': [
                'Synthetic credit-card purchases; not bank-to-bank transfers.',
                'Review cues are not fraud determinations.',
                'The fraud ground-truth label is excluded from MCP outputs.',
                'Shared merchants do not establish money movement or identity.'
            ]}
