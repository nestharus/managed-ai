"""Pinned domain data, typed payload schemas and reference source catalogues.

This is selection and structural validation for the existing native System.
It neither runs domain code nor judges the meaning or acquisition of a source.
"""
from pathlib import Path
import errno
import json
import math
import os
import re
import stat

from core.ownership import packet_id
from core.storage import (
    _OPEN_RO_NOFOLLOW,
    check_ordinary_ancestors,
    encode,
    open_directory,
    safe_relpath,
    sha,
    strict_json_loads,
)
from core import formats as _formats
from core.formats import CATALOGUE_FORMAT, DOMAIN_FORMAT, INFORMATION_FORMAT

# Literal assignments preserved for static AST engine-API verification in controller.code_identity().
ENGINE_API = 'managed-ai-runtime/1'
STORE_SCHEMA = 'managed-ai-store/1'
assert ENGINE_API == _formats.ENGINE_API and STORE_SCHEMA == _formats.STORE_SCHEMA


def _verify_ordinary_dir(path: Path) -> Path:
    return check_ordinary_ancestors(path, expect_dir=True, error_prefix='ordinary selected source required')


def ordinary(path):
    return check_ordinary_ancestors(Path(path), expect_dir=False, error_prefix='ordinary selected source required')


def _read_ordinary_under(root: Path, name: str, *, byte_limit: int | None = None,
                         validated_root: bool = False, root_fd: int | None = None) -> bytes:
    """Read an ordinary non-symlink file relative to root using O_NOFOLLOW dir-FD traversal."""
    rel = Path(name)
    if not name or rel.is_absolute() or '..' in rel.parts or '\\' in name:
        raise ValueError('invalid selected member path: ' + str(name))
    parts = [part for part in rel.parts if part not in ('', '.')]
    if not parts:
        raise ValueError('invalid selected member path: ' + str(name))
    display = root / rel
    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        opened_fds: list[int] = []
        try:
            if root_fd is not None:
                cur = root_fd
            else:
                if not validated_root:
                    _verify_ordinary_dir(root)
                try:
                    cur = open_directory(root)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                        raise ValueError('ordinary selected source required: ' + str(display)) from None
                    raise
                opened_fds.append(cur)
            for segment in parts[:-1]:
                try:
                    nxt = open_directory(segment, dir_fd=cur)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                        raise ValueError('ordinary selected source required: ' + str(display)) from None
                    raise
                opened_fds.append(nxt)
                cur = nxt
            try:
                leaf_fd = os.open(parts[-1], _OPEN_RO_NOFOLLOW, dir_fd=cur)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                    raise ValueError('ordinary selected source required: ' + str(display)) from None
                raise
            try:
                info = os.fstat(leaf_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('ordinary selected source required: ' + str(display))
                if byte_limit is not None and info.st_size > byte_limit:
                    raise ValueError('source body exceeds the requested acquisition bound')
                stream = os.fdopen(leaf_fd, 'rb')
            except BaseException:
                os.close(leaf_fd)
                raise
            with stream:
                if byte_limit is not None:
                    raw = stream.read(byte_limit + 1)
                    if len(raw) > byte_limit:
                        raise ValueError('source body exceeds the requested acquisition bound')
                    return raw
                return stream.read()
        finally:
            for fd in reversed(opened_fds):
                os.close(fd)
    path = source_path(root, name)
    if byte_limit is not None and path.stat().st_size > byte_limit:
        raise ValueError('source body exceeds the requested acquisition bound')
    return path.read_bytes()


def source_path(root, name):
    path = Path(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('invalid selected member path: ' + str(name))
    return ordinary(root / path)


def load_json(path, expected=None):
    raw = ordinary(path).read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError('selected manifest changed: ' + str(path))
    return strict_json_loads(raw, path, require_object=True)


def pin(root, path):
    path = ordinary(Path(path).resolve())
    return {'path': safe_relpath(path, root), 'sha256': sha(path.read_bytes())}


PAYLOAD_TYPES = {
    'object': lambda x: isinstance(x, dict),
    'array': lambda x: isinstance(x, list),
    'string': lambda x: isinstance(x, str),
    'integer': lambda x: type(x) is int,
    'number': lambda x: type(x) in (int, float) and math.isfinite(x),
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


def _local_schema_target(document, reference):
    if not isinstance(reference, str) or not reference.startswith('#/'):
        raise ValueError(f'only local payload-schema references are supported: {reference!r}')
    target = document
    for token in reference[2:].split('/'):
        key = token.replace('~1', '/').replace('~0', '~')
        if not isinstance(target, dict) or key not in target:
            raise ValueError(f'unresolved local payload-schema reference {reference!r}')
        target = target[key]
    if not isinstance(target, (dict, bool)):
        raise ValueError(f'local payload-schema reference {reference!r} does not name a schema')
    return target


def validate_schema(schema):
    """Check every declaration once, including referenced schemas, without a payload."""
    pending = [schema]
    visited = set()
    while pending:
        node = pending.pop()
        if id(node) not in visited:
            visited.add(id(node))
            pending.extend(_schema_children(node, schema))


def _schema_children(schema, document):
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
    for key in ('minLength', 'maxLength', 'minItems', 'maxItems'):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f'schema keyword {key} must be a nonnegative integer')
    if 'minLength' in schema and 'maxLength' in schema and schema['minLength'] > schema['maxLength']:
        raise ValueError('schema minLength exceeds maxLength')
    if 'minItems' in schema and 'maxItems' in schema and schema['minItems'] > schema['maxItems']:
        raise ValueError('schema minItems exceeds maxItems')
    for key in ('minimum', 'maximum'):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError(f'schema keyword {key} must be a finite number')
    if 'minimum' in schema and 'maximum' in schema and schema['minimum'] > schema['maximum']:
        raise ValueError('schema minimum exceeds maximum')
    if 'pattern' in schema:
        if not isinstance(schema['pattern'], str):
            raise ValueError('schema pattern must be a string')
        try:
            re.compile(schema['pattern'])
        except re.error as exc:
            raise ValueError(f'invalid schema pattern regex: {exc}') from None
    if 'required' in schema:
        req = schema['required']
        if not isinstance(req, list) or any(not isinstance(item, str) for item in req):
            raise ValueError('schema required must be a list of strings')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError('schema enum must be a nonempty list')
    for group in ('properties', '$defs', 'definitions'):
        if group in schema:
            if not isinstance(schema[group], dict):
                raise ValueError(f'schema {group} must be an object')
            yield from schema[group].values()
    for group in ('anyOf', 'allOf', 'oneOf'):
        if group in schema:
            if not isinstance(schema[group], list) or not schema[group]:
                raise ValueError(f'schema {group} must be a nonempty list')
            yield from schema[group]
    for key in ('items', 'additionalProperties'):
        if key in schema:
            yield schema[key]
    if '$ref' in schema:
        yield _local_schema_target(document, schema['$ref'])


def validate_payload(value, schema, *, at='content', document=None, _references=frozenset()):
    document = schema if document is None else document
    if schema is True:
        return
    if schema is False:
        raise ValueError(at + ': prohibited by schema')
    if '$ref' in schema:
        target = _local_schema_target(document, schema['$ref'])
        if id(target) in _references:
            raise ValueError(at + ': cyclic schema reference at the same payload location')
        validate_payload(value, target, at=at, document=document, _references=_references | {id(target)})
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
                    validate_payload(value, child, at=at, document=document, _references=_references)
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
    if isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            raise ValueError(at + ': minLength violated')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            raise ValueError(at + ': maxLength violated')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            raise ValueError(at + ': pattern differs')
    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            raise ValueError(at + ': minItems violated')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            raise ValueError(at + ': maxItems violated')
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError(at + ': nonfinite numeric value prohibited')
        if 'minimum' in schema and value < schema['minimum'] or 'maximum' in schema and value > schema['maximum']:
            raise ValueError(at + ': numeric bound violated')


_DOMAIN_ALLOWED_KEYS = frozenset({
    'format',
    'id',
    'version',
    'engine_api',
    'title',
    'summary',
    'description',
    'source_root',
    'catalogue',
    'requires_domains',
    'schemas',
    'entry_sources',
    'tools',
    'relation_vocabulary',
    'protected_record_ids',
    'content_addressed_basis_prefixes',
})
_CATALOGUE_ALLOWED_KEYS = frozenset({
    'format',
    'id',
    'version',
    'title',
    'summary',
    'description',
    'source_root',
    'sources',
})


def _validate_domain_version(value, path):
    if isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z._-]+)?', value):
        return value
    raise ValueError(f'unsupported domain manifest version {value!r}: {path}')


