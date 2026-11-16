#!/usr/bin/env python3
"""End-to-end check of a running stack, stdlib only (used locally and in CI).

Drives the real flows instead of a browser:
  1. every service answers through the proxy
  2. Mailpit is gated by Authentik forward auth
  3. demo-user signs into Rallly through Authentik (OIDC) and gets a session
     carrying the IdP's e-mail
  4. demo-denied (no group) is refused by Authentik's application policy
  5. demo-user (rallly-users) is refused on Mailpit, demo-admin is let in
  6. akadmin creates a single-use invitation; an external user enrolls with
     it, lands in rallly-guests and signs into Rallly; the link then stops working

Reads hosts and passwords from .env, overridable from the environment.
Usage: scripts/smoke-test.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import secrets
import sys
import time
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
DEMO_PASSWORD = ENV.get("DEMO_PASSWORD") or sys.exit("DEMO_PASSWORD is not set")
ADMIN_PASSWORD = ENV.get("AUTHENTIK_BOOTSTRAP_PASSWORD") or sys.exit("AUTHENTIK_BOOTSTRAP_PASSWORD is not set")


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


def run_authentik_flow(client: Client, flow_url: str, username: str = "", password: str = "", prompt: dict | None = None) -> dict:
    """Executes an Authentik flow page through its JSON executor API.

    Answers identification/password stages with username/password and prompt
    stages with `prompt`; returns the first challenge it cannot answer (a
    redirect, an access-denied stage...).
    """
    parsed = urllib.parse.urlparse(flow_url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    api = f"{AUTHENTIK}/api/v3/flows/executor/{slug}/?query={urllib.parse.quote(parsed.query)}"
    _, _, challenge = client.follow(api)
    for _ in range(10):
        component = (challenge or {}).get("component")
        if component == "ak-stage-identification":
            answer = {"component": component, "uid_field": username}
            if challenge.get("password_fields"):
                answer["password"] = password
        elif component == "ak-stage-password":
            answer = {"component": component, "password": password}
        elif component == "ak-stage-prompt" and prompt is not None:
            answer = {"component": component, **prompt}
        else:
            return challenge or {}
        status, headers, challenge = client.request("POST", api, answer)
        if status in (301, 302, 303) and headers.get("Location"):
            # The executor answers a POST with a redirect to its own GET.
            _, _, challenge = client.follow(urllib.parse.urljoin(api, headers["Location"]))
    raise RuntimeError(f"flow {slug} did not finish: {challenge}")


def login_to_authentik(client: Client, username: str, password: str = DEMO_PASSWORD) -> None:
    url, _, _ = client.follow(f"{AUTHENTIK}/")
    result = run_authentik_flow(client, url, username, password)
    assert result.get("component") == "xak-flow-redirect", f"login of {username} failed: {result}"


def authorize(client: Client, url: str):
    """Follows an OAuth authorize URL; returns None when Authentik denies it."""
    url, status, payload = client.follow(url)
    if url.startswith(AUTHENTIK) and "/if/flow/" in url:
        # The implicit-consent authorization flow.
        result = run_authentik_flow(client, url)
        if result.get("component") == "ak-stage-access-denied":
            return None
        assert result.get("component") == "xak-flow-redirect", result
        url, status, payload = client.follow(result["to"])
    if url.startswith(AUTHENTIK) and isinstance(payload, str) and "Request has been denied" in payload:
        # A failed application policy renders the denial page on /authorize itself.
        return None
    return url, status, payload


def rallly_sso(username: str, password: str = DEMO_PASSWORD, client: Client | None = None) -> tuple[str, object]:
    """Returns ("session", user) on success, ("denied", None) on refusal.

    Pass a client already signed into Authentik to skip the login step.
    """
    signed_in = client is not None
    client = client or Client()
    status, _, payload = client.request(
        "POST",
        f"{RALLLY}/api/better-auth/sign-in/oauth2",
        {"providerId": "oidc", "callbackURL": "/"},
        {"Origin": RALLLY},
    )
    assert status == 200 and payload.get("url", "").startswith(AUTHENTIK), (status, payload)

    if not signed_in:
        login_to_authentik(client, username, password)
    landed = authorize(client, payload["url"])
    if landed is None:
        return "denied", None
    url, status, _ = landed
    assert url.startswith(RALLLY), f"ended at {url} ({status})"

    _, _, session = client.request("GET", f"{RALLLY}/api/better-auth/get-session")
    assert session and session.get("user"), f"no Rallly session after callback: {session}"
    return "session", session["user"]


def mail_access(username: str) -> int:
    client = Client()
    login_to_authentik(client, username)
    landed = authorize(client, f"{MAIL}/")
    if landed is None:
        return 403
    url, status, _ = landed
    return status if url.startswith(MAIL) else 0


def create_invitation(email: str) -> str:
    """Signs in as akadmin and creates a single-use invitation; returns its token."""
    admin = Client()
    login_to_authentik(admin, "akadmin", ADMIN_PASSWORD)
    _, _, flow = admin.request("GET", f"{AUTHENTIK}/api/v3/flows/instances/devdocker-invitation/")
    status, _, invitation = admin.request(
        "POST",
        f"{AUTHENTIK}/api/v3/stages/invitation/invitations/",
        {
            "name": f"smoke-{secrets.token_hex(4)}",
            "flow": flow["pk"],
            "single_use": True,
            "fixed_data": {"email": email},
        },
        {"Referer": f"{AUTHENTIK}/"},
    )
    assert status == 201, (status, invitation)
    return invitation["pk"]


def enroll(token: str, username: str, email: str, password: str) -> tuple[Client, dict]:
    client = Client()
    result = run_authentik_flow(
        client,
        f"{AUTHENTIK}/if/flow/devdocker-invitation/?itoken={token}",
        prompt={
            "username": username,
            "name": "Smoke Guest",
            "email": email,
            "password": password,
            "password_repeat": password,
        },
    )
    return client, result


def check(name: str, ok: bool, detail: object = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def wait_for_blueprint(client: Client, timeout: float = 240) -> None:
    """`compose up --wait` returns once containers are healthy, but the worker
    applies the blueprint and the embedded outpost loads the Mailpit provider
    a little later. Both are observable from outside: poll until they are."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        discovery_status, _, _ = client.request("GET", f"{AUTHENTIK}/application/o/rallly/.well-known/openid-configuration")
        mail_status, _, _ = client.request("GET", f"{MAIL}/")
        if discovery_status == 200 and mail_status == 302:
            return
        time.sleep(5)
    print(f"blueprint still not applied after {timeout:.0f}s", file=sys.stderr)


def main() -> int:
    anon = Client()
    results = []
    wait_for_blueprint(anon)

    status, _, _ = anon.request("GET", f"{AUTHENTIK}/-/health/ready/")
    results.append(check("authentik ready through the proxy", status in (200, 204), status))

    _, _, discovery = anon.request("GET", f"{AUTHENTIK}/application/o/rallly/.well-known/openid-configuration")
    issuer = discovery.get("issuer") if isinstance(discovery, dict) else None
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

    guest = f"guest-{secrets.token_hex(4)}"
    guest_email = f"{guest}@external.test"
    guest_password = secrets.token_urlsafe(18)
    token = create_invitation(guest_email)
    client, result = enroll(token, guest, guest_email, guest_password)
    results.append(check("external user enrolls with the invitation", result.get("component") == "xak-flow-redirect", result))

    outcome, user = rallly_sso(guest, client=client)
    results.append(check("invited guest signs into Rallly", outcome == "session" and user.get("email") == guest_email, user))

    _, result = enroll(token, f"{guest}-again", f"again-{guest_email}", guest_password)
    results.append(check("single-use invitation cannot be replayed", result.get("component") != "xak-flow-redirect", result))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
