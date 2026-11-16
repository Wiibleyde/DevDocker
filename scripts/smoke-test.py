#!/usr/bin/env python3
"""End-to-end check of a running stack, stdlib only (used locally and in CI).

Drives the real flows with the demo accounts instead of a browser:
  1. every service answers through the proxy
  2. Mailpit is gated by Authentik forward auth
  3. demo-user signs into Rallly through Authentik (OIDC) and gets a session
     carrying the IdP's e-mail
  4. demo-denied (no group) is refused by Authentik's application policy
  5. demo-user (rallly-users) is refused on Mailpit, demo-admin is let in

Reads hosts and DEMO_PASSWORD from .env, or from the environment.
Usage: scripts/smoke-test.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k in env or k.endswith("_HOST")})
    return env


ENV = load_env()
SCHEME = ENV.get("PUBLIC_SCHEME", "http")
RALLLY = f"{SCHEME}://{ENV.get('RALLLY_HOST', 'rallly.localhost')}"
AUTHENTIK = f"{SCHEME}://{ENV.get('AUTHENTIK_HOST', 'authentik.localhost')}"
MAIL = f"{SCHEME}://{ENV.get('MAIL_HOST', 'mail.localhost')}"
DOMAIN = ENV.get("DEMO_EMAIL_DOMAIN", "devdocker.test")
PASSWORD = ENV.get("DEMO_PASSWORD") or sys.exit("DEMO_PASSWORD is not set")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class Client:
    """A cookie-keeping HTTP client that never follows redirects by itself."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def request(self, method: str, url: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for cookie in self.jar:
            if cookie.name == "authentik_csrf":
                req.add_header("X-authentik-CSRF", cookie.value)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = self.opener.open(req, timeout=30)
        except urllib.error.HTTPError as err:  # 3xx/4xx/5xx all land here
            resp = err
        raw = resp.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace")
        return resp.status, resp.headers, payload

    def follow(self, url: str, limit: int = 15):
        """GET, following redirects by hand so cookies land on every hop."""
        for _ in range(limit):
            status, headers, payload = self.request("GET", url)
            location = headers.get("Location")
            if status not in (301, 302, 303, 307, 308) or not location:
                return url, status, payload
            url = urllib.parse.urljoin(url, location)
        raise RuntimeError(f"too many redirects ending at {url}")


def run_authentik_flow(client: Client, flow_url: str, username: str) -> dict:
    """Executes an Authentik flow page through its JSON executor API."""
    parsed = urllib.parse.urlparse(flow_url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    api = f"{AUTHENTIK}/api/v3/flows/executor/{slug}/?query={urllib.parse.quote(parsed.query)}"
    _, _, challenge = client.request("GET", api)
    for _ in range(10):
        component = challenge.get("component")
        if component == "ak-stage-identification":
            answer = {"component": component, "uid_field": username}
            if challenge.get("password_fields"):
                answer["password"] = PASSWORD
        elif component == "ak-stage-password":
            answer = {"component": component, "password": PASSWORD}
        elif component == "ak-stage-consent":
            answer = {"component": component, "token": challenge.get("token")}
        else:
            return challenge
        _, _, challenge = client.request("POST", api, answer)
    raise RuntimeError(f"flow {slug} did not finish: {challenge}")


def login_to_authentik(client: Client, username: str) -> None:
    url, _, _ = client.follow(f"{AUTHENTIK}/")
    result = run_authentik_flow(client, url, username)
    assert result.get("component") == "xak-flow-redirect", f"login of {username} failed: {result}"


def rallly_sso(username: str) -> tuple[str, object]:
    """Returns ("session", user) on success, ("denied", challenge) on refusal."""
    client = Client()
    status, _, payload = client.request(
        "POST",
        f"{RALLLY}/api/better-auth/sign-in/oauth2",
        {"providerId": "oidc", "callbackURL": "/"},
        {"Origin": RALLLY},
    )
    assert status == 200 and payload.get("url", "").startswith(AUTHENTIK), (status, payload)
    authorize = payload["url"]

    login_to_authentik(client, username)
    url, status, payload = client.follow(authorize)
    if url.startswith(AUTHENTIK) and "/if/flow/" in url:
        # Implicit-consent authorization flow, or the access-denied stage.
        result = run_authentik_flow(client, url, username)
        if result.get("component") == "ak-stage-access-denied":
            return "denied", result
        assert result.get("component") == "xak-flow-redirect", result
        url, status, payload = client.follow(result["to"])
    assert url.startswith(RALLLY), f"ended at {url} ({status})"

    _, _, session = client.request("GET", f"{RALLLY}/api/better-auth/get-session")
    assert session and session.get("user"), f"no Rallly session after callback: {session}"
    return "session", session["user"]


def mail_access(username: str) -> int:
    client = Client()
    login_to_authentik(client, username)
    url, status, payload = client.follow(f"{MAIL}/")
    if url.startswith(AUTHENTIK) and "/if/flow/" in url:
        result = run_authentik_flow(client, url, username)
        if result.get("component") == "ak-stage-access-denied":
            return 403
        url, status, payload = client.follow(result["to"])
    return status if url.startswith(MAIL) else 0


def check(name: str, ok: bool, detail: object = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def main() -> int:
    anon = Client()
    results = []

    status, _, _ = anon.request("GET", f"{AUTHENTIK}/-/health/ready/")
    results.append(check("authentik ready through the proxy", status in (200, 204), status))

    _, _, discovery = anon.request("GET", f"{AUTHENTIK}/application/o/rallly/.well-known/openid-configuration")
    issuer = (discovery or {}).get("issuer") if isinstance(discovery, dict) else None
    results.append(check("OIDC issuer is the public URL", issuer == f"{AUTHENTIK}/application/o/rallly/", issuer))

    status, _, _ = anon.request("GET", f"{RALLLY}/api/status")
    results.append(check("rallly answers through the proxy", status == 200, status))

    status, headers, _ = anon.request("GET", f"{MAIL}/")
    location = headers.get("Location", "")
    results.append(check("mailpit is gated by forward auth", status == 302 and location.startswith(AUTHENTIK), (status, location)))

    outcome, user = rallly_sso("demo-user")
    results.append(check("demo-user signs into Rallly via OIDC", outcome == "session" and user.get("email") == f"demo-user@{DOMAIN}", user))

    outcome, detail = rallly_sso("demo-denied")
    results.append(check("demo-denied is refused by the Rallly policy", outcome == "denied", detail))

    results.append(check("demo-user is refused on Mailpit", mail_access("demo-user") == 403))
    results.append(check("demo-admin reaches Mailpit", mail_access("demo-admin") == 200))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
