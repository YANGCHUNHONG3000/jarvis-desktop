"""Read-only validation of local model authorization and spending mode."""
import json
from pathlib import Path


PERMISSION = Path(__file__).resolve().parents[1] / 'data' / 'agent_service' / 'model-permission.json'
LIMIT_MICRO = 5_000_000
TIMEZONE = 'America/New_York'


class PermissionPolicyError(ValueError):
    """Missing or malformed local authorization; never grants spending."""


def budget_mode(permit):
    """Return a validated mode, preserving the pre-mode five-yuan contract."""
    if (type(permit) is not dict or permit.get('authorized') is not True
            or permit.get('timezone') != TIMEZONE):
        raise PermissionPolicyError('model_permission_invalid')
    mode = permit.get('budget_mode', 'limited')
    if mode == 'unlimited':
        if (not {'daily_limit_cny', 'rolling_limit_cny'}.issubset(permit)
                or permit['daily_limit_cny'] is not None or permit['rolling_limit_cny'] is not None):
            raise PermissionPolicyError('model_permission_invalid')
    elif mode == 'limited':
        if (permit.get('daily_limit_cny') != '5.00'
                or permit.get('rolling_limit_cny', '5.00') != '5.00'):
            raise PermissionPolicyError('model_permission_invalid')
    else:
        raise PermissionPolicyError('model_permission_invalid')
    return mode


def read_permission(path=None):
    """Read and validate without caching; no credentials are opened."""
    path = PERMISSION if path is None else Path(path)
    class DuplicateKey(ValueError):
        pass
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKey()
            result[key] = value
        return result
    try:
        permit = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    except DuplicateKey:
        raise PermissionPolicyError('model_permission_invalid') from None
    except (OSError, ValueError, UnicodeError):
        raise PermissionPolicyError('model_permission_missing') from None
    budget_mode(permit)
    return permit