def _validate_domain_manifest(domain, path):
    if not isinstance(domain, dict):
        raise ValueError('domain manifest must be a JSON object: ' + str(path))
    extra = set(domain) - _DOMAIN_ALLOWED_KEYS
    if extra:
        raise ValueError(f'unknown domain manifest fields ({path}): ' + ', '.join(sorted(extra)))
    if domain.get('format') != DOMAIN_FORMAT or domain.get('engine_api') != ENGINE_API:
        raise ValueError('incompatible domain manifest: ' + str(path))
    if not isinstance(domain.get('id'), str) or not domain['id'].strip():
        raise ValueError('domain manifest requires nonempty id: ' + str(path))
    if 'version' in domain:
        _validate_domain_version(domain['version'], path)
    if 'requires_domains' in domain:
        reqs = domain['requires_domains']
        if not isinstance(reqs, list) or any(not isinstance(r, str) or not r.strip() for r in reqs):
            raise ValueError(f"{domain['id']} requires_domains must be a list of nonempty strings")
    if 'schemas' in domain and not isinstance(domain['schemas'], dict):
        raise ValueError(f"{domain['id']} schemas must be an object")
    if 'entry_sources' in domain:
        es = domain['entry_sources']
        if not isinstance(es, list) or any(not isinstance(s, str) or not s.strip() for s in es):
            raise ValueError(f"{domain['id']} entry_sources must be a list of nonempty strings")
    if 'tools' in domain:
        if not isinstance(domain['tools'], list):
            raise ValueError(f"{domain['id']} tools must be a list")
        for tool in domain['tools']:
            if not isinstance(tool, dict) or not isinstance(tool.get('id'), str) or not tool['id'].strip():
                raise ValueError(f"{domain['id']} tool must be an object with nonempty id")
            if 'dependency_source_ids' in tool:
                deps = tool['dependency_source_ids']
                if (not isinstance(deps, list)
                        or any(not isinstance(d, str) or not d.strip() for d in deps)
                        or len(deps) != len(set(deps))):
                    raise ValueError(f"{domain['id']} tool {tool['id']} dependency_source_ids must be unique nonempty strings")
    if 'protected_record_ids' in domain:
        pids = domain['protected_record_ids']
        if not isinstance(pids, list) or len(pids) != len(set(pids)):
            raise ValueError(f"{domain['id']} protected_record_ids must be a list of unique record IDs")
        for pid in pids:
            packet_id(pid)
    if 'content_addressed_basis_prefixes' in domain:
        prefixes = domain['content_addressed_basis_prefixes']
        if not isinstance(prefixes, list) or len(prefixes) != len(set(prefixes)):
            raise ValueError(f"{domain['id']} content_addressed_basis_prefixes must be unique relative prefixes")
        for pref in prefixes:
            if (not isinstance(pref, str) or not pref or pref.startswith('/') or '\\' in pref
                    or ':' in pref or '..' in pref.split('/') or not pref.endswith('/')):
                raise ValueError(f"{domain['id']} invalid content_addressed_basis_prefix: {pref!r}")


