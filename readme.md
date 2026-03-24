# Web Crawler & Search Engine

A Python-based web crawler and real-time search engine built with **Flask** and **MySQL**. The system crawls web pages using BFS traversal with configurable depth, applies back-pressure controls and rate limiting, and indexes the content for instant keyword and phrase search — all using Python's native standard library for core operations.

---

## Setup & Installation

### Prerequisites

- **Python 3.9+**
- **MySQL 8.0+** running on localhost
- **pip** (Python package manager)

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/crawler.git
cd crawler

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure database credentials
#    Copy the example and fill in your MySQL password:
cp .env.example .env
#    Edit .env with your MYSQL_PASSWORD

# 4. Start the application
python app.py
```

The server starts at **http://localhost:3600** and automatically creates the `web_crawler` database and all required tables on first run.

### Environment Variables

All configuration is done through `.env` (or environment variables):

| Variable         | Default       | Description                     |
|------------------|---------------|---------------------------------|
| `MYSQL_HOST`     | `localhost`   | MySQL server host               |
| `MYSQL_PORT`     | `3306`        | MySQL server port               |
| `MYSQL_USER`     | `root`        | MySQL username                  |
| `MYSQL_PASSWORD` | *(empty)*     | MySQL password                  |
| `MYSQL_DATABASE` | `web_crawler` | Database name (auto-created)    |

---

## Architecture

The system is composed of three main layers:

```
┌─────────────────┐      ┌───────────┐      ┌──────────────────┐
│   Flask Web UI  │─────▶│   MySQL   │◀─────│  Crawler Thread  │
│   (port 3600)   │      │           │      │                  │
│                 │      │ • jobs    │      │  urllib.request   │
│  /index (POST)  │      │ • queue   │      │  html.parser     │
│  /search (GET)  │      │ • visited │      │  threading       │
│  /status (GET)  │      │ • logs    │      │                  │
└────────┬────────┘      └───────────┘      └────────┬─────────┘
         │                                           │
         │          ┌─────────────────────┐           │
         └─────────▶│  data/storage/      │◀──────────┘
                    │  a.data … z.data    │
                    │  (word index +      │
                    │   positional data)  │
                    └─────────────────────┘
```

### Key Design Decisions

| Concern | Solution |
|---------|----------|
| **Duplicate prevention** | MySQL `UNIQUE` constraint on `(url, crawl_job_id)` + in-memory check before enqueue |
| **Back pressure** | When pending queue depth exceeds `max_queue_depth`, new link discovery is paused while existing queue drains |
| **Rate limiting** | Token-bucket style — minimum interval between HTTP requests enforced via `time.sleep()` |
| **Thread safety** | `threading.Lock` protects file writes; MySQL transactions protect queue/visited operations |
| **Live search** | Search reads append-only `.data` files; crawler appends to the same files — no lock contention |
| **Resume** | Queue and visited set persist in MySQL; resuming re-creates the crawler thread from saved state |
| **Native libraries** | Core crawling uses `urllib.request`, HTML parsing uses `html.parser.HTMLParser`, concurrency uses `threading` |

### Data Format

Word index files (`data/storage/<letter>.data`) use tab-separated format:

```
word    url    origin    depth    frequency    positions
```

The `positions` field (comma-separated word offsets) enables **exact phrase matching** for multi-word queries.

---

## Features & Usage

### 1. Start a Crawl

**UI:** Go to http://localhost:3600, fill in the form, click **Start Crawl**.

**API:**
```bash
curl -X POST http://localhost:3600/index \
  -H "Content-Type: application/json" \
  -d '{"origin": "https://en.wikipedia.org/wiki/Python", "k": 2, "max_rate": 5, "max_queue_depth": 1000, "max_urls": 50}'
```

| Parameter         | Description                                    | Default |
|-------------------|------------------------------------------------|---------|
| `origin`          | Starting URL                                   | —       |
| `k`               | Maximum crawl depth (hops from origin)         | `2`     |
| `max_rate`        | Max HTTP requests per second                   | `5`     |
| `max_queue_depth` | Queue depth threshold for back-pressure        | `1000`  |
| `max_urls`        | Stop after visiting this many pages (0 = no limit) | `0` |

### 2. Search Indexed Pages

**UI:** Go to http://localhost:3600/search, type a query, click **Search**.

**API:**
```bash
curl "http://localhost:3600/search?query=python&sortBy=relevance"
```

Response:
```json
{
  "query": "python",
  "total_results": 42,
  "results": [
    {
      "relevant_url": "https://en.wikipedia.org/wiki/Python",
      "origin_url": "https://en.wikipedia.org/wiki/Python",
      "depth": 0,
      "relevance_score": 2150,
      "frequency": 115,
      "matched_words": ["python"],
      "phrase_frequency": 0
    }
  ]
}
```

**Relevance scoring (3 layers):**

| Layer | Formula | Purpose |
|-------|---------|---------|
| Base score | `(frequency × 10) + 1000 − (depth × 5)` | Per-word exact match score |
| Coverage bonus | `+500` per additional matched word | Rewards multi-word coverage |
| Phrase bonus | `+5000 × phrase_frequency` | Rewards exact phrase adjacency |

### 3. Monitor System State

- **Dashboard:** http://localhost:3600/status — all jobs overview
- **Job Detail:** http://localhost:3600/status/{job_id} — live-updating progress, queue stats, back-pressure status, and logs
- **Controls:** Pause / Resume / Stop / Delete buttons available on each job

### 4. Resume After Interruption

If the server stops while a crawl is running, the pending queue and visited URLs remain in MySQL. Navigate to the job's status page and click **Resume** to continue from where it left off.

---

## Project Structure

```
├── app.py              # Flask application — routes, API endpoints
├── crawler.py          # Crawler engine — BFS, threading, back-pressure, rate limiting
├── search.py           # Search engine — file-based index lookup, phrase matching
├── database.py         # MySQL connection and schema management
├── requirements.txt    # Python dependencies (Flask, mysql-connector, python-dotenv)
├── .env.example        # Environment variable template
├── templates/
│   ├── base.html       # Base layout with neon-green theme
│   ├── crawler.html    # Start crawl + job list page
│   ├── status.html     # All jobs dashboard
│   ├── job_status.html # Single job detail with live logs
│   └── search.html     # Search interface with results
├── data/storage/       # Generated word index files (gitignored)
├── product_prd.md      # Product requirements document
└── recommendation.md   # Production deployment recommendations
```
