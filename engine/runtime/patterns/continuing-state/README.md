# Continuing state: exact support after a saved base

`support.py` is an optional POSIX/Python standard-library operation. It captures the exact files selected for one declared continuation into a small portable archive. It does not change the agent-system API, invoke an effect, install a live owner, or require a dependency graph for every artifact.

The motivating failure was concrete: a successful `System.put` receipt retained the record ID, revision and intrinsic digest, while the generated canonical file also contained `updated_at`. The caller request and compact receipt did not supply that missing field. An interpretation and its evidence digest could be restored, but the evidence file could not. A new current acceptance can be judged from surviving evidence; it must not be presented as recovery of the absent old occurrence.

The existing `system show ID` already returns the complete record under the owner lock. Compare its ID, revision and intrinsic digest to the actual successful `put` return. A mismatch means an intervening version was observed. Keep the actual full canonical bytes before the workspace is lost. `support.py capture` performs that receipt match against the canonical record file while holding the same cooperative lock and carries the selected supporting bytes as well.

## Correction edition: native obligation filename order

This two-file adapter corrects one portable verification condition in the earlier released helper. The earlier helper SHA-256 was `0d54503ede091d23d65d399fad77b972fe402eb414f31a476a43829a96873725`; the corrected helper SHA-256 is `0d64ab4fbca975aa3f4fbb97e7e40bb68c51545f63ee1fcdb29fd41ec7241fd2`. The existing archive format and command interface are unchanged. Earlier source, products and actual failed capture remain retained at their original identities.

The native agent-system enumerates record files in `<id>.json` filename order and preserves that order in an interpretation's obligation basis. For prefix-related IDs, filename order can differ from bare-ID order: `obligation-inspection-delivery.json` precedes `obligation-inspection.json`, while bare-ID sorting reverses that pair. The earlier portable check incorrectly required bare-ID order and rejected a valid actual multi-obligation interpretation before publishing a support archive.

The corrected check requires unique obligation IDs in the native filename order. It retains all exact record kind, ID, revision, intrinsic digest, structural membership and content checks; it does not sort or rewrite the supplied interpretation. Reversed, duplicate and mismatched record bindings remain refusals. Capture and portable verification now use the same ordering contract. No originating write, interpretation or effect needs to be replayed to make an existing valid interpretation acceptable.

The correction source proof, exact original two-file product, actual native-CLI prefix encounter and scoped positive/refusal results are retained in the separately delivered correction package. This README and `support.py` remain the complete runnable adapter; those development records are not startup dependencies. This correction does not replace an owner's pinned runtime engine, rewrite the historical missing delivery record or claim a successful real owner capture before its actual return.

## What capture checks

The capture specification explicitly chooses the owner, its work/object/lock paths, an immutable interpretation and its byte identity, an earlier base archive, expected generated-record receipts, and optional extra files. The operation reads and hashes the actual complete base ZIP, checks the embedded `snapshot.json` digest and original-root relationship, then captures under the existing owner lock:

1. The declared interpretation and its exact current goal.
2. The current obligation membership and exact revisions/digests recorded by that interpretation. A bounded census of current records checks the membership observation.
3. Every direct evidence body named by the interpretation.
4. Each explicitly expected generated record, matched to its successful write's ID/revision/intrinsic digest.
5. Recursively declared record-basis objects and their current named-path state. A captured basis object can differ from the current file, or the current file can be absent. Those are distinct retained facts.
6. Explicit extra files, exact ordinary file/directory modes, and a final stability recheck before publication.

The archive always carries all bodies in that selected projection, including files also present in the base. Its content-addressed object table deduplicates identical bodies inside the archive. It does not follow `previous_object` history chains, arbitrary links in content, or undeclared semantic dependencies. If a selected body is recognized as an agent-system record, its declared basis is followed. Other ordinary bytes remain opaque, including binary or malformed JSON source material. Known record positions must still contain valid canonical records.

The caller chooses the interpretation; this helper does not search for the latest one or judge whether its account is adequate. Matching goal, obligation and evidence bindings establishes the stated structural relationship. It does not establish all semantic closure. A wider required dependency must be supplied through the appropriate declared record basis, evidence, or extra-file selection.

