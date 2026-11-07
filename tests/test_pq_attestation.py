"""Tests for the post quantum attestation of evaluation results.

These need the pq extra (dilithium-py). They touch only local files, so they
run without network, API keys or a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("dilithium_py")

from simple_evals_mm import pq_attestation as pqa

META = {
    "attempt": 1,
    "complete": True,
    "trials_sha256": "a" * 64,
    "commit": "deadbeef",
}


def _write_meta(tmp_path: Path) -> str:
    path = tmp_path / "trials_a1.meta.json"
    path.write_text(json.dumps(META), encoding="utf-8")
    return str(path)


def test_build_and_verify_roundtrip(tmp_path: Path) -> None:
    target = _write_meta(tmp_path)
    public_key, secret_key = pqa.generate_keypair()
    attestation = pqa.build_attestation(target, public_key, secret_key)
    assert attestation["scheme"] == "ML-DSA-65"
    assert attestation["issuer"] == "Quantova"
    assert pqa.verify_attestation(target, attestation) is True
    assert pqa.verify_attestation(target, attestation, expected_public_key=public_key) is True


def test_tampered_file_is_rejected(tmp_path: Path) -> None:
    target = _write_meta(tmp_path)
    public_key, secret_key = pqa.generate_keypair()
    attestation = pqa.build_attestation(target, public_key, secret_key)
    Path(target).write_text(json.dumps({**META, "trials_sha256": "b" * 64}), encoding="utf-8")
    assert pqa.verify_attestation(target, attestation) is False


def test_wrong_signer_is_rejected_when_pinned(tmp_path: Path) -> None:
    target = _write_meta(tmp_path)
    public_key, secret_key = pqa.generate_keypair()
    other_public_key, _ = pqa.generate_keypair()
    attestation = pqa.build_attestation(target, public_key, secret_key)
    assert pqa.verify_attestation(target, attestation, expected_public_key=other_public_key) is False


def test_forged_signature_is_rejected(tmp_path: Path) -> None:
    import base64

    target = _write_meta(tmp_path)
    public_key, secret_key = pqa.generate_keypair()
    attestation = pqa.build_attestation(target, public_key, secret_key)
    attestation["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    assert pqa.verify_attestation(target, attestation) is False


def test_key_file_roundtrip(tmp_path: Path) -> None:
    public_key, secret_key = pqa.generate_keypair()
    path = str(tmp_path / "signer.json")
    pqa.save_signer(path, public_key, secret_key)
    loaded_public, loaded_secret = pqa.load_signer(path)
    assert loaded_public == public_key
    assert loaded_secret == secret_key
