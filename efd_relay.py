"""Senate eFD relay (congress-senate-source spec §5). efdsearch.senate.gov refuses addresses outside the US,
so this script runs on a GitHub-hosted runner there: it accepts the site's agreement, lists the periodic
transaction reports (PTRs) filed in the window, saves each electronic PTR page once under ptr/, and writes
feed.json for the desk. Standard library only, so the runner needs nothing installed.

    python3 efd_relay.py --out . --window-days 60
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("senate-relay")
BASE = "https://efdsearch.senate.gov"
HOME = f"{BASE}/search/home/"
SEARCH = f"{BASE}/search/"
DATA = f"{BASE}/search/report/data/"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
USER_AGENT = "Mozilla/5.0 (compatible; desk-congress/1.0)"  # so the Senate's site can tell who asks
PAGE = 100  # grid rows per search page
PAUSE_S = 1.0  # between two requests to the Senate's site
PTR_TYPE = "[11]"  # the report-type checkbox for periodic transaction reports
VIEW = re.compile(
    r"/search/view/(ptr|paper)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/", re.I
)
CSRF = re.compile(r"""name=["']csrfmiddlewaretoken["'][^>]*value=["']([^"']+)["']""")
MDY = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})")
# feed key → the PTR table's header texts (lower case) that name that column
COLUMNS = {
    "date": ("transaction date",),
    "owner": ("owner",),
    "ticker": ("ticker",),
    "asset": ("asset name", "asset"),
    "asset_type": ("asset type",),
    "type": ("type", "transaction type"),
    "amount": ("amount",),
    "comment": ("comment",),
}


class RelayError(Exception):
    pass


class Session:
    """urllib with a cookie jar: the agreement answers a 302 whose cookies must be kept, and the jar records
    every hop's Set-Cookie before the redirect is followed."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, url: str, form: dict | None = None, headers: dict | None = None) -> tuple[str, str]:
        """(final url, body text); a POST when `form` is given. Any failure is a RelayError."""
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RelayError(f"{e.code} from {url}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RelayError(f"{type(e).__name__} from {url}: {e}") from e

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.jar if c.name == name), None)


def accept_agreement(session: Session) -> str:
    """GET the home page for its form token, POST the agreement; returns the CSRF token the search needs."""
    _, page = session.request(HOME)
    found = CSRF.search(page)
    token = found.group(1) if found else session.cookie("csrftoken")
    if not token:
        raise RelayError("no csrfmiddlewaretoken on the search home page: the form changed")
    time.sleep(PAUSE_S)
    session.request(
        HOME, form={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"}, headers={"Referer": HOME}
    )
    return session.cookie("csrftoken") or token


def search(session: Session, token: str, since: date, until: date) -> list[list[str]]:
    """Every grid row of the PTRs filed in [since, until], 100 a page."""
    rows: list[list[str]] = []
    start = 0
    while True:
        form = {
            "start": str(start),
            "length": str(PAGE),
            "report_types": PTR_TYPE,
            "filer_types": "[]",
            "submitted_start_date": f"{since:%m/%d/%Y} 00:00:00",
            "submitted_end_date": f"{until:%m/%d/%Y} 23:59:59",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": token,
        }
        time.sleep(PAUSE_S)
        _, body = session.request(
            DATA,
            form=form,
            headers={"Referer": SEARCH, "X-CSRFToken": token, "X-Requested-With": "XMLHttpRequest"},
        )
        try:
            page = json.loads(body)
            data = page.get("data") or []
            total = int(page.get("recordsFiltered") or page.get("recordsTotal") or 0)
        except (ValueError, AttributeError, TypeError) as e:
            raise RelayError(f"the search answered something other than its JSON grid: {body[:200]!r}") from e
        rows += data
        start += PAGE
        if not data or start >= total:
            return rows


def _iso(mdy: str) -> str | None:
    found = MDY.match(mdy)
    if not found:
        return None
    try:
        return date(int(found.group(3)), int(found.group(1)), int(found.group(2))).isoformat()
    except ValueError:
        return None


def filing_from_row(row: list) -> dict | None:
    """A grid row [first, last, office, link html, filed] → a feed filing (state and transactions come later);
    None when the link or the date is not there."""
    cells = [str(c) for c in row]
    link = next((c for c in cells if VIEW.search(c)), None)
    filed = next((d for d in (_iso(c) for c in cells) if d), None)
    if link is None or filed is None:
        return None
    kind, uuid = (part.lower() for part in VIEW.search(link).groups())
    return {
        "uuid": uuid,
        "kind": kind,
        "first": cells[0].strip() if len(cells) > 0 else "",
        "last": cells[1].strip() if len(cells) > 1 else "",
        "office": cells[2].strip() if len(cells) > 2 else "",
        "title": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", link)).split()),
        "filed": filed,
        "url": f"{BASE}/search/view/{kind}/{uuid}/",
    }


class _Tables(HTMLParser):
    """Every table on the page as rows of cell texts (th and td alike), whitespace collapsed."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    return next((header.index(n) for n in names if n in header), None)