## Interface

Create a fresh output path outside the owner. All paths inside the spec are ordinary relative POSIX paths except the explicitly absolute base archive. The source owner, lock and files must already exist.

```json
{
  "format": "g60-continuation-capture/1",
  "work_root": "runtime/work",
  "object_root": "runtime/exchange/objects",
  "owner_lock": "runtime/exchange/owner.lock",
  "interpretation": "runtime/work/interpretations/ACTUAL-NAME.json",
  "expected_interpretation": {"bytes": 1234, "sha256": "ACTUAL_FILE_SHA256"},
  "expected_records": [
    {"path": "runtime/work/records/delivery.json", "id": "delivery",
     "revision": 2, "sha256": "ACTUAL_INTRINSIC_RECORD_SHA256"}
  ],
  "extra_files": ["active.json"],
  "base": {
    "archive": "/absolute/path/to/earlier-deployment.zip",
    "bytes": 12345,
    "sha256": "ACTUAL_ZIP_SHA256",
    "snapshot_sha256": "ACTUAL_SNAPSHOT_JSON_SHA256"
  }
}
```

The intrinsic record digest differs from the complete file digest. Copy the expected ID/revision/intrinsic digest from the successful write return, and compute the interpretation byte binding from the actual immutable interpretation. Do not substitute a caller request or manufacture generated fields. The example values above are placeholders.

```sh
python3 -B /absolute/path/support.py capture --root /absolute/owner --spec /absolute/spec.json --output /absolute/new-support.zip
python3 -B /absolute/path/support.py verify --archive /absolute/new-support.zip --sha256 ACTUAL_ZIP_SHA256
python3 -B /absolute/path/support.py recover --archive /absolute/new-support.zip --sha256 ACTUAL_ZIP_SHA256 --to /absolute/fresh-projection
```

Capture returns `status: "captured"`, `archive: {path, bytes, sha256}`, `files`, `file_bytes`, `interpretation: {path, id, bytes, sha256}`, `verified_records`, `base`, `source_stability_checked`, and `scope`. Each verified record contains `path`, `id`, `revision`, its intrinsic `sha256`, and `file: {bytes, sha256, mode}` for the full canonical file.

Verify returns `status: "verified"` with the same archive, file totals, interpretation, record, base and stability fields. It rechecks complete object bytes, structural closure, ordinary modes and membership from the portable archive. The included base verification and source membership/stability are **retained capture observations**. Verify does not re-open the separate base or the former source owner.

Recover returns `status: "recovered-support-projection"`, destination, source root, archive identity, file totals, scope and failure boundary. It validates the complete archive before creating a fresh, previously absent destination. The recovered tree contains only selected support files and their required parent directories. All commands return JSON on stdout on success; unresolved operations return JSON on stderr and exit 2.

The ZIP contains `manifest.json` and `objects/<sha256>` only. The manifest records selected paths, complete byte/hash/mode bindings, directory modes, observed absent paths, base relationships, structural selection, interpretation and generated-record bindings, record-basis edges and the capture census. An external expected ZIP hash is required to verify or recover; a self-consistent archive alone does not authenticate its origin.

## Complete small standalone example

This example needs only Python on POSIX and `support.py`. It creates a small **format fixture**, including a complete tiny base ZIP, then generates a later record and interpretation. It does not need a research deployment or installed agent-system runtime. The separate full-source trial uses the actual unchanged system CLI to exercise that producer boundary.

Choose an absolute fresh example directory outside the product/source tree. Replace the two command arguments; leave the Python body unchanged. The command creates all inputs, calls capture/verify/recover, retains their actual returns, and compares the recovered generated file with the original bytes.

