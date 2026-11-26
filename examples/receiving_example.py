#!/usr/bin/env python3
"""Prepare, resume and admit an accounted receiving use via G60's root CLI.

The caller owns semantic judgment and actor authorization. This module performs
only local Collective operations; it never executes a candidate's payload.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class _Stopped(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unresolved(operation, error):
    return {"status": "unresolved", "operation": operation, "error": str(error)}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip()
                                          for x in value):
        raise ValueError(name + " must be a list of nonempty strings")
    return value


def _pin(value):
    return {key: value[key] for key in ("id", "revision", "sha256")}


class _CLI:
    """Retain request/transport evidence, while the core alone owns receipts."""

    def __init__(self, engine, db):
        self.engine = Path(engine).expanduser().resolve()
        self.db = Path(db).expanduser().resolve()
        if self.db.is_relative_to(self.engine):
            raise ValueError("database and request files must be outside the engine directory")
        entry = self.engine / "store" / "cli.py"
        if not entry.is_file():
            entry = self.engine / "__main__.py"
        if not entry.is_file():
            raise ValueError("engine must contain store/cli.py or __main__.py")
        self.entry = entry
        self.files = self.db.with_name(self.db.name + ".receiving-example")
        self.files.mkdir(parents=True, exist_ok=True)

    def _run(self, arguments, request=None):
        args = [sys.executable, "-B", str(self.entry),
                "--db", str(self.db), *arguments]
        started = time.time_ns()
        try:
            completed = subprocess.run(args, capture_output=True, text=True,
                                       encoding="utf-8", check=False)
        except OSError as exc:
            outcome = _unresolved(arguments[0], exc)
            self._record(started, args, request, None, "", str(exc), outcome)
            raise _Stopped(outcome)
        try:
            outcome = json.loads(completed.stdout)
            if not isinstance(outcome, dict):
                raise ValueError("root CLI did not return a JSON object")
        except (ValueError, TypeError) as exc:
            outcome = _unresolved(arguments[0], "No complete JSON outcome was observed: " + str(exc))
            outcome["exit_code"] = completed.returncode
            outcome["stdout"] = completed.stdout
            outcome["stderr"] = completed.stderr
        self._record(started, args, request, completed.returncode,
                     completed.stdout, completed.stderr, outcome)
        if completed.returncode != 0 or outcome.get("status") in ("refused", "unresolved"):
            raise _Stopped(outcome)
        if arguments[0] == "call" and outcome.get("status") != "accepted":
            raise _Stopped(_unresolved("call", "Unrecognized core outcome; inspect retained transport evidence"))
        return outcome

    def _record(self, started, args, request, code, stdout, stderr, outcome):
        record = {"started_ns": started, "args": args, "request": request,
                  "exit_code": code, "stdout": stdout, "stderr": stderr,
                  "outcome": outcome}
        # This journal is evidence, never a substitute for the core's admission.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.files,
                                         prefix="transport-", suffix=".json", delete=False) as file:
            file.write(_json(record) + "\n")

    def initialize_if_absent(self):
        if not self.db.exists():
            self._run(["init"])

    def call(self, request):
        # Each actual invocation retains its input, including a caller's replay.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.files,
                                         prefix="request-", suffix=".json", delete=False) as file:
            file.write(_json(request) + "\n")
            name = file.name
        return self._run(["call", "--request", name], request)

    def saved_request(self, name):
        try:
            with (self.files / name).open(encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            return None

    def retain_request(self, name, value):
        """Keep the first complete input; success still comes only from core."""
        saved = self.saved_request(name)
        if saved is not None:
            return saved
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.files,
                                         prefix="pending-input-", suffix=".json", delete=False) as file:
            file.write(_json(value) + "\n")
            file.flush()
            os.fsync(file.fileno())
            temporary = file.name
        try:
            # Atomic no-overwrite installation also preserves one exact request
            # when two invocations of the same caller attempt overlap.
            try:
                os.link(temporary, self.files / name)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        return self.saved_request(name)


def prepare(engine: str, db: str, spec: dict) -> dict:
    """Publish the caller's explicit receiving account; stop on any failure.

    A namespace identifies one exact preparation. Repeating it recovers prior
    local receipts. Changed input needs a new namespace and receiving judgment.
    """
    try:
        if not isinstance(spec, dict):
            raise ValueError("prepare specification must be a JSON object")
        spec = json.loads(_json(spec))
        namespace = _text(spec.get("namespace"), "namespace")
        producer = _text(spec.get("producer"), "producer")
        receiver = _text(spec.get("receiver"), "receiver")
        account = _text(spec.get("account"), "account")
        if producer == receiver:
            return {"status": "refused", "operation": "prepare",
                    "reason": "distinct-owners-required",
                    "account": "The candidate producer and receiving owner must differ; no store operation was attempted."}
        if "candidate" not in spec or "observation" not in spec:
            raise ValueError("candidate and observation bodies must both be supplied")
        sources = _strings(spec.get("sources", []), "sources")
        policy = _strings(spec.get("reconsider_from", []), "reconsider_from")
        prefix = "receiving-" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        ids = {role: prefix + "-" + role for role in
               ("candidate", "observation", "approval", "context", "proposal")}
        core = _CLI(engine, db)
        core.initialize_if_absent()

        def call(actor, step, operation, **fields):
            return core.call({"actor": actor, "command": prefix + "-" + step,
                              "op": operation, **fields})

        candidate = call(producer, "create-candidate", "create",
                         id=ids["candidate"], body=spec["candidate"])["artifact"]
        observation = call(receiver, "create-observation", "create",
                           id=ids["observation"], body=spec["observation"])["artifact"]
        call(receiver, "create-approval", "create", id=ids["approval"],
             body={"format": "g60-receiver-use/1", "disposition": "pending"},
             reconsider_from=policy)
        source_ids = list(dict.fromkeys([ids["candidate"], ids["observation"], *sources]))
        captured = call(receiver, "capture", "capture", id=ids["context"],
                        target=ids["approval"], sources=source_ids)["context"]
        captured_pins = {item["id"]: _pin(item) for item in captured["sources"]}
        # Do not apply the supplied account to substituted candidate/observation
        # editions. Additional source currentness is owned by capture/publish/take.
        for expected in (candidate, observation):
            current = captured_pins.get(expected["id"])
            if current != expected:
                return {"status": "refused", "operation": "prepare",
                        "reason": "input-changed-before-capture", "expected": expected,
                        "captured": current, "context": ids["context"]}
        body = {"format": "g60-receiver-use/1", "disposition": "ready",
                "candidate": ids["candidate"], "observation": ids["observation"],
                "account": account, "sources": captured["basis"]}
        call(receiver, "propose", "propose", id=ids["proposal"],
             context=ids["context"], body=body)
        # No automatic notice treatment: uncovered required findings make the
        # real publication refuse and remain owed at this exact target.
        receipt = call(receiver, "publish", "publish", proposal=ids["proposal"],
                       account=account)
        return {"status": receipt["status"], "candidate": candidate,
                "observation": observation, "approval": receipt["artifact"],
                "receipt": receipt, "sources": captured["basis"],
                "context": ids["context"], "proposal": ids["proposal"]}
    except _Stopped as stopped:
        return stopped.outcome
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _unresolved("prepare", exc)


def take(engine: str, db: str, spec: dict) -> dict:
    """Return the actual native take receipt, including refusals and replay."""
    try:
        if not isinstance(spec, dict):
            raise ValueError("take specification must be a JSON object")
        spec = json.loads(_json(spec))
        receiver = _text(spec.get("receiver"), "receiver")
        command = _text(spec.get("command"), "command")
        if not isinstance(spec.get("approval"), dict):
            raise ValueError("approval must be the exact returned pin object")
        core = _CLI(engine, db)
        core.initialize_if_absent()
        return core.call({"actor": receiver, "command": command, "op": "take",
                          "approval": spec["approval"]})
    except _Stopped as stopped:
        return stopped.outcome
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _unresolved("take", exc)


def resume(engine: str, db: str, spec: dict) -> dict:
    """Reissue this unfinished target using the caller's actual judgments.

    Omission never releases a source. The core retains continuing enrollments,
    and only a later native take determines whether this approval can be used.
    """
    try:
        if not isinstance(spec, dict):
            raise ValueError("resume specification must be a JSON object")
        spec = json.loads(_json(spec))
        receiver = _text(spec.get("receiver"), "receiver")
        command_prefix = _text(spec.get("command_prefix"), "command_prefix")
        target = _text(spec.get("target"), "target")
        candidate_id = _text(spec.get("candidate"), "candidate")
        observation_id = _text(spec.get("observation"), "observation")
        account = _text(spec.get("account"), "account")
        sources = _strings(spec.get("sources", []), "sources")
        decisions = spec.get("source_decisions", [])
        treatments = spec.get("treatments", [])
        if not isinstance(decisions, list) or not isinstance(treatments, list):
            raise ValueError("source_decisions and treatments must be lists")
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("each source decision must be an object")
            _text(decision.get("source"), "source_decisions.source")
            _text(decision.get("account"), "source_decisions.account")
            if decision.get("disposition") not in ("renew", "release"):
                raise ValueError("source disposition must be renew or release")
        for treatment in treatments:
            if not isinstance(treatment, dict):
                raise ValueError("each treatment must be an object")
            _text(treatment.get("account"), "treatments.account")
            if type(treatment.get("notice")) is not int or treatment["notice"] < 1:
                raise ValueError("treatment notice must be a positive integer")
            if treatment.get("disposition") not in ("incorporated", "retained-adequate", "not-applicable"):
                raise ValueError("unsupported finding treatment disposition")
        identity = _json({"receiver": receiver, "command_prefix": command_prefix})
        prefix = "resume-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        core = _CLI(engine, db)
        core.initialize_if_absent()
        retained_spec = core.retain_request(prefix + "-spec.json", spec)
        if _json(retained_spec) != _json(spec):
            raise ValueError("This command_prefix already names a different resume specification")

        def call(step, operation, **fields):
            return core.call({"actor": receiver, "command": prefix + "-" + step,
                              "op": operation, **fields})

        qualifications = []
        for index, decision in enumerate(decisions):
            source = decision["source"]
            command = prefix + "-qualify-" + str(index)
            name = command + ".json"
            request = core.saved_request(name)
            if request is None:
                current_target = core._run(["show", target])
                relation = core._run(["use-source", target, source])
                prior = core._run(["show", source, "--revision", str(relation["source"]["revision"])])
                current = core._run(["show", source])
                request = core.retain_request(name, {
                    "actor": receiver, "command": command, "op": "qualify",
                    "target": _pin(current_target), "prior": _pin(prior), "current": _pin(current),
                    "disposition": decision["disposition"], "account": decision["account"]})
            # Retained files hold exact requests, never a local completion flag.
            # Every caller replay asks the authoritative core for that result.
            expected = {"actor": receiver, "command": command, "op": "qualify",
                        "disposition": decision["disposition"], "account": decision["account"]}
            if any(request.get(key) != value for key, value in expected.items()):
                raise ValueError("Retained qualification request disagrees with this exact attempt")
            if request["target"]["id"] != target or any(request[key]["id"] != source for key in ("prior", "current")):
                raise ValueError("Retained qualification pins name a different target or source")
            qualifications.append(core.call(request))

        context_id, proposal_id = prefix + "-context", prefix + "-proposal"
        source_ids = list(dict.fromkeys([candidate_id, observation_id, *sources]))
        captured = call("capture", "capture", id=context_id, target=target,
                        sources=source_ids, notices=[item["notice"] for item in treatments])["context"]
        records = {item["id"]: item for item in captured["sources"]}
        candidate, observation = records[candidate_id], records[observation_id]
        if candidate["owner"] == receiver or observation["owner"] != receiver:
            return {"status": "refused", "operation": "resume", "reason": "source-role-owner-mismatch",
                    "candidate": _pin(candidate), "observation": _pin(observation), "context": context_id}
        body = {"format": "g60-receiver-use/1", "disposition": "ready",
                "candidate": candidate_id, "observation": observation_id,
                "account": account, "sources": captured["basis"],
                "source_decisions": [item["judgment"] for item in qualifications]}
        call("propose", "propose", id=proposal_id, context=context_id, body=body)
        receipt = call("publish", "publish", proposal=proposal_id,
                       account=account, treatments=treatments)
        return {"status": receipt["status"], "candidate": _pin(candidate),
                "observation": _pin(observation), "approval": receipt["artifact"],
                "receipt": receipt, "sources": captured["basis"],
                "context": context_id, "proposal": proposal_id, "qualifications": qualifications}
    except _Stopped as stopped:
        return stopped.outcome
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _unresolved("resume", exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--operation", choices=("prepare", "take", "resume"), required=True)
    parser.add_argument("--input", required=True,
                        help="JSON request file, '-' for stdin, or an inline JSON object")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            spec = json.load(sys.stdin)
        elif args.input.lstrip().startswith("{"):
            spec = json.loads(args.input)
        else:
            with open(args.input, encoding="utf-8") as file:
                spec = json.load(file)
        result = {"prepare": prepare, "take": take, "resume": resume}[args.operation](args.engine, args.db, spec)
    except (OSError, ValueError, TypeError) as exc:
        result = _unresolved(args.operation, exc)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return {"accepted": 0, "refused": 2, "unresolved": 3}.get(result.get("status"), 3)


if __name__ == "__main__":
    raise SystemExit(main())
