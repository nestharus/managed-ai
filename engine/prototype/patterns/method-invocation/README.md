# Prepare one source-bound G60 method invocation

`prepare.py` joins an existing Cognition runtime, selected literal configuration, complete operating knowledge and a task into an inspectable one-step invocation. The main method reaches the commissioned brief as complete text. Current knowledge comes from an explicitly pinned library edition and becomes a purpose-specific invocation view; this does not recreate a separately maintained agent manual.

The product prepares files only. The supported source boundary remains Cognition's inspected configurable Python runner and **Claude method route** at commit `3525b5fdf39756ac7c109af0058bcaafec1a4d00`. The unchanged `source.json` binds ten delivery source bodies. The caller supplies a complete working runtime, effective configuration, an existing router class actually mapped to Claude and an explicit effective Claude profile. Preparation checks the bound source files; it does not query the router, import the runtime, launch a provider or establish installed adoption. Retained source slices alone are not a complete runtime installation.

## Prepare from current knowledge

Choose the accepted knowledge edition for the actual use. Read its exact catalogue SHA-256 with the knowledge component's `verify` command. Supply that observed value explicitly below; `ACTUAL_CATALOGUE_SHA256` is a placeholder, not a valid digest.

```bash
python3 -B /path/to/G60/prototype/knowledge-system/knowledge.py verify
python3 -B prepare.py \
  --runtime-root /path/to/complete/applicable-cognition-runtime \
  --paths /path/to/selected/paths.conf \
  --knowledge-library /path/to/G60/prototype/knowledge-system/library \
  --knowledge-sha256 ACTUAL_CATALOGUE_SHA256 \
  --knowledge-body research/current/method.md \
  --knowledge-body prototype/knowledge-system/RESEARCH.md \
  --task /path/to/project/task.md \
  --support /path/to/project/source.md \
  --output /path/to/project/result.md \
  --class engineer --profile claude2 \
  --work-root /path/to/project \
  --invocation-cwd /path/to/actual/caller-directory \
  --invocation /path/to/new-invocation
```

The four required roles for this particular consumer are the complete `agent/AGENTS.md` operating body and the current knowledge system's `README.md`, `CONTRACT.md` and `MIGRATION.md`. The main method remains byte-identical to its selected full source and is inlined. The other roles supply current entrance, acquisition and migration interpretation as explicit full inputs. The example also selects the canonical research method and research participation because its use includes research; those extra bodies are not prerequisites for every application.

Repeat `--knowledge-body ID` to select other complete bodies when the receiving task needs them. Duplicate selections are consolidated. This consumer selects named complete bodies directly; it does not import module authority, treat all ancestors as dependencies or automatically acquire every reference. The knowledge system's discovery and context tools can help the caller choose further bodies.

The preparer reads only catalogue and object data. It executes no code from the selected library. It checks the exact catalogue pin, supported `knowledge-library/1` format, unique logical IDs, selected complete body hashes/lengths and source identities. Catalogue and bodies are bounded at 4 MiB and 2 MiB; selected bodies total at most 8 MiB. Knowledge paths must use ordinary files without symlink components. A missing required body, stale catalogue pin, changed object, unsafe object path or a selected compatibility stub refuses preparation before an invocation directory is created. The selected edition's actual currentness and adequacy remain a caller/receiver judgment.

## Retain a valid historical complete-guide route

For a purpose that actually selects an earlier complete guide, replace the knowledge options with `--guide-root /path/to/accepted-complete-guide`. That mode retains the earlier nine-file copy contract: `AGENTS.md`, `README.md`, `methods.md`, `guide.md`, `attention.md`, `worked-discovery.md`, `worked-examples.md`, `software-patterns.md` and `shared-systems.md`. Their complete bytes and original source bindings are preserved.

The current `G60/agent` directory contains retired compatibility entrances and is explicitly refused. A historical accepted full guide does not become invalid because its current path has been retired. The two input modes are mutually exclusive; a historical guide does not consume knowledge selectors.

## Inspect the actual prepared relationship

| File | Participation |
|---|---|
| `commission.md` | Complete selected main method, current knowledge-reading instruction when applicable, exact task/support locations, work root and required outputs |
| `inputs/` | Exact task and supplied support bytes |
| `method/AGENTS.md` | Complete selected operating method, also present in the commission |
| `method/README.md` | Generated current-selection reading map, or the exact earlier guide's map in historical mode |
| `method/knowledge/objects/` | Complete selected `.txt` bodies, including any source code as data |
| `method/knowledge/selection.json` | Role assignments, complete selected source entries, exact pins, original object locations and copied paths |
| `method/knowledge/source-catalogue.json` | Exact wider catalogue used for acquisition and its identity; unselected objects are not supplied |
| `manifest.json` | One whole-purpose `work` step, explicit class/profile, method hashes and actual complete input routes |
| `paths.conf` | Literal selected configuration with `METHOD_ROOT` and `SEAT_WD` bound to this generated method view |
| `invocation.json` | Command/environment, source contract, acquisition basis, original/copied identities and recorded normalizations |
| `invoke.sh` | Shell-quoted command for a separately authorized later invocation; generation does not execute it |
| `prepared-files.json` | Exact hashes and sizes of the prepared files |

The generated knowledge directory is a selected view, not a full installed library. Its retained wider source catalogue can describe unselected bodies. Use `selection.json` to locate complete supplied text; resolve references using each entry's original `link_base` and logical identity. Acquire a consequential outside route explicitly. This avoids presenting copied source-relative links as already resolved local files.

The task must be UTF-8. Supports are copied as bytes. Repeat `--support` or `--output` as needed. Existing invocation directories remain untouched and their parent must exist. `--work-root` defaults to the task's original parent and is distinct from the method working directory. `--invocation-cwd` retains the caller directory used to interpret relative configuration. `--python` and `--bash` select command operands without executing them during preparation.

## Preserve the inspected runner boundaries

The runner's `method_files` hashes do not themselves deliver text. The commissioned brief therefore contains the complete main method and explicit full input routes. A prepared file and a later recipient's actual reading are distinct contributions.

The inspected renderer replaces `{run_dir}`, `{repo_root}`, `{method_root}` and `{operations_root}` in the commissioned brief. Preparation still refuses those tokens in the one inlined main method and concrete operand paths. Task, support and other selected knowledge bodies remain exact files outside that substitution step; literal token spellings in those inputs remain data.

Configuration follows the inspected literal `KEY=value` grammar, without shell sourcing. Generated quoting round-trips through `shlex.split`. Values are retained except the two method selectors and an explicitly recorded normalization of a relative `MODEL_ROUTER_CONFIG` against the retained caller directory. The Python chooser and later shell reader can start from different directories, so this normalization preserves the selected router file. `invoke.sh` quotes every command/environment operand. Its later execution can start the caller's selected provider and is a separate action.

## Check this preparation boundary

`check_preparation.py` consumes explicitly selected runtime, knowledge, valid historical-guide and current retired-root inputs, plus a new verification directory. Its `--help` gives exact operands. It exercises actual preparation, historical use, retired-root refusal, stale pins, missing/changed bodies, inline-versus-input template behavior and changed runner-source refusal. It checks the actual output with exact retained `loop.render` and `environment.paths_values` function bodies and shell-word round-tripping. `--prepared PATH` additionally checks a real already prepared receiving packet.

These checks execute only the nominated source functions and preparation commands. They do not import the complete runtime, use its fixture delivery branch, launch a provider or establish the class mapping. A capable receiving owner must read the actual packet and qualify its contribution to the current migration.
