"""Offline research contract: HTML parsing and Jarvis's real save/readback path."""
import json
from urllib.parse import quote

import pytest
import requests

from agent_service.contracts import Observation, utc_now
from agent_service.research import ResearchError, _candidate, main, run_research, search_public


class Response:
    def __init__(self, html, *, status=200, mime="text/html", location=None):
        self.content = html.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": mime}
        if location:
            self.headers["Location"] = location
        self.closed = False

    def iter_content(self, _size):
        yield self.content

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        self.closed = True


class Browser:
    def __init__(self):
        self.visited = []

    def read(self, url, should_stop=lambda: False):
        self.visited.append(url)
        assert not should_stop()
        return Observation(url, url, "Sample", "Verified source text", utc_now())


def test_search_public_accepts_bounded_public_results():
    target = "https://www.example.org/article"
    html = f'<a class="result__a" href="https://duckduckgo.com/l/?uddg={quote(target, safe="")}">A</a>'
    session = Session([Response(html)])
    assert search_public("test topic", 2, session=session) == [target]
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert _candidate("/l/?uddg=" + quote(target, safe="")) == target


def test_search_ranks_relevant_article_over_earlier_generic_index_and_deduplicates():
    index = "https://docs.example.org/zh/3/"
    article = "https://docs.example.org/zh/3/library/text-encoding.html"
    html = (f'<a class="result__a" href="{index}">Example documentation</a>'
            f'<a class="result__a" href="{article}">Text file encoding guide</a>'
            f'<a class="result__a" href="{article}#section">Text file encoding guide</a>')
    session = Session([Response(html)])
    assert search_public("text file encoding site:docs.example.org", 2, session=session) == [article, index]
    assert len(session.calls) == 1


def test_search_preserves_engine_order_for_equal_scores():
    first = "https://example.org/alpha"
    second = "https://example.org/beta"
    html = (f'<a class="result__a" href="{first}">Topic guide</a>'
            f'<a class="result__a" href="{second}">Topic guide</a>')
    assert search_public("topic", 2, session=Session([Response(html)])) == [first, second]


def test_search_limits_ranking_to_twenty_valid_unique_candidates():
    invalid = '<a class="result__a" href="http://127.0.0.1/private">Invalid</a>'
    ordinary = "".join(f'<a class="result__a" href="https://example.org/item-{i}">Item</a>'
                       for i in range(20))
    late = '<a class="result__a" href="https://example.org/rare-topic">Rare topic</a>'
    selected = search_public("rare topic", 1, session=Session([Response(invalid + ordinary + late)]))
    assert selected != ["https://example.org/rare-topic"]
    assert len(selected) == 1


def test_search_uses_second_public_engine_after_verification_page():
    first = Response("verify you are human captcha")
    second = Response('<li class="b_algo"><h2><a href="https://example.org/fact">Fact</a></h2></li>')
    session = Session([first, second])
    assert search_public("facts", session=session) == ["https://example.org/fact"]
    assert len(session.calls) == 2


def test_search_rejects_private_and_missing_results():
    html = '<a class="result__a" href="http://127.0.0.1/admin">A</a>'
    html += '<a class="result__a" href="https://localhost.local/test">B</a>'
    session = Session([Response(html), Response("")])
    with pytest.raises(ResearchError, match="search_unavailable"):
        search_public("facts", session=session)
    assert _candidate("https://127.0.0.1/private") is None


def test_search_stream_deadline_closes_response_and_keeps_injected_session():
    response = Response('<a class="result__a" href="https://example.org/">A</a>')
    session = Session([response])
    ticks = iter((0, 0, 0, 21))
    with pytest.raises(ResearchError, match="search_timeout"):
        search_public("facts", session=session, clock=lambda: next(ticks))
    assert response.closed is True
    assert session.closed is False


def test_owned_search_session_disables_environment_and_closes(monkeypatch):
    session = Session([Response("captcha"), Response("captcha")])
    session.trust_env = True
    monkeypatch.setattr("agent_service.research.requests.Session", lambda: session)
    with pytest.raises(ResearchError, match="search_unavailable"):
        search_public("facts")
    assert session.trust_env is False
    assert session.closed is True


def test_search_rejects_http_redirect_and_non_html():
    session = Session([Response("", status=302), Response("plain", mime="text/plain")])
    with pytest.raises(ResearchError, match="http_302.*content_type"):
        search_public("facts", session=session)
    assert all(row[1]["allow_redirects"] is False for row in session.calls)


def test_owned_session_uses_checked_proxy_without_netrc(monkeypatch):
    session = Session([Response('<a class="result__a" href="https://example.org/doc">Doc</a>')])
    monkeypatch.setattr("agent_service.research.requests.Session", lambda: session)
    monkeypatch.setattr("agent_service.research.getproxies", lambda: {"https": "http://127.0.0.1:10809"})
    assert search_public("pathlib") == ["https://example.org/doc"]
    assert session.trust_env is False
    assert session.calls[0][1]["proxies"] == {"https": "http://127.0.0.1:10809"}
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.closed is True


