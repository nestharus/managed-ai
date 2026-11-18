"""Collective command values. Source identity, bounds and refusal vocabulary."""
import hashlib
import json

BODY_LIMIT = 256 * 1024
CONTEXT_LIMIT = 2 * 1024 * 1024
VIEW_LIMIT = 1024 * 1024
CONTEXT_DETAIL = 'Complete context exceeds 2 MiB; select a narrower receiving contribution'


class Refused(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(condition, code, detail):
    if not condition:
        raise Refused(code, detail)


def name(value, label='identity'):
    require(isinstance(value, str) and 0 < len(value) <= 240 and not any(ord(c) < 32 for c in value),
            'invalid-input', label + ' must be a nonempty bounded string')
    return value


def account(value):
    require(isinstance(value, str) and value.strip() and len(value.encode()) <= 16 * 1024,
            'invalid-input', 'An account must contain text, at most 16 KiB')
    return value


def body(value):
    require(len(encoded(value).encode()) <= BODY_LIMIT, 'body-bound', 'Body exceeds 256 KiB')
    return value


def integer(value, label, lower=0, upper=2**63-1):
    require(type(value) is int and lower <= value <= upper, 'invalid-input', label + ' is out of range')
    return value


def fields(value, allowed):
    require(isinstance(value, dict) and not set(value) - set(allowed), 'invalid-input', 'Unknown request fields')


def pin(record):
    return {k: record[k] for k in ('id', 'revision', 'sha256')}



def owned(record, actor):
    require(record['owner'] == actor, 'wrong-owner', 'The target belongs to another participant')
