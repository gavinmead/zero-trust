#!/usr/bin/env python3
"""Sign in to Keycloak in your own browser, requesting the delegation scope.
(With standing delegation grants there is no consent screen; may_act appears only if a grant covers you.)

  python3 scripts/browser_consent.py [--scope "openid delegation:client:spark-connect"] [--no-open]

Starts a one-shot callback server on http://127.0.0.1:8765/callback (the lab-browser client's
redirect URI) and opens the authorization URL (auth code + PKCE, prompt=login so you can switch
users). You sign in and see the consent screen. After you approve, the page and terminal show
the token's claims (look for may_act). The raw access token is written to stdout, so it can be piped:

  ALICE_TOKEN=$(python3 scripts/browser_consent.py) ./demos/03-delegation/run.sh
"""
import argparse, base64, hashlib, html, http.server, json, os, secrets, sys, threading
import urllib.parse, urllib.request, webbrowser

BASE = os.environ.get("KC_URL", "http://keycloak.localhost") + "/realms/zt/protocol/openid-connect"
CLIENT_ID = os.environ.get("KC_CLIENT", "lab-browser")
HOST, PORT = "127.0.0.1", 8765
REDIRECT = f"http://{HOST}:{PORT}/callback"
SHOW = ("preferred_username", "groups", "aud", "azp", "scope", "may_act", "act", "exp")

def claims(tok):
    p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="openid delegation:client:spark-connect")
    ap.add_argument("--no-open", action="store_true", help="print the URL instead of opening a browser")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = BASE + "/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": a.scope,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login"})
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404); self.end_headers(); return
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if q.get("state") != state:
                    raise RuntimeError("state mismatch")
                if "error" in q:
                    raise RuntimeError(f"{q['error']}: {q.get('error_description', '')}")
                body = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": CLIENT_ID,
                    "code": q["code"], "redirect_uri": REDIRECT, "code_verifier": verifier}).encode()
                with urllib.request.urlopen(BASE + "/token", body) as r:
                    resp = json.load(r)
                result["token"] = resp["access_token"]
                c = claims(resp["access_token"])
                shown = {k: c[k] for k in SHOW if k in c}
                verdict = ("✅ may_act present: spark-connect may act on your behalf" if "may_act" in c else
                           "❌ no may_act: no standing delegation grant covers this user")
                page = (f"<h2>{html.escape(verdict)}</h2><pre>{html.escape(json.dumps(shown, indent=2))}</pre>"
                        "<p>You can close this tab; the token was handed back to the terminal.</p>")
                self.send_response(200)
            except Exception as e:  # show the error in the browser and terminal
                result["error"] = str(e)
                page = f"<h2>Login failed</h2><pre>{html.escape(str(e))}</pre>"
                self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(f"<html><body style='font-family:system-ui;max-width:52rem;margin:2rem auto'>{page}</body></html>".encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    srv = http.server.HTTPServer((HOST, PORT), Handler)
    print(f"Open this URL to sign in (lab users: see .secrets/keycloak.env):\n  {url}\n", file=sys.stderr)
    if not a.no_open:
        webbrowser.open(url)
    timer = threading.Timer(a.timeout, srv.shutdown); timer.start()
    srv.serve_forever(); timer.cancel()

    if "token" not in result:
        sys.exit(f"no token: {result.get('error', 'timed out waiting for the browser')}")
    c = claims(result["token"])
    print(json.dumps({k: c[k] for k in SHOW if k in c}, indent=2), file=sys.stderr)
    print(result["token"])

if __name__ == "__main__":
    main()