def test_bing_region_redirect_only_once_and_same_supplier():
    session = Session([Response("", status=503),
                       Response("", status=302, location="https://cn.bing.com/search?q=pathlib"),
                       Response('<li class="b_algo"><h2><a href="https://docs.python.org/3/library/pathlib.html">Docs</a></h2></li>')])
    assert search_public("pathlib", session=session) == ["https://docs.python.org/3/library/pathlib.html"]
    assert session.calls[-1][0] == "https://cn.bing.com/search"
    assert len(session.calls) == 3


def test_proxy_credentials_are_rejected_without_network(monkeypatch):
    monkeypatch.setattr("agent_service.research.getproxies", lambda: {"https": "http://user:secret@127.0.0.1:10809"})
    with pytest.raises(ResearchError, match="proxy_invalid"):
        search_public("pathlib")


def test_bing_redirect_to_other_host_is_not_followed():
    session = Session([Response("", status=503),
                       Response("", status=302, location="https://other.example/search")])
    with pytest.raises(ResearchError, match="bing:http_302"):
        search_public("pathlib", session=session)
    assert len(session.calls) == 2


def test_research_saves_real_worker_excerpts_and_readback(tmp_path):
    browser = Browser()
    result = run_research("sample research", tmp_path,
                          search=lambda _topic, _limit: ["https://example.org/article"], browser=browser)
    assert result["ok"] is True
    assert result["state"] == "succeeded"
    assert result["readback_sha256"]
    assert browser.visited == ["https://example.org/article"]
    from pathlib import Path
    content = Path(result["artifact"]).read_text(encoding="utf-8")
    assert "Verified source text" in content
    assert "https://example.org/article" in content
    assert "未经模型归纳" in content


def test_research_rejects_invalid_search_results_before_persistence(tmp_path):
    with pytest.raises(ResearchError, match="search_results_invalid"):
        run_research("topic", tmp_path, search=lambda _topic, _limit: ["https://127.0.0.1/private"])
    assert list(tmp_path.iterdir()) == []


def test_cli_attempt_log_records_success_before_and_after_run(tmp_path, monkeypatch, capsys):
    def fake_run(topic, root, max_sources):
        logs = list((tmp_path / "attempts").glob("*.jsonl"))
        assert len(logs) == 1
        started = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
        assert len(started) == 1 and started[0]["event"] == "started"
        assert started[0]["topic"] == topic and started[0]["max_sources"] == max_sources
        return {"ok": True, "state": "succeeded", "artifact": str(tmp_path / "new.md")}

    monkeypatch.setattr("agent_service.research.run_research", fake_run)
    assert main(["pathlib", "--root", str(tmp_path), "--max-sources", "2"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    rows = [json.loads(line) for line in open(output["attempt_log"], encoding="utf-8")]
    assert [row["event"] for row in rows] == ["started", "result"]
    assert rows[1]["result"]["ok"] is True


def test_cli_attempt_log_records_known_failure_without_raw_error(tmp_path, monkeypatch, capsys):
    def fail(*_args):
        raise ResearchError("search_unavailable:secret")

    monkeypatch.setattr("agent_service.research.run_research", fail)
    assert main(["pathlib", "--root", str(tmp_path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "search_unavailable"
    rows = [json.loads(line) for line in open(output["attempt_log"], encoding="utf-8")]
    assert [row["event"] for row in rows] == ["started", "result"]
    assert rows[1]["result"]["error"] == "search_unavailable"
    assert "secret" not in json.dumps(rows)


def test_cli_attempt_log_records_cancel_and_unexpected_type(tmp_path, monkeypatch, capsys):
    def cancel(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("agent_service.research.run_research", cancel)
    assert main(["pathlib", "--root", str(tmp_path)]) == 130
    cancelled = json.loads(capsys.readouterr().out)
    rows = [json.loads(line) for line in open(cancelled["attempt_log"], encoding="utf-8")]
    assert rows[-1]["result"]["error"] == "cancelled"

    def unexpected(*_args):
        raise RuntimeError("private error detail")

    monkeypatch.setattr("agent_service.research.run_research", unexpected)
    assert main(["pathlib", "--root", str(tmp_path)]) == 1
    failed = json.loads(capsys.readouterr().out)
    rows = [json.loads(line) for line in open(failed["attempt_log"], encoding="utf-8")]
    assert rows[-1]["result"]["error"] == "unexpected_RuntimeError"
    assert "private error detail" not in json.dumps(rows)


def test_four_bing_results_require_explicit_comparison_contract():
    urls=[f'https://example.org/page-{i}' for i in range(5)]
    html=''.join(f'<li class="b_algo"><h2><a href="{url}">Topic</a></h2></li>' for url in urls)
    session=Session([Response(html)])
    assert search_public('topic',4,session=session,provider='bing',comparison=True)==urls[:4]
    assert len(session.calls)==1
    for kwargs in ({}, {'provider':'bing'}, {'comparison':True}):
        denied=Session([])
        with pytest.raises(ResearchError):
            search_public('topic',4,session=denied,**kwargs)
        assert not denied.calls
    with pytest.raises(ResearchError,match='source_count_invalid'):
        search_public('topic',5,session=Session([]),provider='bing',comparison=True)
