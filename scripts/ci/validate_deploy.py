#!/usr/bin/env python3
"""Pure-stdlib static validator for container, local compose, OpenTofu, and GitOps substrates."""

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai_information.platform import check_ordinary_ancestors, is_ordinary_file_stat  # noqa: E402

FORBIDDEN_WHEEL_DIRS = ("containers", "deploy", "infra", "gitops", ".buildkite", "scripts")
REQUIRED_DOCKERIGNORE = (
    ".git",
    "tests",
    "docs",
    "examples",
    "deploy",
    "infra",
    "gitops",
    ".buildkite",
    "scripts",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
)
REQUIRED_INFRA_MODULES = (
    "platform-cluster-k3d",
    "platform-cluster-vms",
    "platform-network",
)
REQUIRED_GITOPS_BASE = (
    "namespace.yaml",
    "networkpolicy.yaml",
    "pvc.yaml",
    "deployment.yaml",
    "kustomization.yaml",
)

_LATEST_TAG_RE = re.compile(r":latest(?:\b|[\s\"'\]})])", re.IGNORECASE)
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.IGNORECASE
)
_USER_RE = re.compile(r"^\s*USER\s+(?P<spec>\S+)", re.IGNORECASE)
_DIGEST_IMAGE_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")
_KUSTOMIZE_DIGEST_RE = re.compile(r"^\s*digest:\s*sha256:[0-9a-f]{64}\s*$")
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?\b")
_ALLOWED_IPV4_NETS = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_SECRET_RE = re.compile(
    r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|AGE-SECRET-KEY-1[A-Z0-9]+|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})",
)


def _read_ordinary_text(root: Path, rel: str, errors: list[str]) -> str | None:
    target = root / rel
    try:
        checked = check_ordinary_ancestors(
            target,
            expect_dir=False,
            error_prefix=f"ordinary non-symlink file required ({rel})",
        )
    except FileNotFoundError:
        errors.append(f"missing required deploy file: {rel}")
        return None
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return None

    try:
        info = checked.lstat()
    except OSError:
        errors.append(f"missing required deploy file: {rel}")
        return None

    if not is_ordinary_file_stat(info):
        errors.append(f"ordinary non-symlink file required: {rel}")
        return None

    try:
        return checked.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"failed to read UTF-8 file {rel}: {exc}")
        return None


def _strip_yaml_comments(text: str) -> list[str]:
    cleaned: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cleaned.append(raw)
    return cleaned


def _yaml_section_lines(lines: list[str], top_key: str) -> list[str]:
    in_section = False
    out: list[str] = []
    prefix = f"{top_key}:"

    for line in lines:
        if not line.startswith(" ") and not line.startswith("\t"):
            if line.strip() == prefix or line.strip().startswith(prefix + " "):
                in_section = True
                continue
            if in_section:
                break
        elif in_section:
            out.append(line)

    return out


def _yaml_subblock_lines(section_lines: list[str], key: str, indent: int = 2) -> list[str]:
    in_block = False
    out: list[str] = []
    marker = (" " * indent) + f"{key}:"

    for line in section_lines:
        lead = len(line) - len(line.lstrip(" "))
        if lead == indent:
            if line.startswith(marker):
                in_block = True
                continue
            if in_block:
                break
        elif in_block and lead > indent:
            out.append(line)

    return out


