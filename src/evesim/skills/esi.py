"""EVE SSO login (OAuth2 authorization code + PKCE) and ESI reads.

One-time setup: register an application at https://developers.eveonline.com/
with callback ``http://localhost:8642/callback`` and the scopes below, then
export its client ID as ``EVESIM_CLIENT_ID`` (or pass ``--client-id``). PKCE
means no client secret is needed or stored.

Tokens are kept in ``~/.config/evesim/tokens.json`` (mode 600), keyed by
character ID; the access token is refreshed automatically.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from evesim.data.store import StaticData
from evesim.skills.profile import SkillProfile, from_dict

SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"
ESI_BASE = os.environ.get("EVESIM_ESI_BASE", "https://esi.evetech.net/latest")
# Only what evesim reads. Every scope requested must also be enabled on the SSO application,
# or EVE rejects the login with "invalid_scope".
SCOPES = ["esi-skills.read_skills.v1", "esi-clones.read_implants.v1"]
FITTINGS_SCOPE = "esi-fittings.write_fittings.v1"  # for save_fitting; opt in with login(save_fittings=True)
DEFAULT_PORT = 8642
USER_AGENT = "evesim (https://github.com/HarryZ4127/eve_ship_sim)"


def token_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "evesim" / "tokens.json"


def _load_tokens() -> dict:
    p = token_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save_tokens(tokens: dict):
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _client_id(client_id: str | None) -> str:
    cid = client_id or os.environ.get("EVESIM_CLIENT_ID")
    if not cid:
        raise RuntimeError("no SSO client ID: register an app at https://developers.eveonline.com/ "
                           "(callback http://localhost:8642/callback) and set EVESIM_CLIENT_ID")
    return cid


def _http(req: urllib.request.Request) -> dict | list:
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def decode_jwt(token: str) -> dict:
    """Read the JWT payload. The token comes straight from SSO over TLS and is only
    used to learn the character ID, so the signature is not verified here."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, challenge: str, state: str, port: int = DEFAULT_PORT,
                  scopes: list[str] = SCOPES) -> str:
    q = {"response_type": "code", "redirect_uri": f"http://localhost:{port}/callback", "client_id": client_id,
         "scope": " ".join(scopes), "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    return SSO_AUTHORIZE + "?" + urllib.parse.urlencode(q)


def _token_request(form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(SSO_TOKEN, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Host": "login.eveonline.com"})
    return _http(req)


def _store(tok: dict, client_id: str) -> dict:
    claims = decode_jwt(tok["access_token"])
    char_id = int(claims["sub"].split(":")[-1])
    rec = {"character_id": char_id, "name": claims.get("name", str(char_id)), "client_id": client_id,
           "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
           "expires_at": time.time() + float(tok.get("expires_in", 1200)) - 30}
    tokens = _load_tokens()
    tokens[str(char_id)] = rec
    _save_tokens(tokens)
    return rec


def login(client_id: str | None = None, port: int = DEFAULT_PORT, open_browser: bool = True,
          timeout: float = 300, save_fittings: bool = False) -> dict:
    """Run the browser login and store the tokens. Returns the token record."""
    cid = _client_id(client_id)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [None])[0] != state:
                result["error"] = "state mismatch"
            elif "code" in q:
                result["code"] = q["code"][0]
            else:
                result["error"] = q.get("error", ["no code"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Logged in, you can close this tab." if "code" in result else f"Login failed: {result.get('error')}"
            self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = authorize_url(cid, challenge, state, port, SCOPES + ([FITTINGS_SCOPE] if save_fittings else []))
    print(f"Open this URL to log in:\n{url}")
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + timeout
    while not result and time.time() < deadline:
        time.sleep(0.2)
    srv.shutdown()
    if "code" not in result:
        raise RuntimeError(f"SSO login failed: {result.get('error', 'timed out')}")
    tok = _token_request({"grant_type": "authorization_code", "code": result["code"],
                          "client_id": cid, "code_verifier": verifier})
    return _store(tok, cid)


def characters() -> list[dict]:
    return [{"character_id": v["character_id"], "name": v["name"]} for v in _load_tokens().values()]


def _record(character: str | None) -> dict:
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError("no logged-in characters; run `evesim login` first")
    if character is None:
        if len(tokens) > 1:
            names = ", ".join(v["name"] for v in tokens.values())
            raise RuntimeError(f"several characters logged in ({names}); pick one with esi:<name>")
        return next(iter(tokens.values()))
    for v in tokens.values():
        if character == str(v["character_id"]) or character.lower() == v["name"].lower():
            return v
    raise RuntimeError(f"character {character!r} is not logged in")


def access_token(character: str | None = None) -> tuple[str, int]:
    rec = _record(character)
    if time.time() >= rec["expires_at"]:
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": rec["refresh_token"],
                              "client_id": rec["client_id"]})
        rec = _store(tok, rec["client_id"])
    return rec["access_token"], rec["character_id"]


def esi_get(path: str, token: str | None = None):
    req = urllib.request.Request(ESI_BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return _http(req)


def fetch_profile(data: StaticData, character: str | None = None) -> SkillProfile:
    token, cid = access_token(character)
    skills = esi_get(f"/characters/{cid}/skills/", token)
    try:
        implants = esi_get(f"/characters/{cid}/implants/", token)
    except Exception:
        implants = []
    try:
        attrs = esi_get(f"/characters/{cid}/attributes/", token)
    except Exception:
        attrs = {}
    name = _record(str(cid))["name"]
    prof = from_dict(data, {"name": name, "skills": skills["skills"], "attributes": {
        k: attrs[k] for k in ("charisma", "intelligence", "memory", "perception", "willpower") if k in attrs}},
        name=name, source=f"esi:{cid}")
    prof.implants = [i for i in implants if i in data.types]
    return prof


def save_fitting(fit_eft_items: dict, character: str | None = None) -> dict:
    """POST a fitting (ESI fitting JSON) to the character's saved fittings. Needs a login
    with ``save_fittings=True`` (and that scope enabled on the application)."""
    token, cid = access_token(character)
    req = urllib.request.Request(f"{ESI_BASE}/characters/{cid}/fittings/", method="POST",
                                 data=json.dumps(fit_eft_items).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    return _http(req)
