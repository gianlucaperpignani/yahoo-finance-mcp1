"""1.0.0: load pinned technical policy; no network, no normative authority."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_policy(name):
    contract = json.loads((ROOT / "release_contract.json").read_text())
    if name not in {"risk_policy.json", "screening_config.json"}:
        raise ValueError("POLICY_NOT_ALLOWED")
    if sha_file(ROOT / name) != contract["policy_files"][name]:
        raise ValueError("POLICY_HASH_MISMATCH:" + name)
    return json.loads((ROOT / name).read_text())


POLICY = load_policy("risk_policy.json")
T = POLICY["thresholds"]


def validate_mandate(version, sha256):
    if POLICY["mandate_fingerprints"].get(version) != sha256:
        raise ValueError("MANDATE_POLICY_MISMATCH")


def package_digest(root, files):
    values = {name: sha_file(Path(root) / name) for name in sorted(files)}
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), values


def verify_engine(root):
    contract = json.loads((ROOT / "release_contract.json").read_text())
    pin = contract["risk_engine"]
    digest, files = package_digest(root, pin["files"])
    if digest != pin["package_sha256"]:
        raise ValueError("RISK_ENGINE_PACKAGE_MISMATCH")
    return dict(version=pin["version"], package_sha256=digest, files=files)