def _validate_relation_vocabulary(raw, owner):
    if not isinstance(raw, dict):
        raise ValueError(f'{owner} relation_vocabulary must be an object')
    extra = set(raw) - {'predicates', 'provenance_self_loop_relations'}
    if extra:
        raise ValueError(f'{owner} relation_vocabulary unknown fields: ' + ', '.join(sorted(extra)))
    parsed = {}
    for key in ('predicates', 'provenance_self_loop_relations'):
        values = raw.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f'{owner} relation_vocabulary.{key} must be a list of nonempty strings')
        if len(values) != len(set(values)):
            raise ValueError(f'{owner} relation_vocabulary.{key} contains duplicates')
        parsed[key] = list(values)
    return parsed


class DomainSelection:
    def __init__(self, root, descriptor):
        self.root = Path(root)
        self.schemas, self.entries, self.entry_sources, self.domains = {}, [], [], []
        self.tools = []
        self.bindings = {}
        self.schema_bindings = {}
        self._validated_roots: set[str] = set()
        relation_predicates: set[str] = set()
        provenance_self_loops: set[str] = set()
        protected_record_ids: set[str] = set()
        basis_prefixes: list[str] = []
        declaring_domain = {}
        required_domains = set()
        if descriptor.get('store_schema', STORE_SCHEMA) != STORE_SCHEMA:
            raise ValueError('unsupported store schema; explicit migration required')
        if descriptor.get('engine_api', ENGINE_API) != ENGINE_API:
            raise ValueError('store requires a different engine API')
        for binding in descriptor.get('domains', []):
            path = self.root / binding['path']
            domain = load_json(path, binding['sha256'])
            _validate_domain_manifest(domain, path)
            if domain['id'] in self.domains:
                raise ValueError('duplicate selected domain identity')
            self.domains.append(domain['id'])
            required_domains.update(domain.get('requires_domains', []))
            protected_record_ids.update(domain.get('protected_record_ids', ()))
            for pref in domain.get('content_addressed_basis_prefixes', ()):
                if pref not in basis_prefixes:
                    basis_prefixes.append(pref)
            if 'relation_vocabulary' in domain:
                vocab = _validate_relation_vocabulary(domain['relation_vocabulary'], domain['id'])
                relation_predicates.update(vocab['predicates'])
                provenance_self_loops.update(vocab['provenance_self_loop_relations'])
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
        self.relation_predicates = sorted(relation_predicates)
        self.provenance_self_loop_relations = sorted(provenance_self_loops)
        self.relation_vocabulary = {
            'predicates': self.relation_predicates,
            'provenance_self_loop_relations': self.provenance_self_loop_relations,
        }
        self.protected_record_ids = frozenset(protected_record_ids)
        self.content_addressed_basis_prefixes = tuple(basis_prefixes)

    def validate_catalogue(self, available):
        requested = [*self.entry_sources]
        for tool in self.tools:
            for key in ('source_id', 'entry_source_id', 'contract_source_id'):
                if key in tool:
                    requested.append(tool[key])
            requested.extend(tool.get('dependency_source_ids', ()))
        if any(ident not in available or available[ident].get('availability') == 'sealed' for ident in requested):
            raise ValueError('entry guidance and tool contracts must name available selected sources')

    def add_catalogue(self, path, expected=None, source_root=None):
        catalogue = load_json(path, expected)
        if catalogue.get('format') != CATALOGUE_FORMAT:
            raise ValueError('unsupported reference source catalogue')
        extra = set(catalogue) - _CATALOGUE_ALLOWED_KEYS
        if extra:
            raise ValueError(f'unknown reference source catalogue fields ({path}): ' + ', '.join(sorted(extra)))
        if not isinstance(catalogue.get('sources'), list):
            raise ValueError('reference source catalogue requires a sources list: ' + str(path))
        root = (path.parent / catalogue['source_root']).resolve() if 'source_root' in catalogue else (source_root or path.parent.resolve())
        if str(root) not in self._validated_roots:
            _verify_ordinary_dir(root)
            self._validated_roots.add(str(root))
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
            if isinstance(payload, dict) and payload.get('format') == INFORMATION_FORMAT:
                payload = payload['payload']
            validate_payload(payload, self.schemas[kind])

    def body(self, entry, *, byte_limit=None):
        validated = entry.get('source_root') in self._validated_roots
        return read_selected_body(entry, byte_limit=byte_limit, validated_root=validated)

    def view(self):
        return {'domains': self.domains, 'typed_kinds': sorted(self.schemas),
                'source_count': len(self.entries), 'entry_sources': self.entry_sources,
                'tools': self.tools, 'tool_scope': 'Selected source and contract references; loading does not execute code.'}


def read_selected_body(entry, *, byte_limit=None, validated_root=False, root_fd=None):
    if entry.get('availability') == 'sealed':
        raise ValueError(f"sealed source {entry['id']!r}: body reading and context capture are prohibited; "
                         'inspect availability with a catalogue metadata listing (no identity argument), '
                         'then select an available source')
    raw = _read_ordinary_under(
        Path(entry['source_root']), entry['body_path'],
        byte_limit=byte_limit, validated_root=validated_root, root_fd=root_fd,
    )
    if len(raw) != entry['bytes'] or sha(raw) != entry['sha256']:
        raise ValueError('source reference changed: ' + entry['id'])
    return raw
