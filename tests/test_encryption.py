"""Deliverable 1: transit policy, fail-closed secrets, at-rest envelope encryption, secrets not in git."""

from __future__ import annotations

import subprocess
import sys

import pytest

from security import encrypt_at_rest as ear
from security.resolve_secrets import resolve
from security.transit_policy import ROOT, assert_partners_ok, check_partners, transit_ok

GOOD = "Zq3v8N1rS6pY0wE4tU7iO2aL5kJ9hG8fD1sA3qW6eR0tY"


# ---- transit ----------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://afyaplus-kv.vault.azure.net",
                                 "HTTPS://Stock-Feed.benkiyetu.example/v1"])
def test_https_is_allowed_everywhere(url):
    for env in ("dev", "staging", "prod"):
        assert transit_ok(url, env).ok


@pytest.mark.parametrize("url", ["http://stock-feed.benkiyetu.example/v1/clinics", "http://10.0.0.5/api",
                                 "http://api.openai.com/v1", "ftp://files.partner.example/x",
                                 "ws://sms.partner.example/stream"])
def test_cleartext_or_odd_scheme_to_a_real_host_is_refused_in_every_environment(url):
    for env in ("dev", "staging", "prod"):
        assert not transit_ok(url, env).ok


def test_cleartext_production_host_is_refused():
    d = transit_ok("http://triage.benkiyetu.example", "prod")
    assert not d.ok and "cleartext" in d.reason


@pytest.mark.parametrize("url", ["http://localhost:8080/health", "http://127.0.0.1:8765/mock", "http://[::1]:8080"])
def test_localhost_http_is_a_lab_exception_only(url):
    assert transit_ok(url, "dev").ok and transit_ok(url, "staging").ok
    assert not transit_ok(url, "prod").ok


def test_credentials_in_url_and_unknown_environment_are_refused():
    assert not transit_ok("https://user:pw@sms.partner.example/v2", "dev").ok
    assert not transit_ok("http://localhost:8080", "qa").ok      # unknown env is treated as prod


def test_shipped_partner_config_passes_and_the_broken_fixture_fails():
    for env in ("dev", "staging", "prod"):
        assert all(d.ok for _, d in check_partners(env))
    broken = ROOT / "security" / "fixtures" / "partners_cleartext.yaml"
    with pytest.raises(RuntimeError, match="clinic_stock_feed"):
        assert_partners_ok("prod", broken)


def test_api_refuses_to_start_with_a_cleartext_partner(monkeypatch):
    from fastapi.testclient import TestClient

    import prompt_app

    broken = ROOT / "security" / "fixtures" / "partners_cleartext.yaml"
    monkeypatch.setattr(prompt_app, "assert_partners_ok", lambda env: assert_partners_ok("prod", broken))
    with pytest.raises(RuntimeError, match="transit policy refused"), TestClient(prompt_app.app):
        pass


# ---- secrets fail closed --------------------------------------------------------

def test_missing_jwt_secret_fails_closed(tmp_path):
    results = {r.name: r for r in resolve("api", {"SECRETS_DIR": str(tmp_path), "AUDIT_HASH_KEY": GOOD})}
    assert results["JWT_SECRET"].fatal and results["JWT_SECRET"].reason == "missing"
    assert not results["OPENAI_API_KEY"].required          # only required for MODEL_BACKEND=openai


def test_placeholders_and_short_values_fail_closed(tmp_path):
    env = {"SECRETS_DIR": str(tmp_path), "JWT_SECRET": "change-me-to-a-long-random-string-of-48-chars",
           "AUDIT_HASH_KEY": "short", "MODEL_BACKEND": "openai"}
    r = {x.name: x for x in resolve("api", env)}
    assert r["JWT_SECRET"].fatal and "placeholder" in r["JWT_SECRET"].reason
    assert r["AUDIT_HASH_KEY"].fatal and "too short" in r["AUDIT_HASH_KEY"].reason
    assert r["OPENAI_API_KEY"].fatal                       # now required, and missing


