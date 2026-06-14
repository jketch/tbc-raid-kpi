"""Tests for wcl_client — the OAuth + GraphQL transport leaf, with an injected fake `requests`
and a no-op sleep. The partial-payload return
that wcl_fetchers._report defends against ORIGINATES here. Cases: gql success; 429 honors
Retry-After then retries; a 5xx / network blip retries then succeeds; a GraphQL `errors` payload
WITH usable data returns the partial (no retry); a null payload raises RuntimeError (no retry);
get_token success + 5xx-then-success + bounded-retry exhaustion."""
import contextlib, io, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import wcl_client as wc


class _HTTPError(Exception):
    pass


class _ConnErr(Exception):       # a non-RuntimeError network blip → gql RETRIES it
    pass


class FakeResp:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code, self._json = status_code, (json_data or {})
        self.headers, self.text = (headers or {}), text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPError(f"HTTP {self.status_code}")


class FakeRequests:
    """Pops queued responses per .post(); repeats the last once exhausted. A queued Exception
    instance is raised instead of returned (simulates a network error)."""
    class exceptions:            # get_token references _req.exceptions.HTTPError for 5xx
        HTTPError = _HTTPError

    def __init__(self, responses):
        self._q = list(responses)
        self._last = None
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self._q:
            self._last = self._q.pop(0)
        r = self._last
        if isinstance(r, Exception):
            raise r
        return r


class FakeTime:
    def __init__(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_req, self._orig_time = wc._req, wc.time
        self.time = FakeTime()
        wc.time = self.time

    def tearDown(self):
        wc._req, wc.time = self._orig_req, self._orig_time

    def install(self, responses):
        self.req = FakeRequests(responses)
        wc._req = self.req
        return self.req

    def _gql(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return wc.gql("TOKEN", "query {}", **kw)

    def _token(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return wc.get_token("id", "secret", **kw)


class TestGql(_Base):
    def test_success_returns_payload(self):
        self.install([FakeResp(200, {"data": {"x": 1}})])
        self.assertEqual(self._gql(), {"x": 1})
        self.assertEqual(len(self.req.calls), 1)

    def test_429_honors_retry_after_then_succeeds(self):
        self.install([FakeResp(429, headers={"Retry-After": "7"}),
                      FakeResp(200, {"data": {"ok": 1}})])
        self.assertEqual(self._gql(), {"ok": 1})
        self.assertEqual(self.time.sleeps, [7])            # honored the header, not the default
        self.assertEqual(len(self.req.calls), 2)

    def test_persistent_429_eventually_raises(self):
        self.install([FakeResp(429, headers={"Retry-After": "1"})])   # repeats forever
        with self.assertRaises(_HTTPError):
            self._gql(retries=2)                           # bounded — does NOT loop forever

    def test_5xx_retries_then_succeeds(self):
        self.install([FakeResp(503), FakeResp(200, {"data": {"ok": 1}})])
        self.assertEqual(self._gql(), {"ok": 1})
        self.assertEqual(len(self.req.calls), 2)
        self.assertEqual(self.time.sleeps, [2])            # 2**1 backoff

    def test_network_error_retries_then_succeeds(self):
        self.install([_ConnErr("reset"), FakeResp(200, {"data": {"ok": 1}})])
        self.assertEqual(self._gql(), {"ok": 1})
        self.assertEqual(len(self.req.calls), 2)

    def test_graphql_errors_with_data_returns_partial_no_retry(self):
        self.install([FakeResp(200, {"data": {"a": 1}, "errors": [{"message": "boom"}]})])
        self.assertEqual(self._gql(), {"a": 1})            # partial honored
        self.assertEqual(len(self.req.calls), 1)           # deterministic → NOT retried

    def test_null_payload_with_errors_raises_no_retry(self):
        self.install([FakeResp(200, {"data": None, "errors": [{"message": "boom"}]})])
        with self.assertRaises(RuntimeError):
            self._gql(retries=5)
        self.assertEqual(len(self.req.calls), 1)           # RuntimeError re-raised, not retried


class TestGetToken(_Base):
    def test_success_returns_access_token(self):
        self.install([FakeResp(200, {"access_token": "TKO"})])
        self.assertEqual(self._token(), "TKO")

    def test_5xx_then_success(self):
        self.install([FakeResp(503), FakeResp(200, {"access_token": "TKO"})])
        self.assertEqual(self._token(), "TKO")
        self.assertEqual(len(self.req.calls), 2)
        self.assertEqual(self.time.sleeps, [3])            # 3 * 2**0

    def test_exhausts_retries_then_raises(self):
        self.install([FakeResp(401, text="nope")])         # repeats; auth never recovers
        with self.assertRaises(_HTTPError):
            self._token(retries=3)
        self.assertEqual(len(self.req.calls), 3)           # tried exactly `retries` times


if __name__ == "__main__":
    unittest.main()
