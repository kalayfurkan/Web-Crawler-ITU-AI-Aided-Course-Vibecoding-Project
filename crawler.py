"""
Web crawler engine with BFS traversal, back pressure, and rate limiting.

Uses only Python-native libraries for HTTP and HTML parsing:
  - urllib for HTTP requests
  - html.parser.HTMLParser for HTML parsing
  - threading for concurrency
"""

import threading
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
import time
import os
import re
import ssl
import unicodedata
from collections import Counter

from database import get_connection

STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'storage')

# ---------------------------------------------------------------------------
# Global registry of active crawler engines (job_id -> CrawlerEngine)
# ---------------------------------------------------------------------------
active_crawlers = {}
crawlers_lock = threading.Lock()


def _normalize(text):
    """Normalize text for consistent word extraction and matching.

    - NFKC collapses compatibility characters (e.g. ﬁ → fi)
    - Turkish İ (U+0130) → plain 'i'  (Python's .lower() turns it into
      'i' + combining-dot which breaks exact matching)
    - Final .lower() for case-insensitive storage
    """
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('\u0130', 'i')   # İ → i
    return text.lower()


# ---------------------------------------------------------------------------
# Native HTML parser — extracts links and visible text
# ---------------------------------------------------------------------------
class HTMLContentExtractor(HTMLParser):
    """Extract hyperlinks and visible text from raw HTML."""

    SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'svg', 'iframe'})

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self.title = ''
        self._in_title = False
        self._skip_depth = 0

    # -- callbacks ----------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag == 'title':
            self._in_title = True
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                resolved = self._resolve_url(href)
                if resolved:
                    self.links.append(resolved)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned)

    def error(self, message):
        pass  # suppress parser errors

    # -- helpers ------------------------------------------------------------

    def _resolve_url(self, href: str):
        """Resolve *href* against *base_url*; keep only http(s) URLs."""
        href = href.strip()
        if href.startswith(('#', 'mailto:', 'javascript:', 'tel:', 'data:')):
            return None
        try:
            resolved = urllib.parse.urljoin(self.base_url, href)
            parsed = urllib.parse.urlparse(resolved)
            if parsed.scheme not in ('http', 'https'):
                return None
            # strip fragment
            resolved = urllib.parse.urlunparse(parsed._replace(fragment=''))
            return resolved
        except Exception:
            return None

    def get_word_data(self):
        """Return word frequencies AND positions from visible text.

        Supports Turkish and other Unicode letters (ç, ğ, ı, ö, ş, ü, etc.).

        Returns dict:  word -> {'freq': int, 'positions': [int, ...]}
        Positions are 0-based word offsets in the page text, enabling
        phrase / proximity search at query time.
        """
        text = ' '.join(self.text_parts)
        text = _normalize(text)
        tokens = re.findall(r'[^\W\d_]{2,}', text)

        data: dict[str, dict] = {}
        for pos, word in enumerate(tokens):
            if word not in data:
                data[word] = {'freq': 0, 'positions': []}
            data[word]['freq'] += 1
            data[word]['positions'].append(pos)
        return data