def test_test_values_pass_in_dev_but_not_in_prod(tmp_path):
    env = {"SECRETS_DIR": str(tmp_path), "JWT_SECRET": "test-secret-key-for-pytest-only-0123456789abcdef",
           "AUDIT_HASH_KEY": GOOD}
    assert not any(r.fatal for r in resolve("api", {**env, "DEPLOY_ENV": "dev"}))
    assert any(r.fatal for r in resolve("api", {**env, "DEPLOY_ENV": "prod"}))


def test_secret_can_come_from_a_mounted_file_and_is_never_printed(tmp_path):
    (tmp_path / "jwt_secret").write_text(GOOD + "\n")
    r = {x.name: x for x in resolve("api", {"SECRETS_DIR": str(tmp_path), "AUDIT_HASH_KEY": GOOD})}
    assert r["JWT_SECRET"].ok and r["JWT_SECRET"].source.startswith("file:")
    assert GOOD not in repr(r["JWT_SECRET"])


def test_cli_exits_1_without_jwt_secret(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "SECRETS_DIR": str(tmp_path), "AUDIT_HASH_KEY": GOOD}
    p = subprocess.run([sys.executable, "-m", "security.resolve_secrets", "--check"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert p.returncode == 1 and "REFUSING TO START: JWT_SECRET" in p.stderr
    assert GOOD not in p.stdout + p.stderr


def test_api_settings_refuse_a_placeholder_secret(monkeypatch):
    from pydantic import ValidationError

    from afyaplus.settings import Settings

    monkeypatch.setenv("JWT_SECRET", "change-me-to-a-long-random-string-of-48-chars")
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(_env_file=None)
    monkeypatch.delenv("JWT_SECRET")
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(_env_file=None)


def test_startup_error_names_the_problem_but_never_echoes_secret_values(monkeypatch):
    from afyaplus.settings import SettingsError, get_settings

    monkeypatch.delenv("JWT_SECRET")
    monkeypatch.setenv("AUDIT_HASH_KEY", GOOD)
    get_settings.cache_clear()
    with pytest.raises(SettingsError) as info:
        get_settings()
    assert "jwt_secret: Field required" in str(info.value)
    assert GOOD[:5] not in str(info.value) and info.value.__cause__ is None


# ---- at rest --------------------------------------------------------------------

def test_envelope_encryption_round_trip_and_tamper_detection():
    kek = ear.new_kek()
    data = b'{"actor":"chw_amina","action":"triage.submit"}\n' * 50
    blob = ear.encrypt_bytes(data, kek, aad="audit.jsonl")
    assert data not in blob and b"chw_amina" not in blob
    assert ear.decrypt_bytes(blob, kek) == data
    with pytest.raises(ear.DecryptionError):
        ear.decrypt_bytes(blob, ear.new_kek())                       # wrong KEK
    tampered = bytearray(blob)
    tampered[-5] ^= 1
    with pytest.raises(ear.DecryptionError):
        ear.decrypt_bytes(bytes(tampered), kek)                      # flipped bit in the ciphertext
    header_swapped = blob.replace(b'"aad": "audit.jsonl"', b'"aad": "other.jsonl"')
    with pytest.raises(ear.DecryptionError):
        ear.decrypt_bytes(header_swapped, kek)                       # header is authenticated too


def test_each_file_gets_its_own_data_key():
    kek = ear.new_kek()
    a, b = ear.encrypt_bytes(b"same", kek), ear.encrypt_bytes(b"same", kek)
    assert a != b


# ---- secrets not in git -----------------------------------------------------------

def test_check_gitignore_passes_on_this_repo():
    p = subprocess.run([sys.executable, "-m", "security.check_gitignore"], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout


def test_secret_scanner_finds_real_looking_values_and_allows_placeholders():
    from security.check_gitignore import placeholders, scan_text

    allowed = placeholders()
    assert scan_text("JWT_SECRET=" + GOOD, allowed)
    assert scan_text("Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9." * 3, allowed)
    assert not scan_text("JWT_SECRET=change-me-to-a-long-random-string", allowed)
    assert not scan_text("JWT_SECRET: ${JWT_SECRET:?set it}", allowed)
