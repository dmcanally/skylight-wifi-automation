from __future__ import annotations

import asyncio

import httpx
from starlette.testclient import TestClient
from tests.uiutil import PASSWORD, SETUP_TOKEN, USERNAME, build, csrf_from, db_rows


def test_first_page_is_setup_until_an_admin_exists(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        home = client.get("/", follow_redirects=False)
        login = client.get("/login", follow_redirects=False)
        protected = client.get("/audit", follow_redirects=False)
        health = client.get("/healthz")
        static = client.get("/static/app.css")
        setup = client.get("/setup", follow_redirects=False)
        followed = client.get("/", follow_redirects=True)
    assert home.status_code == 303
    assert home.headers["location"].endswith("/setup")
    assert login.status_code == 303
    assert login.headers["location"].endswith("/setup")
    assert protected.status_code == 303
    assert protected.headers["location"].endswith("/setup")
    assert health.status_code == 200
    assert static.status_code == 200
    assert setup.status_code == 200
    assert followed.status_code == 200
    assert followed.url.path == "/setup"
    assert "Create the admin" in followed.text

    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        created = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert client.get("/setup", follow_redirects=False).status_code == 404
        logged_out = client.get("/", follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"].endswith("/login")


def test_two_setup_posts_create_one_admin(tmp_path):
    bundle = build(tmp_path)

    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=bundle.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            pages = await asyncio.gather(client.get("/setup"), client.get("/setup"))
            tokens = [csrf_from(page.text) for page in pages]

            async def post(token: str) -> int:
                response = await client.post(
                    "/setup",
                    data={
                        "csrf_token": token,
                        "setup_token": SETUP_TOKEN,
                        "username": USERNAME,
                        "password": PASSWORD,
                        "password2": PASSWORD,
                    },
                    headers={"origin": "http://testserver"},
                )
                return response.status_code

            return list(await asyncio.gather(post(tokens[0]), post(tokens[1])))

    assert sorted(asyncio.run(run())) == [303, 404]
    assert len(db_rows(bundle.store.path, "SELECT username FROM admin")) == 1
