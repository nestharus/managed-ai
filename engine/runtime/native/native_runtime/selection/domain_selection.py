"""Pinned domain data, typed payload schemas and reference source catalogues.

This is selection and structural validation for the existing native System.
It neither runs domain code nor judges the meaning or acquisition of a source.
"""
from pathlib import Path
import json
import os
import re

from continuation_support.storage import encode, sha

ENGINE_API = 'managed-ai-runtime/1'
STORE_SCHEMA = 'managed-ai-store/1'
DOMAIN_FORMAT = 'managed-ai-domain/1'
CATALOGUE_FORMAT = 'managed-ai-source-catalogue/1'


def ordinary(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('ordinary selected source required: ' + str(path))
    return path


def source_path(root, name):
    path = Path(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('invalid selected member path: ' + str(name))
    return ordinary(root / path)


def load_json(path, expected=None):
    raw = ordinary(path).read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError('selected manifest changed: ' + str(path))
    return json.loads(raw)


def pin(root, path):
    path = ordinary(Path(path).resolve())
    return {'path': os.path.relpath(path, root), 'sha256': sha(path.read_bytes())}


PAYLOAD_TYPES = {
    'object': lambda x: isinstance(x, dict),
    'array': lambda x: isinstance(x, list),
    'string': lambda x: isinstance(x, str),
    'integer': lambda x: type(x) is int,
    'number': lambda x: type(x) in (int, float),
    'boolean': lambda x: type(x) is bool,
    'null': lambda x: x is None,
}


def same_literal(left, right):
    """Compare JSON containers recursively, retaining the dialect's scalar equality."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_literal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_literal(a, b) for a, b in zip(left, right))
    return left == right


def validate_schema(schema):
    """Fail closed for keywords outside the documented structural vocabulary."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError('payload schema must be an object or Boolean')
    allowed = {'$schema', '$id', 'title', 'description', 'type', 'properties', 'required',
               'additionalProperties', 'items', 'enum', 'const', 'minLength', 'maxLength',
               'minItems', 'maxItems', 'minimum', 'maximum', 'pattern', 'anyOf', 'allOf',
               'oneOf', '$defs', 'definitions', '$ref'}
    unknown = set(schema) - allowed
    if unknown:
        raise ValueError('unsupported structural schema keywords: ' + ', '.join(sorted(unknown)))
    if 'type' in schema:
        declared = schema['type']
        names = [declared] if isinstance(declared, str) else declared
        if (not isinstance(names, list) or not names or
                any(not isinstance(name, str) or name not in PAYLOAD_TYPES for name in names)):
            raise ValueError(f'unsupported payload type declaration: {declared!r}')
    for group in ('properties', '$defs', 'definitions'):
        for child in schema.get(group, {}).values():
            validate_schema(child)
    for group in ('anyOf', 'allOf', 'oneOf'):
        for child in schema.get(group, []):
            validate_schema(child)
    for key in ('items', 'additionalProperties'):
        if key in schema:
            validate_schema(schema[key])
    if '$ref' in schema and not schema['$ref'].startswith('#/'):
        raise ValueError('only local payload-schema references are supported')


def validate_payload(value, schema, *, at='content', document=None):
    document = schema if document is None else document
    if schema is True:
        return
    if schema is False:
        raise ValueError(at + ': prohibited by schema')
    if '$ref' in schema:
        target = document
        for key in schema['$ref'][2:].split('/'):
            target = target[key.replace('~1', '/').replace('~0', '~')]
        validate_payload(value, target, at=at, document=document)
    wanted = schema.get('type')
    if wanted is not None:
        wanted = [wanted] if isinstance(wanted, str) else wanted
        if not any(PAYLOAD_TYPES[k](value) for k in wanted):
            raise ValueError(at + ': wrong payload type')
    if 'enum' in schema and not any(same_literal(value, item) for item in schema['enum']):
        raise ValueError(at + ': value is outside schema enum')
    if 'const' in schema and not same_literal(value, schema['const']):
        raise ValueError(at + ': value differs from schema constant')
    for group in ('allOf', 'anyOf', 'oneOf'):
        if group in schema:
            valid = 0
            for child in schema[group]:
                try:
                    validate_payload(value, child, at=at, document=document)
                    valid += 1
                except ValueError:
                    pass
            expected = len(schema[group]) if group == 'allOf' else 1
            if (valid != expected if group != 'anyOf' else valid < 1):
                raise ValueError(at + ': schema alternatives do not match')
    if isinstance(value, dict):
        missing = set(schema.get('required', [])) - set(value)
        if missing:
            raise ValueError(at + ': missing required fields ' + ', '.join(sorted(missing)))
        props = schema.get('properties', {})
        for key, item in value.items():
            validate_payload(item, props.get(key, schema.get('additionalProperties', True)),
                             at=at + '.' + key, document=document)
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_payload(item, schema.get('items', True), at=at + '[' + str(index) + ']', document=document)
    for key, compare in (('minLength', lambda a, b: a >= b), ('maxLength', lambda a, b: a <= b),
                         ('minItems', lambda a, b: a >= b), ('maxItems', lambda a, b: a <= b)):
        if key in schema and isinstance(value, (str, list)) and not compare(len(value), schema[key]):
            raise ValueError(at + ': ' + key + ' violated')
    if isinstance(value, str) and 'pattern' in schema and re.search(schema['pattern'], value) is None:
        raise ValueError(at + ': pattern differs')
    if type(value) in (int, float):
        if 'minimum' in schema and value < schema['minimum'] or 'maximum' in schema and value > schema['maximum']:
            raise ValueError(at + ': numeric bound violated')


class DomainSelection:
    def __init__(self, root, descriptor):
        self.root = Path(root)
        self.schemas, self.entries, self.entry_sources, self.domains = {}, [], [], []
        self.tools = []
        self.bindings = {}
        self.schema_bindings = {}
        declaring_domain = {}
        required_domains = set()
        if descriptor.get('store_schema', STORE_SCHEMA) != STORE_SCHEMA:
            raise ValueError('unsupported store schema; explicit migration required')
        if descriptor.get('engine_api', ENGINE_API) != ENGINE_API:
            raise ValueError('store requires a different engine API')
        for binding in descriptor.get('domains', []):
            path = self.root / binding['path']
            domain = load_json(path, binding['sha256'])
            if domain.get('format') != DOMAIN_FORMAT or domain.get('engine_api') != ENGINE_API:
                raise ValueError('incompatible domain manifest: ' + str(path))
            if domain['id'] in self.domains:
                raise ValueError('duplicate selected domain identity')
            self.domains.append(domain['id'])
            required_domains.update(domain.get('requires_domains', []))
            for kind, schema in domain.get('schemas', {}).items():
                owner = domain['id']
                try:
                    validate_schema(schema)
                except ValueError as exc:
                    raise ValueError(f'{owner} schema for {kind}: {exc}') from exc
                binding = sha(encode(schema))
                if kind in self.schema_bindings:
                    if self.schema_bindings[kind] != binding:
                        raise ValueError(f'conflicting payload schemas for {kind}: {declaring_domain[kind]} and {owner}')
                    continue
                declaring_domain[kind] = owner
                self.schemas[kind] = schema
                self.schema_bindings[kind] = binding
            self.entry_sources.extend(domain.get('entry_sources', []))
            self.tools.extend(dict(tool, domain=domain['id']) for tool in domain.get('tools', []))
            if domain.get('catalogue'):
                cat = domain['catalogue']
                self.add_catalogue(path.parent / cat['path'], cat['sha256'],
                                   source_root=(path.parent / domain.get('source_root', '.')).resolve())
        for binding in descriptor.get('catalogues', []):
            self.add_catalogue(self.root / binding['path'], binding['sha256'])
        ids = [e['id'] for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError('selected source identity collision')
        if required_domains - set(self.domains):
            raise ValueError('required domains are not selected: ' + ', '.join(sorted(required_domains - set(self.domains))))
        if len({tool['id'] for tool in self.tools}) != len(self.tools):
            raise ValueError('selected domain tool identity collision')

    def validate_catalogue(self, available):
        requested = [*self.entry_sources, *(tool[key] for tool in self.tools
                       for key in ('source_id', 'entry_source_id', 'contract_source_id') if key in tool)]
        if any(ident not in available or available[ident].get('availability') == 'sealed' for ident in requested):
            raise ValueError('entry guidance and tool contracts must name available selected sources')

    def add_catalogue(self, path, expected=None, source_root=None):
        catalogue = load_json(path, expected)
        if catalogue.get('format') != CATALOGUE_FORMAT:
            raise ValueError('unsupported reference source catalogue')
        root = (path.parent / catalogue['source_root']).resolve() if 'source_root' in catalogue else (source_root or path.parent.resolve())
        for row in catalogue['sources']:
            name = row['path']
            if not name or Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name:
                raise ValueError('unsafe source catalogue path')
            exposure = row.get('exposure', 'available')
            if exposure not in {'available', 'sealed'}:
                raise ValueError('unknown source exposure policy')
            if not re.fullmatch('[0-9a-f]{64}', row['sha256']) or type(row['bytes']) is not int or row['bytes'] < 0:
                raise ValueError('invalid source identity')
            entry = dict(row, title=row.get('title', row['id']), kind=row.get('kind', 'source'),
                         purpose=row.get('summary', ''), family='selected-reference-source',
                         body_role='complete-current-source-reference', availability=exposure,
                         source_path=row['id'], body_path=name, source_root=str(root))
            self.entries.append(entry)
            self.bindings[row['id']] = root

    def validate_record(self, record, previous=None):
        kind = record.get('kind')
        if previous and previous['kind'] in self.schemas and kind != previous['kind']:
            raise ValueError('typed record reclassification requires an explicit migration')
        if kind in self.schemas:
            payload = record.get('content')
            if isinstance(payload, dict) and payload.get('format') == 'g60-information/1':
                payload = payload['payload']
            validate_payload(payload, self.schemas[kind])

    def body(self, entry, *, byte_limit=None):
        return read_selected_body(entry, byte_limit=byte_limit)

    def view(self):
        return {'domains': self.domains, 'typed_kinds': sorted(self.schemas),
                'source_count': len(self.entries), 'entry_sources': self.entry_sources,
                'tools': self.tools, 'tool_scope': 'Selected source and contract references; loading does not execute code.'}


def read_selected_body(entry, *, byte_limit=None):
    if entry.get('availability') == 'sealed':
        raise ValueError(f"sealed source {entry['id']!r}: body reading and context capture are prohibited; "
                         'inspect availability with a catalogue metadata listing (no identity argument), '
                         'then select an available source')
    path = source_path(Path(entry['source_root']), entry['body_path'])
    if byte_limit is not None and path.stat().st_size > byte_limit:
        raise ValueError('source body exceeds the requested acquisition bound')
    raw = path.read_bytes()
    if len(raw) != entry['bytes'] or sha(raw) != entry['sha256']:
        raise ValueError('source reference changed: ' + entry['id'])
    return raw
