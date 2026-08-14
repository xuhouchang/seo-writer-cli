"""Real-provider adapters tested only against a local HTTP stub."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from seo_writer.errors import UsageError
from seo_writer.models import PolicyYaml
from seo_writer.providers import build_providers
from seo_writer.providers.real import DataForSEOProvider, HttpWebFetchProvider, RedditProvider


class _ProviderStub(BaseHTTPRequestHandler):
    server_version = "SEOWriterProviderStub/1.0"

    def log_message(self, *_args) -> None:
        return

    def _send(self, payload: object, status: int = 200, content_type: str = "application/json") -> None:
        body = (
            json.dumps(payload).encode("utf-8")
            if content_type == "application/json"
            else str(payload).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        path = urlparse(self.path).path
        if path == "/token":
            assert self.headers.get("Authorization", "").startswith("Basic ")
            self._send({"access_token": "stub-access-token", "token_type": "bearer"})
            return
        assert self.headers.get("Authorization", "").startswith("Basic ")
        tasks = json.loads(body)[0]
        if path.endswith("search_volume/live"):
            result = [
                {"keyword": keyword, "search_volume": 123, "competition": 0.2, "cpc": 1.5}
                for keyword in tasks["keywords"]
            ]
        elif path.endswith("keyword_suggestions/live"):
            result = [{"keyword": f"{tasks['keywords'][0]} workflow"}]
        elif path.endswith("serp/google/organic/live/advanced"):
            result = [
                {
                    "items": [
                        {
                            "type": "organic",
                            "rank_group": 1,
                            "url": f"{self.server.base_url}/page",
                            "title": "Stub page",
                        },
                        {"type": "people_also_ask", "items": [{"title": "What is the workflow?"}]},
                        {"type": "related_searches", "items": [{"title": "workflow tools"}]},
                        {"type": "ai_overview", "rank_group": 0},
                    ]
                }
            ]
        else:
            self._send({"status_code": 404, "status_message": "not found"}, status=404)
            return
        self._send({"status_code": 20000, "tasks": [{"status_code": 20000, "cost": 0.01, "result": result}]})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/search.json":
            self._send(
                {
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "permalink": "/r/test/comments/abc123/stub/",
                                    "subreddit": "test",
                                    "title": "A real discussion",
                                }
                            }
                        ]
                    }
                }
            )
            return
        if path == "/comments/abc123.json":
            self._send(
                [
                    {
                        "data": {
                            "children": [
                                {"data": {"title": "A real discussion", "selftext": "A problem"}}
                            ]
                        }
                    },
                    {"data": {"children": [{"kind": "t1", "data": {"body": "A useful counterexample"}}]}},
                ]
            )
            return
        if path == "/2.3/search/advanced":
            assert query["site"] == ["stackoverflow"]
            self._send({"items": [{"title": "Stack question", "link": f"{self.server.base_url}/stack"}]})
            return
        if path == "/page":
            self._send(
                "<html><head><title>Stub page</title></head><body><h1>Heading</h1>"
                "<p>Evidence paragraph.</p></body></html>",
                content_type="text/html",
            )
            return
        self._send({"status_code": 404, "status_message": "not found"}, status=404)


@pytest.fixture
def provider_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderStub)
    server.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _write_secrets(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".secrets.yaml").write_text(
        yaml.safe_dump(
            {
                "dataforseo": {"login": "stub-login", "password": "stub-password"},
                "reddit": {"client_id": "stub-client", "client_secret": "stub-secret"},
            }
        ),
        encoding="utf-8",
    )


def test_real_dataforseo_reddit_and_webfetch_adapters_use_stub(provider_stub, tmp_path: Path) -> None:
    _write_secrets(tmp_path)
    base = provider_stub.base_url
    dfs = DataForSEOProvider("stub", tmp_path, base_url=base)
    metrics = dfs.search_volume(["seo workflow"])
    assert metrics.data["seo workflow"]["volume"] == 123
    assert dfs.related("seo workflow").data == ["seo workflow workflow"]
    serp = dfs.query("seo workflow", "head")
    assert serp.data["query_method"] == "dataforseo_api"
    assert serp.data["aio_visible"] is True
    assert serp.data["paa"] == ["What is the workflow?"]

    reddit = RedditProvider(
        "stub",
        tmp_path,
        oauth_base_url=base,
        token_url=f"{base}/token",
        stackexchange_base_url=base,
    )
    candidates = reddit.discover("seo workflow")
    assert candidates.data[0]["platform"] == "Reddit / r/test"
    thread = reddit.read_thread(candidates.data[0]["url"], candidates.data[0]["platform"])
    assert thread.data["fetch_method"] == "reddit_api"
    second = reddit.second_platform_thread()
    assert second.data["platform"].startswith("Stack Exchange")

    page = HttpWebFetchProvider().fetch(f"{base}/page")
    assert page.data["fetch_method"] == "web_fetch"
    assert page.data["title"] == "Stub page"


def test_real_policy_build_requires_user_credentials(tmp_path: Path, provider_stub) -> None:
    policy = PolicyYaml.model_validate(
        {
            "brand": "acme",
            "providers": {
                "keyword": {"name": "dataforseo", "profile": "real", "base_url": provider_stub.base_url},
                "serp": {"name": "dataforseo", "profile": "real", "base_url": provider_stub.base_url},
                "webfetch": {"name": "http", "profile": "real"},
                "community": {"name": "reddit", "profile": "real", "base_url": provider_stub.base_url},
                "llm": {"name": "mock", "profile": "mock-llm"},
            },
        }
    )
    with pytest.raises(UsageError, match="providers configure"):
        build_providers(policy, data_dir=tmp_path)

    _write_secrets(tmp_path)
    providers = build_providers(policy, data_dir=tmp_path)
    assert isinstance(providers["keyword"], DataForSEOProvider)
    assert isinstance(providers["community"], RedditProvider)
    assert isinstance(providers["webfetch"], HttpWebFetchProvider)
    assert "stub-password" not in repr(providers)