def parse_ptr(page: str) -> list[dict] | None:
    """The transactions of a PTR page as its table prints them; None when the page has no such table."""
    parser = _Tables()
    parser.feed(page)
    for table in parser.tables:
        if not table:
            continue
        header = [h.lower() for h in table[0]]
        if "transaction date" not in header or "amount" not in header:
            continue
        columns = {key: _column(header, names) for key, names in COLUMNS.items()}
        return [
            {key: (cells[i] if i is not None and i < len(cells) else "") for key, i in columns.items()}
            for cells in table[1:]
            if len(cells) >= len(header)  # a spacer row has fewer cells
        ]
    return None


def fetch_ptr(session: Session, uuid: str) -> str:
    """The PTR page; a lost session (the site answers its agreement page) is accepted again, retried once."""
    url = f"{BASE}/search/view/ptr/{uuid}/"
    final, page = session.request(url, headers={"Referer": SEARCH})
    if "/search/home" not in final and "prohibition_agreement" not in page:
        return page
    log.info("session lost at %s: accepting the agreement again", uuid)
    accept_agreement(session)
    time.sleep(PAUSE_S)
    final, page = session.request(url, headers={"Referer": SEARCH})
    if "/search/home" in final or "prohibition_agreement" in page:
        raise RelayError(f"session lost twice fetching {uuid}")
    return page


def _plain(name: str) -> str:
    return re.sub(
        r"[^a-z]", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    )


def load_senators(session: Session) -> dict[str, list[dict]]:
    """Current senators by plain last name, from the unitedstates/congress-legislators file."""
    _, body = session.request(LEGISLATORS)
    out: dict[str, list[dict]] = {}
    try:
        for member in json.loads(body):
            term = member["terms"][-1]
            if term.get("type") != "sen":
                continue
            name = member["name"]
            out.setdefault(_plain(name["last"]), []).append(
                {
                    "first": _plain(name["first"]),
                    "nick": _plain(name.get("nickname") or ""),
                    "state": term["state"],
                }
            )
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise RelayError(f"legislators file unreadable: {type(e).__name__}: {e}") from e
    return out


def state_of(first: str, last: str, senators: dict[str, list[dict]]) -> str | None:
    """The state of the one current senator with this name; None when none or several fit (never a guess)."""
    same = senators.get(_plain(last), [])
    if len(same) == 1:
        return same[0]["state"]
    given = _plain(first.split(" ")[0]) if first.strip() else ""
    fit = [s for s in same if given and given in (s["first"], s["nick"])]
    return fit[0]["state"] if len(fit) == 1 else None


def build_feed(
    out: Path, filings: list[dict], senators: dict, now: datetime, since: date, window_days: int
) -> dict:
    """feed.json's content (spec §4.3): the window's filings, electronic PTRs with the transactions parsed
    from their saved page; a page not on disk leaves its filing out of this run's feed."""
    entries = []
    for f in filings:
        entry = {**f, "state": state_of(f["first"], f["last"], senators), "transactions": []}
        if f["kind"] == "ptr":
            path = out / "ptr" / f"{f['uuid']}.html"
            if not path.exists():
                continue
            txs = parse_ptr(path.read_text(encoding="utf-8"))
            if txs is None:
                entry["unparsed"] = True
            else:
                entry["transactions"] = txs
        entries.append(entry)
    return {
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "since": since.isoformat(),
        "filings": entries,
    }


def main(argv: list[str] | None = None, session: Session | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("."), help="the relay repository's checkout")
    ap.add_argument("--window-days", type=int, default=60)
    ap.add_argument("--limit", type=int, default=None, help="fetch at most this many new pages (a trial run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pages = args.out / "ptr"
    pages.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)
    until = now.date()
    since = until - timedelta(days=args.window_days)
    session = session or Session()
    try:
        token = accept_agreement(session)
        rows = search(session, token, since, until)
        senators = load_senators(session)
    except RelayError as e:
        log.error("relay stopped before writing the feed: %s", e)
        return 1
    byid: dict[str, dict] = {}
    for row in rows:
        filing = filing_from_row(row)
        if filing is None:
            log.warning("grid row skipped, no link or date: %r", row)
        else:
            byid.setdefault(filing["uuid"], filing)  # a filing listed twice is one filing
    filings = sorted(byid.values(), key=lambda f: (f["filed"], f["uuid"]))
    log.info(
        "%s PTR filings since %s, %s electronic",
        len(filings),
        since,
        sum(f["kind"] == "ptr" for f in filings),
    )
    fetched = failed = 0
    for filing in filings:
        if filing["kind"] != "ptr" or (pages / f"{filing['uuid']}.html").exists():
            continue
        if args.limit is not None and fetched >= args.limit:
            break
        time.sleep(PAUSE_S)
        try:
            page = fetch_ptr(session, filing["uuid"])
        except RelayError as e:
            failed += 1
            log.warning("ptr %s skipped until the next run: %s", filing["uuid"], e)
            continue
        (pages / f"{filing['uuid']}.html").write_text(page, encoding="utf-8")
        fetched += 1
    feed = build_feed(args.out, filings, senators, now, since, args.window_days)
    (args.out / "feed.json").write_text(
        json.dumps(feed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.info("feed: %s filings, %s pages fetched, %s failed", len(feed["filings"]), fetched, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