def _check_balanced_hcl(rel: str, text: str, errors: list[str]) -> None:
    pairs = {"{": "}", "[": "]", "(": ")"}
    closing = {v: k for k, v in pairs.items()}
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False

    for line_no, raw in enumerate(text.splitlines(), 1):
        idx = 0
        length = len(raw)
        while idx < length:
            ch = raw[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == "#" or (ch == "/" and idx + 1 < length and raw[idx + 1] == "/"):
                    break
                if ch == '"':
                    in_string = True
                elif ch in pairs:
                    stack.append((ch, line_no))
                elif ch in closing:
                    if not stack or stack[-1][0] != closing[ch]:
                        errors.append(f"{rel}:{line_no}: unbalanced HCL delimiter {ch!r}")
                        return
                    stack.pop()
            idx += 1

    if in_string:
        errors.append(f"{rel}: unterminated HCL string literal")
    if stack:
        ch, ln = stack[-1]
        errors.append(f"{rel}:{ln}: unclosed HCL delimiter {ch!r}")


def _check_no_secrets_or_public_ips(rel: str, text: str, errors: list[str]) -> None:
    if _SECRET_RE.search(text):
        errors.append(f"{rel}: contains prohibited hardcoded secret or private key material")

    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        for match in _IPV4_RE.finditer(stripped):
            addr_text = match.group(1)
            prefix_text = match.group(2)
            try:
                addr = ipaddress.IPv4Address(addr_text)
            except ValueError:
                continue

            if addr.is_unspecified:
                if '!= "0.0.0.0/0"' in stripped or "never 0.0.0.0/0" in stripped:
                    continue
                errors.append(f"{rel}:{line_no}: wildcard bind/CIDR {match.group(0)} is prohibited")
                continue

            if any(addr in net for net in _ALLOWED_IPV4_NETS):
                continue

            if prefix_text is not None or not stripped.startswith("version"):
                errors.append(
                    f"{rel}:{line_no}: hardcoded non-RFC1918 IPv4 literal {addr_text} is prohibited"
                )


def validate_containerfile(root: Path, errors: list[str]) -> None:
    """Validate multi-stage Containerfile and .dockerignore invariants."""
    text = _read_ordinary_text(root, "containers/Containerfile", errors)
    if text is not None:
        from_stages: list[tuple[str, str]] = []
        users: list[str] = []

        for line in text.splitlines():
            m_from = _FROM_RE.match(line)
            if m_from:
                img = m_from.group("image")
                stage = m_from.group("stage") or ""
                from_stages.append((img, stage))
                if _LATEST_TAG_RE.search(img) or (":" not in img and "@sha256:" not in img):
                    errors.append(
                        "containers/Containerfile: base image must be pinned "
                        f"(never :latest): {img}"
                    )

            m_user = _USER_RE.match(line)
            if m_user:
                users.append(m_user.group("spec"))

        if len(from_stages) < 2:
            errors.append("containers/Containerfile: multi-stage build (>=2 FROM stages) required")

        if not users:
            errors.append("containers/Containerfile: explicit non-root USER directive required")
        elif users[-1].lower() in ("0", "0:0", "root", "root:root"):
            errors.append(
                f"containers/Containerfile: final USER must be non-root (got {users[-1]!r})"
            )

        if "setuptools==" not in text:
            errors.append(
                "containers/Containerfile: builder stage must install pinned setuptools=="
            )

        if "--no-deps" not in text:
            errors.append("containers/Containerfile: wheel build/install must use --no-deps")

        if 'VOLUME ["/var/lib/managed-ai-information"]' not in text:
            errors.append(
                'containers/Containerfile: missing VOLUME ["/var/lib/managed-ai-information"]'
            )

        if "HEALTHCHECK" not in text:
            errors.append("containers/Containerfile: missing HEALTHCHECK directive")

        if 'ENTRYPOINT ["managed-ai-information"]' not in text:
            errors.append('containers/Containerfile: missing ENTRYPOINT ["managed-ai-information"]')

    ignore_text = _read_ordinary_text(root, ".dockerignore", errors)
    if ignore_text is not None:
        entries = {
            line.strip()
            for line in ignore_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for item in REQUIRED_DOCKERIGNORE:
            if item not in entries:
                errors.append(f".dockerignore: missing required exclusion {item!r}")


def validate_local_compose(root: Path, errors: list[str]) -> None:
    """Validate IP-hiding local Docker compose stack, gateway config, and README."""
    compose = _read_ordinary_text(root, "deploy/local/compose.yaml", errors)
    if compose is not None:
        _check_no_secrets_or_public_ips("deploy/local/compose.yaml", compose, errors)
        if _LATEST_TAG_RE.search(compose):
            errors.append("deploy/local/compose.yaml: :latest image tags are prohibited")

        lines = _strip_yaml_comments(compose)
        services = _yaml_section_lines(lines, "services")
        networks = _yaml_section_lines(lines, "networks")

        worker = _yaml_subblock_lines(services, "managed-ai-information", indent=2)
        gateway = _yaml_subblock_lines(services, "gateway", indent=2)
        internal_net = _yaml_subblock_lines(networks, "managed_ai_information_internal", indent=2)

        if not worker:
            errors.append("deploy/local/compose.yaml: missing managed-ai-information service")
        else:
            worker_text = "\n".join(worker)
            if any(line.strip().startswith("ports:") for line in worker):
                errors.append("deploy/local/compose.yaml: worker must NOT publish host ports")
            if "read_only: true" not in worker_text:
                errors.append("deploy/local/compose.yaml: worker requires read_only: true")
            if "no-new-privileges:true" not in worker_text:
                errors.append("deploy/local/compose.yaml: worker requires no-new-privileges:true")
            if "ALL" not in worker_text or "cap_drop:" not in worker_text:
                errors.append("deploy/local/compose.yaml: worker requires cap_drop: ALL")
            if "managed_ai_information_store:/var/lib/managed-ai-information" not in worker_text:
                errors.append(
                    "deploy/local/compose.yaml: worker must mount "
                    "managed_ai_information_store at /var/lib/managed-ai-information"
                )
            if "managed_ai_information_edge" in worker_text:
                errors.append(
                    "deploy/local/compose.yaml: worker must stay exclusively on "
                    "managed_ai_information_internal"
                )

        if not any(line.strip() == "internal: true" for line in internal_net):
            errors.append(
                "deploy/local/compose.yaml: managed_ai_information_internal "
                "network must declare internal: true"
            )

        if not gateway:
            errors.append("deploy/local/compose.yaml: missing gateway service")
        else:
            gw_ports = _yaml_subblock_lines(gateway, "ports", indent=4)
            if not gw_ports:
                errors.append(
                    "deploy/local/compose.yaml: gateway must publish a loopback control port"
                )
            for p_line in gw_ports:
                val = p_line.strip().lstrip("-").strip().strip('"').strip("'")
                if not val.startswith("127.0.0.1:"):
                    errors.append(
                        "deploy/local/compose.yaml: gateway port must bind "
                        f"127.0.0.1 only (got {val!r})"
                    )

    gw_conf = _read_ordinary_text(root, "deploy/local/gateway.conf", errors)
    if gw_conf is not None:
        _check_no_secrets_or_public_ips("deploy/local/gateway.conf", gw_conf, errors)
        if "server_tokens off;" not in gw_conf:
            errors.append("deploy/local/gateway.conf: missing server_tokens off;")
        if "/healthz" not in gw_conf:
            errors.append("deploy/local/gateway.conf: missing /healthz route")

    _read_ordinary_text(root, "deploy/local/README.md", errors)


def validate_infra(root: Path, errors: list[str]) -> None:
    """Validate OpenTofu channel configs and HCL modules."""
    for channel, provider, exposure in (
        ("local", "k3d-local", "loopback-gateway"),
        ("dev", "vultr-vms", "internal-gateway"),
    ):
        rel = f"infra/channels/{channel}.yaml"
        text = _read_ordinary_text(root, rel, errors)
        if text is None:
            continue
        _check_no_secrets_or_public_ips(rel, text, errors)
        if _LATEST_TAG_RE.search(text):
            errors.append(f"{rel}: :latest tags are prohibited")
        if f"provider: {provider}" not in text:
            errors.append(f"{rel}: expected provider: {provider}")
        if f"exposure: {exposure}" not in text:
            errors.append(f"{rel}: expected exposure: {exposure}")

    for mod in REQUIRED_INFRA_MODULES:
        for fname in ("main.tf", "variables.tf", "outputs.tf"):
            rel = f"infra/modules/{mod}/{fname}"
            text = _read_ordinary_text(root, rel, errors)
            if text is None:
                continue
            _check_balanced_hcl(rel, text, errors)
            _check_no_secrets_or_public_ips(rel, text, errors)
            if _LATEST_TAG_RE.search(text):
                errors.append(f"{rel}: :latest tags are prohibited")
            if fname == "main.tf" and (
                "required_version" not in text or "required_providers" not in text
            ):
                errors.append(f"{rel}: missing pinned required_version / required_providers block")
            if fname == "variables.tf" and "validation {" not in text:
                errors.append(f"{rel}: missing explicit variable validation block")


def validate_gitops(root: Path, errors: list[str]) -> None:
    """Validate Flux/Kustomize base manifests, channel overlays, and SOPS policy."""
    for fname in REQUIRED_GITOPS_BASE:
        rel = f"gitops/base/{fname}"
        text = _read_ordinary_text(root, rel, errors)
        if text is None:
            continue
        _check_no_secrets_or_public_ips(rel, text, errors)
        if _LATEST_TAG_RE.search(text):
            errors.append(f"{rel}: :latest image tags are prohibited")

        if (
            fname == "namespace.yaml"
            and "pod-security.kubernetes.io/enforce: restricted" not in text
        ):
            errors.append(
                f"{rel}: namespace must enforce Pod Security Standards restricted profile"
            )

        if fname == "networkpolicy.yaml":
            if "podSelector: {}" not in text or "- Ingress" not in text or "- Egress" not in text:
                errors.append(f"{rel}: missing default-deny-all Ingress+Egress NetworkPolicy")
            if "managed-ai-information-gateway" not in text:
                errors.append(f"{rel}: missing gateway-only ingress NetworkPolicy rule")

        if fname == "pvc.yaml" and "kind: PersistentVolumeClaim" not in text:
            errors.append(f"{rel}: missing PersistentVolumeClaim manifest")

        if fname == "deployment.yaml":
            for req in (
                "runAsNonRoot: true",
                "readOnlyRootFilesystem: true",
                "allowPrivilegeEscalation: false",
                "mountPath: /var/lib/managed-ai-information",
                "claimName: managed-ai-information-store",
            ):
                if req not in text:
                    errors.append(f"{rel}: missing required security/storage setting {req!r}")
            if not _DIGEST_IMAGE_RE.search(text):
                errors.append(f"{rel}: container image must be pinned by immutable @sha256 digest")

    for ch in ("local", "dev"):
        rel = f"gitops/channels/{ch}/kustomization.yaml"
        text = _read_ordinary_text(root, rel, errors)
        if text is None:
            continue
        _check_no_secrets_or_public_ips(rel, text, errors)
        if _LATEST_TAG_RE.search(text):
            errors.append(f"{rel}: :latest image tags are prohibited")
        if "commonLabels:" in text:
            errors.append(
                f"{rel}: deprecated commonLabels is prohibited; "
                "use Kustomize v5+ labels with includeSelectors: false"
            )
        if "../../base" not in text:
            errors.append(f"{rel}: channel overlay must reference ../../base")
        if not any(_KUSTOMIZE_DIGEST_RE.match(line) for line in text.splitlines()):
            errors.append(f"{rel}: channel overlay must pin immutable sha256 image digest")

    sops = _read_ordinary_text(root, ".sops.yaml", errors)
    if sops is not None:
        _check_no_secrets_or_public_ips(".sops.yaml", sops, errors)
        if "creation_rules:" not in sops or "encrypted_regex:" not in sops:
            errors.append(".sops.yaml: missing creation_rules or encrypted_regex")


def validate_wheel_exclusions(root: Path, errors: list[str]) -> None:
    """Confirm pyproject.toml ships strictly src/managed_ai_information with zero leakage."""
    raw = _read_ordinary_text(root, "pyproject.toml", errors)
    if raw is None:
        return

    try:
        doc = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"pyproject.toml: invalid TOML: {exc}")
        return

    deps = doc.get("project", {}).get("dependencies", [])
    if deps:
        errors.append(f"pyproject.toml: runtime dependencies must stay empty (got {deps!r})")

    find_where = (
        doc.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {}).get("where")
    )
    if find_where != ["src"]:
        errors.append(
            f"pyproject.toml: setuptools packages.find.where must be ['src'] (got {find_where!r})"
        )

    src_pkg = root / "src" / "managed_ai_information"
    for forbidden in FORBIDDEN_WHEEL_DIRS:
        if (src_pkg / forbidden).exists():
            errors.append(
                f"src/managed_ai_information/{forbidden}: "
                "forbidden deploy/CI directory inside wheel package"
            )


def validate_repository(root: Path) -> dict[str, Any]:
    """Run all static container, compose, OpenTofu, GitOps, and wheel-exclusion checks."""
    checked_root = check_ordinary_ancestors(
        root, expect_dir=True, error_prefix="ordinary repository root required"
    )
    resolved_root = checked_root.resolve(strict=True)
    errors: list[str] = []

    validate_containerfile(resolved_root, errors)
    validate_local_compose(resolved_root, errors)
    validate_infra(resolved_root, errors)
    validate_gitops(resolved_root, errors)
    validate_wheel_exclusions(resolved_root, errors)

    return {
        "format": "managed-ai-deploy-validation/1",
        "root": str(resolved_root),
        "status": "passed" if not errors else "failed",
        "error_count": len(errors),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Static validator for containers, local compose, OpenTofu, and GitOps manifests."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root directory to validate (default: this checkout).",
    )
    args = parser.parse_args(argv)

    try:
        report = validate_repository(args.root)
    except (OSError, ValueError) as exc:
        payload = {
            "format": "managed-ai-deploy-validation/1",
            "status": "error",
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), file=sys.stderr)
        return 2

    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if report["status"] != "passed":
        print(output, file=sys.stderr)
        return 2

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