# ---------------------------------------------------------------------------
# Crawler engine
# ---------------------------------------------------------------------------
class CrawlerEngine:
    """BFS web crawler with rate limiting and back-pressure."""

    def __init__(self, job_id, origin_url, max_depth,
                 max_queue_depth=1000, max_rate=5.0, max_urls=0, resume=False):
        self.job_id = job_id
        self.origin_url = origin_url
        self.max_depth = max_depth
        self.max_queue_depth = max_queue_depth
        self.max_rate = max_rate
        self.max_urls = max_urls  # 0 = unlimited
        self.min_interval = 1.0 / max_rate if max_rate > 0 else 0
        self.resume = resume

        self.file_lock = threading.Lock()
        self.running = True
        self.paused = False
        self.pages_crawled = 0
        self.pages_queued = 0
        self.backpressure_active = False
        self.last_request_time = 0.0
        self.thread = None

    # -- public API ---------------------------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        with crawlers_lock:
            active_crawlers[self.job_id] = self

    def stop(self):
        self.running = False
        self._log('INFO', 'Stop requested by user')

    def pause(self):
        self.paused = True
        self._update_status('paused')
        self._log('INFO', 'Crawler paused')

    def unpause(self):
        self.paused = False
        self._update_status('running')
        self._log('INFO', 'Crawler resumed')

    # -- main loop ----------------------------------------------------------

    def _run(self):
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            self._update_status('running')

            if not self.resume:
                # seed the queue with the origin URL
                self._enqueue_urls([(self.origin_url, 0)])
                self._log('INFO',
                          f'Started crawl of {self.origin_url} '
                          f'(max_depth={self.max_depth}, '
                          f'max_rate={self.max_rate}/s, '
                          f'max_queue={self.max_queue_depth})')
            else:
                self._log('INFO', 'Resuming crawl from saved queue state')

            while self.running:
                # honour pause
                while self.paused and self.running:
                    time.sleep(0.5)
                if not self.running:
                    break

                # check max_urls limit
                if self.max_urls > 0 and self.pages_crawled >= self.max_urls:
                    self._log('INFO',
                              f'Reached max URL limit ({self.max_urls}), stopping')
                    break

                url_row = self._dequeue()
                if url_row is None:
                    break  # queue empty

                url, depth = url_row
                self._rate_limit()
                self._process_url(url, depth)

            status = 'completed' if self.running else 'stopped'
            self._update_status(status)
            self._log('INFO',
                      f'Crawl finished — status={status}, '
                      f'pages_crawled={self.pages_crawled}')

        except Exception as exc:
            self._log('ERROR', f'Crawl failed: {exc}')
            self._update_status('failed')
        finally:
            with crawlers_lock:
                active_crawlers.pop(self.job_id, None)

    # -- page processing ----------------------------------------------------

    def _process_url(self, url, depth):
        try:
            if self._is_visited(url):
                self._mark_queue(url, 'done')
                return

            self._log('INFO', f'[depth={depth}] Fetching {url}')

            html = self._fetch(url)
            if html is None:
                self._mark_queue(url, 'done')
                return

            parser = HTMLContentExtractor(url)
            try:
                parser.feed(html)
            except Exception as exc:
                self._log('WARNING', f'Parse error on {url}: {exc}')
                self._mark_queue(url, 'done')
                return

            self._mark_visited(url, depth, parser.title)
            word_data = parser.get_word_data()
            self._store_words(word_data, url, self.origin_url, depth)

            if depth < self.max_depth:
                new_links = self._filter_new(parser.links)
                if new_links:
                    if not self._is_backpressured():
                        self._enqueue_urls([(l, depth + 1) for l in new_links])
                    else:
                        self._log('WARNING',
                                  f'Back-pressure: skipping {len(new_links)} '
                                  f'links from {url}')

            self.pages_crawled += 1
            self._mark_queue(url, 'done')
            self._sync_stats()

        except Exception as exc:
            self._log('ERROR', f'Error on {url}: {exc}')
            self._mark_queue(url, 'failed')

    # -- HTTP fetch ---------------------------------------------------------

    def _fetch(self, url):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; PyCrawler/1.0)',
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                if resp.status != 200:
                    self._log('WARNING', f'HTTP {resp.status} for {url}')
                    return None
                ctype = resp.headers.get('Content-Type', '')
                if 'text/html' not in ctype and 'xhtml' not in ctype:
                    return None
                raw = resp.read(5 * 1024 * 1024)  # 5 MB cap
                enc = resp.headers.get_content_charset() or 'utf-8'
                try:
                    return raw.decode(enc, errors='replace')
                except (LookupError, UnicodeDecodeError):
                    return raw.decode('utf-8', errors='replace')

        except urllib.error.HTTPError as exc:
            self._log('WARNING', f'HTTP {exc.code} for {url}')
        except urllib.error.URLError as exc:
            self._log('WARNING', f'URL error for {url}: {exc.reason}')
        except Exception as exc:
            self._log('WARNING', f'Fetch error for {url}: {exc}')
        return None

    # -- queue management (MySQL-backed) ------------------------------------

    def _enqueue_urls(self, url_depth_pairs):
        conn = get_connection()
        cur = conn.cursor()
        try:
            for url, depth in url_depth_pairs:
                try:
                    cur.execute(
                        "INSERT INTO crawl_queue (url, crawl_job_id, depth) "
                        "VALUES (%s, %s, %s)",
                        (url, self.job_id, depth),
                    )
                    self.pages_queued += 1
                except Exception:
                    pass  # duplicate
            conn.commit()
            self._sync_stats()
        finally:
            conn.close()

    def _dequeue(self):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, url, depth FROM crawl_queue "
                "WHERE crawl_job_id=%s AND status='pending' ORDER BY id LIMIT 1",
                (self.job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            qid, url, depth = row
            cur.execute("UPDATE crawl_queue SET status='processing' WHERE id=%s", (qid,))
            conn.commit()
            return url, depth
        finally:
            conn.close()

    def _mark_queue(self, url, status):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crawl_queue SET status=%s "
                "WHERE url=%s AND crawl_job_id=%s AND status='processing'",
                (status, url, self.job_id),
            )
            conn.commit()
        finally:
            conn.close()

    # -- visited set (MySQL-backed) -----------------------------------------

    def _is_visited(self, url):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM visited_urls WHERE url=%s AND crawl_job_id=%s LIMIT 1",
                (url, self.job_id),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    def _mark_visited(self, url, depth, title=''):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT IGNORE INTO visited_urls "
                "(url, crawl_job_id, origin_url, depth, title) "
                "VALUES (%s, %s, %s, %s, %s)",
                (url, self.job_id, self.origin_url, depth, (title or '')[:500]),
            )
            conn.commit()
        finally:
            conn.close()

    def _filter_new(self, links, max_links=100):
        """Return links not yet visited or queued, capped at *max_links*."""
        if not links:
            return []
        unique = list(dict.fromkeys(links))[:max_links * 3]  # pre-cap
        conn = get_connection()
        cur = conn.cursor()
        try:
            visited = set()
            queued = set()
            batch = 200
            for i in range(0, len(unique), batch):
                chunk = unique[i:i + batch]
                ph = ','.join(['%s'] * len(chunk))
                cur.execute(
                    f"SELECT url FROM visited_urls "
                    f"WHERE crawl_job_id=%s AND url IN ({ph})",
                    (self.job_id, *chunk),
                )
                visited.update(r[0] for r in cur.fetchall())
                cur.execute(
                    f"SELECT url FROM crawl_queue "
                    f"WHERE crawl_job_id=%s AND url IN ({ph})",
                    (self.job_id, *chunk),
                )
                queued.update(r[0] for r in cur.fetchall())

            new = [l for l in unique
                   if l not in visited and l not in queued]
            return new[:max_links]
        finally:
            conn.close()

    # -- back-pressure ------------------------------------------------------

    def _is_backpressured(self):
        """Check queue depth and update back-pressure flag.

        Returns True when the queue exceeds the limit — the caller should
        skip enqueuing new URLs but keep processing existing ones.
        """
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM crawl_queue "
                "WHERE crawl_job_id=%s AND status='pending'",
                (self.job_id,),
            )
            pending = cur.fetchone()[0]

            if pending >= self.max_queue_depth:
                if not self.backpressure_active:
                    self.backpressure_active = True
                    self._set_backpressure_flag(True)
                    self._log('WARNING',
                              f'Back-pressure ON — queue depth {pending} '
                              f'>= limit {self.max_queue_depth}')
                return True
            else:
                if self.backpressure_active:
                    self.backpressure_active = False
                    self._set_backpressure_flag(False)
                    self._log('INFO',
                              f'Back-pressure OFF — queue depth {pending}')
                return False
        finally:
            conn.close()

    def _rate_limit(self):
        if self.min_interval > 0:
            elapsed = time.time() - self.last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    # -- word storage (file-based) ------------------------------------------

    def _store_words(self, word_data, url, origin_url, depth):
        """Persist word frequencies + positions to data/storage/<letter>.data.

        Format per line (tab-separated):
            word    url    origin    depth    frequency    positions
        positions is a comma-separated list of 0-based word offsets.
        Backward compatible: old readers that split on tab and expect 5
        fields still work — the 6th field is simply ignored.
        """
        by_letter: dict[str, list] = {}
        for word, info in word_data.items():
            ch = word[0]
            if ch.isalpha():
                by_letter.setdefault(ch, []).append(
                    (word, info['freq'], info['positions'])
                )

        with self.file_lock:
            for letter, entries in by_letter.items():
                path = os.path.join(STORAGE_DIR, f'{letter}.data')
                with open(path, 'a', encoding='utf-8') as fh:
                    for word, freq, positions in entries:
                        pos_str = ','.join(str(p) for p in positions)
                        fh.write(f'{word}\t{url}\t{origin_url}\t{depth}\t{freq}\t{pos_str}\n')

    # -- status helpers -----------------------------------------------------

    def _update_status(self, status):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE crawl_jobs SET status=%s WHERE id=%s",
                        (status, self.job_id))
            conn.commit()
        finally:
            conn.close()

    def _sync_stats(self):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crawl_jobs SET pages_crawled=%s, pages_queued=%s, "
                "backpressure_active=%s WHERE id=%s",
                (self.pages_crawled, self.pages_queued,
                 int(self.backpressure_active), self.job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_backpressure_flag(self, active):
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE crawl_jobs SET backpressure_active=%s WHERE id=%s",
                        (int(active), self.job_id))
            conn.commit()
        finally:
            conn.close()

    def _log(self, level, message):
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO crawl_logs (crawl_job_id, message, log_level) "
                "VALUES (%s, %s, %s)",
                (self.job_id, message, level),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
