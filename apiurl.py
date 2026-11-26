"""Addresses of the other Shapes services: tidy up what was typed into an environment variable, and fall back to a second
(usually the public https://) address when the first one fails. The same file is in the API and both bots."""
import requests


def clean_url(value, default=""):
    """http://host:port with no trailing slash. Forgives a pasted 'NAME=http://...' (only the last http(s):// part is kept)
    and a bare 'host:port' (http:// is added)."""
    v = (value or "").strip().strip("\"'").strip()
    i = max(v.rfind("http://"), v.rfind("https://"))
    if i > 0:
        v = v[i:]
    elif i < 0 and v:
        v = "http://" + v.split("=")[-1].strip()
    return (v or default).rstrip("/")


class Link:
    """One service reachable on several addresses. The address that worked last time is tried first. A connection error, a
    timeout, a 403 or a 5xx from one address moves on to the next."""

    def __init__(self, *bases):
        seen = []
        for b in bases:
            if b and b not in seen:
                seen.append(b)
        self.bases = seen
        self.good = 0
        self.last_error = ""

    @property
    def base(self):
        return self.bases[self.good] if self.bases else ""

    def request(self, method, path, session=None, **kw):
        """A requests.Response (the last one seen when every address failed), or raises requests.RequestException."""
        http = session or requests
        order = [self.good] + [i for i in range(len(self.bases)) if i != self.good]
        last, err = None, None
        for i in order:
            try:
                r = http.request(method, f"{self.bases[i]}{path}", **kw)
            except requests.RequestException as e:
                err = e
                self.last_error = f"{self.bases[i]}: {type(e).__name__}"
                continue
            if r.status_code == 403 or r.status_code >= 502:
                last = r
                self.last_error = f"{self.bases[i]}: HTTP {r.status_code}"
                continue
            self.good = i
            self.last_error = ""
            return r
        if last is not None:
            return last
        raise err or requests.ConnectionError("no address configured")

    def ping(self, session=None, samples=3, timeout=8, path="/internal/ping"):
        """How long this server takes to reach the API, in ms: the fastest of a few requests on a warm connection, timed only until the
        API's answer starts to arrive (the body, JSON and any work in this program aren't counted). Any HTTP answer counts, even an
        error page: it still travelled the whole way. None when no address answers."""
        best = None
        for _ in range(max(1, samples) + 1):                    # the first request only opens the connection
            try:
                r = self.request("GET", path, session=session, timeout=timeout)
            except requests.RequestException:
                return None
            ms = r.elapsed.total_seconds() * 1000
            best = ms if best is None or ms < best else best
            if _ == 0:
                best = None                                      # drop the connection-setup sample
        return best
