# Product Requirements Document — Web Crawler & Search Engine

This document is the specification that was used to guide the AI (Claude Code) in building this project. It captures the full set of requirements, constraints, and design decisions that were communicated to the AI during the development process.

---

## Objective

Build a functional web crawler and real-time search engine from scratch that runs on localhost. The system must expose two core capabilities — **indexing** (crawling) and **searching** — through both a web UI and a JSON API.

## Core Requirements

### 1. Indexer (`POST /index`)

**Input:** `origin` (URL), `k` (max crawl depth)

**Behavior:**
- Perform a BFS web crawl starting from `origin`, following hyperlinks up to `k` hops deep.
- **Never crawl the same page twice** — maintain a visited set to guarantee uniqueness.
- Design for large-scale crawls running on a single machine.

**Back Pressure:**
- Implement a configurable maximum queue depth. When the pending queue exceeds this threshold, the crawler must stop enqueuing new URLs while continuing to process existing ones.
- Implement configurable rate limiting (max requests per second) to control HTTP request throughput.
- The back-pressure state must be visible in the UI.

**Native Libraries Constraint:**
- Use Python's native `urllib.request` for HTTP fetching — not `requests` or `httpx`.
- Use Python's native `html.parser.HTMLParser` for HTML parsing — not `BeautifulSoup` or `lxml`.
- Use Python's native `threading` for concurrency — not `asyncio` or `celery`.

### 2. Search (`GET /search?query=<q>&sortBy=relevance`)

**Input:** `query` (string), optional `sortBy` parameter

**Output:** JSON list of triples `(relevant_url, origin_url, depth)` with relevance scores.

**Relevance Scoring:**
```
Base score = (frequency × 10) + 1000 [exact match bonus] − (depth × 5)
```
- Multi-word queries: aggregate scores per URL, add coverage bonus (+500 per additional matched word).
- Phrase matching: when all query words appear consecutively on a page, add +5000 × phrase_frequency.

**Live Search:** Search must be able to run while the indexer is still active, reflecting newly discovered results in real-time.

### 3. Web UI

The system must provide a neon-green themed web dashboard with the following pages:

| Page | Path | Purpose |
|------|------|---------|
| Crawler | `/` | Form to start new crawls + list of previous jobs |
| Status Dashboard | `/status` | Overview of all crawl jobs |
| Job Detail | `/status/<id>` | Live-updating view: progress, queue stats, back-pressure indicator, logs |
| Search | `/search` | Search input with paginated results, "I'm Feeling Lucky" button |

**Real-time updates:** The job detail page must poll the server every 2 seconds and update stats, logs, and control buttons without page reload. When a job completes, pause/stop buttons must disappear automatically.

### 4. Data Storage

**MySQL** for crawler state:
- `crawl_jobs` — job metadata and status
- `crawl_queue` — BFS queue with pending/processing/done/failed status
- `visited_urls` — deduplication set
- `crawl_logs` — timestamped log entries

**File system** for word index:
- `data/storage/<letter>.data` — tab-separated: `word  url  origin  depth  frequency  positions`
- The `positions` field stores comma-separated 0-based word offsets for phrase matching.

### 5. Resume After Interruption (Bonus)

If the server stops mid-crawl, the system must be able to resume from saved state:
- Pending queue items and visited URLs persist in MySQL.
- A "Resume" button on the UI re-creates the crawler thread from the saved queue.

## Additional Features

- **Max URLs limit** — optional parameter to cap total pages crawled per job.
- **Delete job** — remove a crawl job and its indexed data from both DB and storage files.
- **Clear all data** — wipe the entire search index.
- **Unicode support** — Turkish characters (ç, ğ, ı, ö, ş, ü, İ) and other Unicode letters handled correctly in both indexing and search.
- **"I'm Feeling Lucky"** — search button that navigates directly to the top result.

## Technical Constraints

- **Port:** 3600
- **Database:** MySQL on localhost
- **Framework:** Flask (Python)
- **Dependencies:** Minimal — only Flask, mysql-connector-python, python-dotenv
- **Configuration:** All credentials via `.env` file (gitignored)

## Evaluation Criteria

- **Functionality (40%):** Does it crawl accurately and search concurrently?
- **Architectural Sensibility (40%):** How well is back pressure, thread safety, and deduplication handled?
- **AI Stewardship (20%):** Can you explain the AI-generated code and justify the design choices?