```sh
python3 -B - /absolute/path/to/support.py /absolute/path/to/fresh-example <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, os, stat, subprocess, sys, uuid, zipfile

helper = Path(sys.argv[1]).absolute()
example = Path(sys.argv[2]).absolute()
assert helper.is_file() and not example.exists()
example.mkdir()
owner = example / 'owner'
work = owner / 'runtime/work'
objects = owner / 'runtime/exchange/objects'
for p in (work / 'records', work / 'sources', work / 'interpretations', objects):
    p.mkdir(parents=True, exist_ok=True)
(owner / 'runtime/exchange/owner.lock').touch()

def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()
def sha(body):
    return hashlib.sha256(body).hexdigest()
def now():
    return datetime.now(timezone.utc).isoformat()
def save_record(ident, kind, content, basis):
    value = dict(format='g60-agent-system/0.1', id=ident, kind=kind,
                 active=True, revision=1, previous_object=None, updated_at=now(),
                 content=content, basis=basis, relations=[])
    value['sha256'] = sha(encoded(value))
    (work / 'records' / (ident + '.json')).write_bytes(encoded(value))
    return {k:value[k] for k in ('id', 'revision', 'sha256')}

goal = save_record('goal', 'goal', {'contribution':'Retain the later exact fixture record.'}, [])
source = work / 'sources/input.txt'
source.write_bytes(b'Earlier source in the complete tiny base.\n')
os.chmod(source, 0o640)
files, directories, bodies = {}, {}, {}
for path in sorted(owner.rglob('*')):
    name = path.relative_to(owner).as_posix()
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_dir():
        directories[name] = mode
    else:
        body = path.read_bytes(); key = sha(body)
        files[name] = dict(bytes=len(body), sha256=key, mode=mode)
        bodies[key] = body
snapshot = encoded(dict(format='g60-deployment-snapshot/1', original_root=str(owner),
                        root_mode=stat.S_IMODE(owner.stat().st_mode), files=files, directories=directories))
base_path = example / 'base.zip'
with zipfile.ZipFile(base_path, 'x', zipfile.ZIP_DEFLATED) as z:
    z.writestr('snapshot.json', snapshot)
    for key, body in sorted(bodies.items()):
        z.writestr('objects/' + key, body)
base_bytes = base_path.read_bytes()

# Produce state after the actual fixture base has been saved.
source.write_bytes(b'Basis of the later generated record.\n')
captured_basis = source.read_bytes()
(objects / sha(captured_basis)).write_bytes(captured_basis)
delivery = save_record('delivery', 'landmark', {'produced':'Later fixture contribution'},
                       [dict(path='sources/input.txt', bytes=len(captured_basis), sha256=sha(captured_basis))])
produced = (work / 'records/delivery.json').read_bytes()
source.write_bytes(b'Current named source after that captured basis.\n')
ident = uuid.uuid4().hex
interpretation = dict(format='g60-agent-system/0.1', id=ident, created_at=now(),
                      goal_revision=goal['revision'], goal_sha256=goal['sha256'],
                      disposition='met', account='This fixture uses the exact generated delivery file.',
                      evidence=[dict(path='records/delivery.json', bytes=len(produced), sha256=sha(produced))],
                      obligation_basis=[], condition=None, scope='Caller-authored fixture interpretation.')
interpretation_path = work / 'interpretations' / (ident + '.json')
interpretation_bytes = encoded(interpretation)
interpretation_path.write_bytes(interpretation_bytes)
spec = dict(format='g60-continuation-capture/1', work_root='runtime/work',
            object_root='runtime/exchange/objects', owner_lock='runtime/exchange/owner.lock',
            interpretation=interpretation_path.relative_to(owner).as_posix(),
            expected_interpretation=dict(bytes=len(interpretation_bytes), sha256=sha(interpretation_bytes)),
            expected_records=[dict(path='runtime/work/records/delivery.json', **delivery)],
            extra_files=[], base=dict(archive=str(base_path), bytes=len(base_bytes),
                                     sha256=sha(base_bytes), snapshot_sha256=sha(snapshot)))
spec_path = example / 'spec.json'; spec_path.write_bytes(encoded(spec))

def call(label, *arguments):
    result = subprocess.run([sys.executable, '-B', str(helper), *arguments], capture_output=True)
    (example / (label + '.stdout')).write_bytes(result.stdout)
    (example / (label + '.stderr')).write_bytes(result.stderr)
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout)

archive = example / 'support.zip'
captured = call('capture', 'capture', '--root', str(owner), '--spec', str(spec_path), '--output', str(archive))
pin = captured['archive']['sha256']
call('verify', 'verify', '--archive', str(archive), '--sha256', pin)
projection = example / 'projection'
call('recover', 'recover', '--archive', str(archive), '--sha256', pin, '--to', str(projection))
assert (projection / 'runtime/work/records/delivery.json').read_bytes() == produced
assert (projection / 'runtime/exchange/objects' / sha(captured_basis)).read_bytes() == captured_basis
assert (projection / 'runtime/work/sources/input.txt').read_bytes() != captured_basis
print(json.dumps(dict(status='example-passed', archive=captured['archive'],
                      files=captured['files'], projection=str(projection)), indent=2))
PY
```

