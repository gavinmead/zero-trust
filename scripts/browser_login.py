#!/usr/bin/env python3
"""Scripted browser login against Keycloak (authorization code + PKCE), including the consent
screen. Lab/demo use: stands in for a person clicking through the login page.

  browser_login.py <username> <password> [scope]   → prints the access token on stdout

Defaults: client lab-browser, scope "openid delegation:client:spark-connect".
Consent screen text and the granted scope are printed on stderr.
"""
import base64, hashlib, html.parser, http.cookiejar, json, os, re, secrets, sys
import urllib.parse, urllib.request, urllib.error

BASE = os.environ.get("KC_URL", "http://keycloak.localhost") + "/realms/zt/protocol/openid-connect"
CLIENT_ID = os.environ.get("KC_CLIENT", "lab-browser")
REDIRECT = "http://127.0.0.1:8765/callback"

class Forms(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.forms = []; self.text = []
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "form":
            self.forms.append({"action": html.unescape(a.get("action", "")), "id": a.get("id"), "inputs": {}, "submits": []})
        elif tag in ("input", "button") and self.forms:
            if a.get("type") == "submit" or tag == "button":
                self.forms[-1]["submits"].append((a.get("name"), a.get("value")))
            elif a.get("name"):
                self.forms[-1]["inputs"][a["name"]] = a.get("value", "")
    def handle_data(self, d):
        if d.strip(): self.text.append(d.strip())

class LocalhostSecurePolicy(http.cookiejar.DefaultCookiePolicy):
    """Browsers treat http://*.localhost as a secure context and send `Secure` cookies to it
    (Keycloak marks its session cookies Secure). Python's cookiejar doesn't — mimic browsers."""
    def return_ok_secure(self, cookie, request):
        host = urllib.parse.urlparse(request.get_full_url()).hostname or ""
        return host == "localhost" or host.endswith(".localhost") or super().return_ok_secure(cookie, request)

class StopAtRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(REDIRECT):
            raise StopIteration(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def main():
    user, pw = sys.argv[1], sys.argv[2]
    scope = sys.argv[3] if len(sys.argv) > 3 else "openid delegation:client:spark-connect"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy=LocalhostSecurePolicy())), StopAtRedirect())
    auth = BASE + "/auth?" + urllib.parse.urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "response_type": "code", "scope": scope, "state": secrets.token_hex(8),
        "code_challenge": challenge, "code_challenge_method": "S256"})

    def submit(url, fields=None):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        try:
            with opener.open(url, data) as r:
                p = Forms(); p.feed(r.read().decode())
                for f in p.forms:          # resolve relative form actions against the page URL
                    f["action"] = urllib.parse.urljoin(r.geturl(), f["action"])
                return p, None
        except StopIteration as s:
            return None, str(s.args[0])
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code} from {url}: {e.read().decode()[:300]}")

    page, done = submit(auth)
    for _ in range(4):                    # login page → (consent page) → redirect with code
        if done: break
        if not page.forms:
            sys.exit("no form on page: " + " | ".join(page.text[:12]))
        f = page.forms[0]
        if "username" in f["inputs"] or f["id"] == "kc-form-login":
            fields = {**f["inputs"], "username": user, "password": pw}
        else:                             # consent screen: show what is being granted, then accept
            shown = [t for t in page.text if len(t) < 120 and not any(ch in t for ch in "{}/;")]
            print("consent screen: " + " | ".join(shown), file=sys.stderr)
            accept = next((s for s in f["submits"] if s[0] == "accept"), ("accept", "Yes"))
            fields = {**f["inputs"], accept[0]: accept[1] or "Yes"}
        page, done = submit(f["action"], fields)
    if not done:
        sys.exit("login did not complete: " + " | ".join(page.text[:15]))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(done).query)
    if "error" in q:
        sys.exit(f"authorization error: {q}")
    tok = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": q["code"][0],
                                  "redirect_uri": REDIRECT, "code_verifier": verifier}).encode()
    with urllib.request.urlopen(BASE + "/token", tok) as r:
        resp = json.load(r)
    print("granted scope: " + resp.get("scope", ""), file=sys.stderr)
    print(resp["access_token"])

if __name__ == "__main__":
    main()
