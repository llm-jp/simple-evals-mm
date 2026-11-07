"""Post quantum attestation of completed evaluation results.

Signs a finished result file (an attempt's meta.json, which pins the sha256 of
its trajectories, or the trajectory JSONL itself) with FIPS 204 ML-DSA so a
reader can confirm the file is authentic and has not been altered, using a
signature scheme that stays safe once large quantum computers exist.

The attestation is self contained. It carries the public key and the signature,
so a verifier needs only the attestation and the file. Integrity is always
covered by the payload digest and authenticity is covered when the verifier
pins the expected public key.

The scheme comes from the optional pq extra. Install it with
`pip install simple-evals-mm[pq]`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

SCHEME = "ML-DSA-65"
ISSUER = "Quantova"

try:
    from dilithium_py.ml_dsa import ML_DSA_65 as _BACKEND
except ImportError:
    _BACKEND = None


def _require_backend() -> None:
    if _BACKEND is None:
        msg = "Post quantum attestation needs the pq extra. Install with pip install simple-evals-mm[pq]."
        raise RuntimeError(msg)


def generate_keypair() -> tuple[bytes, bytes]:
    """Return a fresh (public_key, secret_key) pair for the signer."""
    _require_backend()
    return _BACKEND.keygen()


def save_signer(path: str, public_key: bytes, secret_key: bytes) -> None:
    """Write a signer key file that load_signer reads back."""
    data = {
        "scheme": SCHEME,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "secret_key": base64.b64encode(secret_key).decode("ascii"),
    }
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def load_signer(path: str) -> tuple[bytes, bytes]:
    """Read (public_key, secret_key) from a signer key file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return base64.b64decode(data["public_key"]), base64.b64decode(data["secret_key"])


def build_attestation(target: str, public_key: bytes, secret_key: bytes) -> dict:
    """Return a self contained attestation over the bytes of the target file."""
    _require_backend()
    payload = Path(target).read_bytes()
    signature = _BACKEND.sign(secret_key, payload, deterministic=True)
    return {
        "scheme": SCHEME,
        "issuer": ISSUER,
        "target": Path(target).name,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_attestation(target: str, attestation: dict, expected_public_key: bytes | None = None) -> bool:
    """Return True when the attestation matches the target file.

    The payload digest is always checked. When expected_public_key is given the
    signer is pinned too, so a valid signature made under a different key is
    rejected.
    """
    _require_backend()
    payload = Path(target).read_bytes()
    if attestation.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        return False
    try:
        public_key = base64.b64decode(attestation["public_key"])
        signature = base64.b64decode(attestation["signature"])
    except (KeyError, ValueError):
        return False
    if expected_public_key is not None and public_key != expected_public_key:
        return False
    return bool(_BACKEND.verify(public_key, payload, signature))


def _main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "keygen":
        public_key, secret_key = generate_keypair()
        save_signer(argv[1], public_key, secret_key)
        print(f"wrote {SCHEME} signer to {argv[1]}")
        return 0
    if len(argv) == 3 and argv[0] == "attest":
        public_key, secret_key = load_signer(argv[1])
        attestation = build_attestation(argv[2], public_key, secret_key)
        out = argv[2] + ".attestation.json"
        Path(out).write_text(json.dumps(attestation, indent=2), encoding="utf-8")
        print(f"wrote attestation to {out}")
        return 0
    if len(argv) == 3 and argv[0] == "verify":
        attestation = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        ok = verify_attestation(argv[1], attestation)
        print("valid" if ok else "invalid")
        return 0 if ok else 1
    print(
        "usage: python -m simple_evals_mm.pq_attestation "
        "keygen <key> | attest <key> <file> | verify <file> <attestation>"
    )
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
