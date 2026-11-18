# Observe source admissions for a receiving duty

This optional adapter retains the exact bytes a cooperative local source owner admits. It gives each job an epoch, an origin scope, an explicit account of earlier coverage, and a final selected candidate. A receiving judgment binds one named receiver, one expected record and one requirement to that source observation.

Use it when the actual source owner can put admission through this store. A supplied capable host identifies the owner, the relevant event, the receiving predicate and the expected record. The store preserves those declared boundaries and the observed bytes. The receiver owns its delivery, publication or payment.

## Begin at the source

Keep the SQLite store and input files outside the extracted product. From the compact root, run these commands with an existing working directory and a real candidate file.

```sh
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite init --epoch revision-one --origin original
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite open order-one --expected-epoch revision-one --prior-history empty
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite admit order-one candidate-one --source ../work/original.json
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite close order-one --selected candidate-one
```

`original` means these are the actual original inputs for the commissioned property. `derived` describes inputs produced upstream. The owner supplies that claim. The adapter observes its own inlet. A normalizing gateway before that inlet can erase repeated member names and other evidence. Locate the observer at the responsible original admission event when the receiving duty depends on that event.

Declare `--prior-history empty` only when this owner starts the complete relevant admission window here. Declare `unknown` when earlier relevant admissions lack a complete account. An unknown prefix stays unknown even if every newly admitted candidate is clean. A witnessed bad candidate establishes failure. Original bytes remain recoverable with `original JOB CANDIDATE`.

Every admitted candidate is retained, including malformed JSON. Candidates are immutable under their job and name. Repeating the same bytes is idempotent. Closing with the same final selection is idempotent. The store refuses a new candidate or a replacement selection after closure. Independently opened jobs continue under their own conditions.

## Select the receiver's requirement

```sh
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite judge order-one --expected ../work/expected.json --receiver reviewer --requirement all-originals --equality json-number-value --use current --expected-epoch revision-one
```

| Requirement | Conditions assessed |
|---|---|
| `normalized` | The closed selected input decodes to the expected JSON record. Repeated member names use the last value for this receiver. |
| `selected-original` | The selected original is an object with unique decoded member names throughout and matches the expected record. |
| `all-originals` | The selected-original conditions hold and every candidate throughout a closed, completely covered admission history is an object with unique decoded member names. Earlier candidates can contain different logical content. |

Each call names its receiver and requirement. Sharing one acquisition leaves those judgments separate. The result records the selected original digest, expected-record digest, epoch, origin scope, candidate count, coverage verdict and reasons for refusal. A downstream writer must consume the judgment under its own actual commitment and current source requirements.

The explicit equality contract `json-number-value` compares JSON numbers by numeric value, so `1` and `1.0` are equal. Booleans remain distinct from numbers. Object member order and whitespace carry no equality distinction. Arrays retain their order. The expected record must itself be an object with unique decoded names. This receiving convention resolves the numeric choice for this adapter. A receiver requiring another numeric or byte representation needs its own comparison before use.

## Follow a source change

```sh
python3 -B adapters/source-admission/admission.py --db ../work/source.sqlite advance --epoch revision-two --origin derived --expected revision-one
```

The source owner advances its edition with an expected-current-epoch check. Earlier jobs and originals remain retained. `--use current` refuses those earlier jobs. `--use historical` assesses the explicitly named old epoch. A fresh job on derived inputs can serve a normalized receiving duty while original-input requirements remain unqualified.

SQLite transactions serialize admission, selection and edition changes. A judgment reads one consistent source snapshot. It is an observation of that committed state. A later outside action needs the receiving owner's own current check or reservation. Hashes identify retained local bytes under the cooperative owner's custody. Source authenticity, complete event coverage, access control and authority to impose a receiving rule depend on the actual installation.

## Supplied implementation and scope

The adapter uses Python 3.10 or later with the standard SQLite module. Each input is bounded to 2 MiB. Integer and fractional tokens use decimal parsing, preserving long integers and exponent-form equality without conversion through binary floating point. Python's parser nesting and decimal exponent representation still bound available observations. When either limit prevents inspection, the store retains the original with an unavailable observation and unknown history. It retains all admitted originals and rereads the declared job's candidates to judge history. Its resource cost therefore grows with that job. The host supplies external service credentials, source transport, model interpretation, pricing, reservations and actual receiving effects.

The implementation incorporates source and history distinctions investigated in Studies 71–74. The source regressions exercise malformed inputs, escaped and nested duplicates, missing earlier coverage, immutable cutoff, current and historical epochs, derived-input scope, independent receivers and explicit numeric equality. Packaged receiving tests create actual separately owned files from the receiver decisions and reopen the original bytes through fresh processes.
