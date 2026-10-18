"""1.0.0: actual deployed artifacts; hashes are identifiers, not backups."""
import importlib.metadata
import json
import os
import platform
from pathlib import Path
from policy_contract import sha_file

CODE = ("server.py", "level_b.py", "universe.py", "etoro_history.py", "etoro_dual_source.py",
        "etoro_corporate_actions.py", "prepare_ecb_fx.py", "acquire_ecb_fx.py",
        "prepare_etoro_history.py", "policy_contract.py", "runtime_manifest.py")
MAPS = ("etoro_etf_map.json", "etoro_isin_map.json", "etoro_corporate_actions.json", "stooq_etf_map.json")


def build(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    packages = {}
    for package in ("numpy", "pandas", "Flask", "exchange-calendars", "gunicorn"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    contract = json.loads((root / "release_contract.json").read_text())
    return dict(schema_version=1, server_commit=os.environ.get("RENDER_GIT_COMMIT"),
                code_sha256={n: sha_file(root / n) for n in CODE},
                maps_sha256={n: sha_file(root / n) for n in MAPS},
                policy_files={n: sha_file(root / n) for n in contract["policy_files"]},
                release_contract_sha256=sha_file(root / "release_contract.json"),
                expected_risk_engine=contract["risk_engine"], python=platform.python_version(),
                installed_dependencies=packages, requirements_sha256=sha_file(root / "requirements.txt"),
                archive_location="original Registro: compressed bundle chunks; no account data in GitHub",
                cache_persistent=False, runtime_manifest_is_not_cycle_completion=True)
