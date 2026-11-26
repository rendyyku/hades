import httpx
import pytest
import respx

from hades.http import Http, SourceError

URL = "https://api.example.test/x"


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, s):
        self.calls.append(s)


@pytest.fixture
async def http():
    sleep = FakeSleep()
    async with httpx.AsyncClient() as c:
        h = Http(c, rates={}, sleep=sleep)
        h.fake_sleep = sleep
        yield h


@respx.mock
async def test_get_json_retries_then_ok(http):
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"a": 1}),
    ])
    assert await http.get_json(URL) == {"a": 1}
    assert route.call_count == 2
    assert len(http.fake_sleep.calls) == 1


@respx.mock
async def test_get_json_html_body_raises(http):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(SourceError):
        await http.get_json(URL)


@respx.mock
async def test_get_json_404_no_retry(http):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(SourceError) as e:
        await http.get_json(URL)
    assert route.call_count == 1
    assert e.value.status == 404


@respx.mock
async def test_get_json_exhausted_retries(http):
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(SourceError):
        await http.get_json(URL, max_retries=2)
    assert route.call_count == 3


@respx.mock
async def test_user_agent_sent(http):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=[]))
    await http.get_json(URL)
    assert route.calls[0].request.headers["user-agent"].startswith("hades-signals/2.0")


@respx.mock
async def test_post_json(http):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await http.post_json(URL, json={"a": 1}) == {"ok": True}
    assert route.calls[0].request.content == b'{"a":1}'