## Delivery and later use

For a real continuing owner, retain the actual base export first. Complete the primary/source-kit save operations, acquire the complete resulting record with a receipt match, and make the current interpretation. Capture the selected later state, save the support ZIP separately, then make the final completion receipt name that actual ZIP and its actual save result. A support ZIP does not contain its own later save receipt. This explicit boundary avoids recursive whole-owner exports.

For later use, verify the base by its own restoration contract, and verify/recover this support ZIP into a separate fresh projection. Compare its base pins and declared per-path relationships to the intended base. A compatible composition must explicitly account for replacements, added files and observed absences, preserve owner/child bindings and maintain one current owner. This helper deliberately does not perform that merge, rewrite absolute bindings, replace a live owner, or assert that a partial projection is runnable. A fresh semantic acceptance based on surviving evidence remains a different operation from exact recovery.

## Bounds and limits

Defaults are 256 selected files, 4 MiB per file, 32 MiB of selected logical file bytes, 64 MiB of base snapshot metadata, and a census of 512 current records. The census also has a 32 MiB aggregate read bound. `limits` may explicitly override these values up to the hard ceilings in the helper: 4,096 files/records, 64 MiB per file, 256 MiB aggregate bytes and 128 MiB metadata. Logical bytes count each selected path, even when identical bodies share one archive object. The complete base ZIP is streamed once for its explicitly supplied byte count and digest; the stdlib ZIP reader also reads its central directory. The support ZIP is limited to 300 MiB and its manifest to 8 MiB. These are operational bounds for this adapter, not claims about arbitrary system scale.

Paths reject traversal and symbolic links, and recovery refuses an existing destination. Special files and special mode bits are excluded. The operation preserves ordinary file/directory modes and exact bytes, not ownership, ACLs, timestamps, hardlink identity or a running process. Publication stages a complete ZIP and uses a nonreplacing hard link; a failure after publication can leave that complete archive without a success return. Inspect its actual identity rather than silently relabel or overwrite it.

This is a cooperative POSIX-owner operation. Holding its existing lock coordinates writers that honor that lock; final byte/mode/census checks catch observed drift. It is not a distributed snapshot protocol or protection against a hostile process racing filesystem substitutions. Full archive validation precedes recovery writes, but an unexpected write failure can leave a partial fresh directory. Retain that return, inspect the directory and choose a new destination for a new attempt.

The separate development artifact `test_support.py` retains actual CLI producer returns and tests exact fresh projection, revision drift, missing structural closure, changed membership, base identity, bounded refusal, unsafe archives, observed absence, opaque source bytes, and no overwrite. It and its reports belong with the containing research occurrence's experiment files, outside this two-file adapter. To rerun it from that location, place it beside this helper and pass `--system /absolute/path/to/runtime/agent-system/system.py --out /absolute/fresh-trial`; the small standalone procedure above requires neither those development files nor the agent-system runtime. The original recovery receipts remain separate from this new operation; missing historical evidence has not been fabricated.

Native record IDs contain 1 to 120 ASCII letters, digits, hyphens or underscores. Either punctuation mark can lead an ID. Census membership preserves the actual native filenames under the same grammar for capture, portable verification and recovery.
