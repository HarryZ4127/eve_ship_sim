"""SSO/ESI client tests with the network mocked out."""

import base64
import json
import time
import urllib.parse

import pytest

from evesim.skills import esi


def _jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.sig"


@pytest.fixture
def tokens_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_authorize_url_uses_pkce():
    verifier, challenge = esi._pkce()
    url = esi.authorize_url("abc", challenge, "st", 8642)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == ["abc"] and q["state"] == ["st"]
    assert "esi-skills.read_skills.v1" in q["scope"][0]
    assert len(verifier) >= 43


def test_token_store_and_refresh(tokens_dir, monkeypatch):
    calls = []

    def fake_http(req):
        calls.append(req)
        form = urllib.parse.parse_qs(req.data.decode())
        assert form["grant_type"] == ["refresh_token"]
        return {"access_token": _jwt({"sub": "CHARACTER:EVE:42", "name": "Pilot"}),
                "refresh_token": "r2", "expires_in": 1200}

    monkeypatch.setattr(esi, "_http", fake_http)
    rec = esi._store({"access_token": _jwt({"sub": "CHARACTER:EVE:42", "name": "Pilot"}),
                      "refresh_token": "r1", "expires_in": 1200}, "cid")
    assert rec["character_id"] == 42
    assert esi.characters() == [{"character_id": 42, "name": "Pilot"}]
    # Expire it and make sure access_token() refreshes.
    toks = esi._load_tokens()
    toks["42"]["expires_at"] = time.time() - 1
    esi._save_tokens(toks)
    tok, cid = esi.access_token("Pilot")
    assert cid == 42 and len(calls) == 1
    assert esi._load_tokens()["42"]["refresh_token"] == "r2"
    assert oct(esi.token_path().stat().st_mode & 0o777) == "0o600"


def test_fetch_profile(tokens_dir, monkeypatch, data):
    esi._store({"access_token": _jwt({"sub": "CHARACTER:EVE:7", "name": "Alt"}),
                "refresh_token": "r", "expires_in": 1200}, "cid")

    def fake_get(path, token=None):
        if path.endswith("/skills/"):
            return {"skills": [{"skill_id": 3300, "active_skill_level": 4, "trained_skill_level": 5}],
                    "total_sp": 1}
        return [9899999999]  # unknown implant ID is dropped

    monkeypatch.setattr(esi, "esi_get", fake_get)
    prof = esi.fetch_profile(data, "7")
    assert prof.name == "Alt" and prof.levels == {3300: 4} and prof.implants == []


def test_missing_client_id(monkeypatch):
    monkeypatch.delenv("EVESIM_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="developers.eveonline.com"):
        esi._client_id(None)
