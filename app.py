"""
Flask web application — exposes crawler and search functionality.

    python app.py          # starts on http://localhost:3600
"""

import os
import time
import uuid
import datetime

from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory

from database import init_db, get_connection, row_to_dict
from crawler import CrawlerEngine, active_crawlers, crawlers_lock, STORAGE_DIR
from search import search as do_search

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False


# ── helpers ────────────────────────────────────────────────────────────────

def _serialize_rows(rows):
    return [row_to_dict(r) for r in rows]


# ── pages ──────────────────────────────────────────────────────────────────

@app.route('/')
def index_page():
    """Crawler dashboard — start new crawls, view existing jobs."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM crawl_jobs ORDER BY created_at DESC")
    jobs = _serialize_rows(cur.fetchall())
    conn.close()
    return render_template('crawler.html', jobs=jobs)


@app.route('/status')
def status_page():
    """Overview of all crawl jobs."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM crawl_jobs ORDER BY created_at DESC")
    jobs = _serialize_rows(cur.fetchall())
    conn.close()
    return render_template('status.html', jobs=jobs)


@app.route('/status/<job_id>')
def job_status_page(job_id):
    """Detailed view of a single crawl job with live logs."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM crawl_jobs WHERE id=%s", (job_id,))
    job = row_to_dict(cur.fetchone())
    if job is None:
        conn.close()
        return "Job not found", 404

    cur.execute(
        "SELECT * FROM crawl_logs WHERE crawl_job_id=%s ORDER BY created_at DESC LIMIT 200",
        (job_id,),
    )
    logs = _serialize_rows(cur.fetchall())

    cur.execute(
        "SELECT status, COUNT(*) AS cnt FROM crawl_queue "
        "WHERE crawl_job_id=%s GROUP BY status",
        (job_id,),
    )
    queue_stats = {r['status']: r['cnt'] for r in cur.fetchall()}
    conn.close()

    return render_template('job_status.html', job=job, logs=logs, queue_stats=queue_stats)


@app.route('/search')
def search_page():
    """Search interface — returns HTML when no query, JSON when query present."""
    query = request.args.get('query', '').strip()
    sort_by = request.args.get('sortBy', 'relevance')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))

    if query:
        results, total = do_search(query, sort_by, page, per_page)
        return jsonify({
            'query': query,
            'sort_by': sort_by,
            'total_results': total,
            'page': page,
            'per_page': per_page,
            'results': results,
        })

    return render_template('search.html')


# ── API: crawl management ─────────────────────────────────────────────────

@app.route('/index', methods=['POST'])
def start_crawl():
    """Start a new crawl job.  Accepts JSON or form data."""
    data = request.json if request.is_json else request.form
    origin = (data.get('origin') or '').strip()
    k = int(data.get('k', data.get('depth', 2)))
    max_queue = int(data.get('max_queue_depth', 1000))
    max_rate = float(data.get('max_rate', 5.0))
    max_urls = int(data.get('max_urls', 0))

    if not origin:
        if request.is_json:
            return jsonify({'error': 'origin is required'}), 400
        return redirect(url_for('index_page'))

    # Normalise URL
    if not origin.startswith(('http://', 'https://')):
        origin = 'https://' + origin

    job_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crawl_jobs (id, origin_url, max_depth, max_queue_depth, max_rate, max_urls) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (job_id, origin, k, max_queue, max_rate, max_urls),
    )
    conn.commit()
    conn.close()

    engine = CrawlerEngine(job_id, origin, k, max_queue, max_rate, max_urls)
    engine.start()

    if request.is_json:
        return jsonify({'job_id': job_id, 'status': 'running', 'origin': origin, 'depth': k})
    return redirect(url_for('job_status_page', job_id=job_id))


@app.route('/api/status/<job_id>')
def api_job_status(job_id):
    """JSON endpoint polled by the UI for live updates."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM crawl_jobs WHERE id=%s", (job_id,))
    job = row_to_dict(cur.fetchone())

    cur.execute(
        "SELECT status, COUNT(*) AS cnt FROM crawl_queue "
        "WHERE crawl_job_id=%s GROUP BY status", (job_id,),
    )
    queue_stats = {r['status']: r['cnt'] for r in cur.fetchall()}

    cur.execute(
        "SELECT message, log_level, created_at FROM crawl_logs "
        "WHERE crawl_job_id=%s ORDER BY created_at DESC LIMIT 50",
        (job_id,),
    )
    logs = _serialize_rows(cur.fetchall())
    conn.close()

    return jsonify({'job': job, 'queue_stats': queue_stats, 'logs': logs})


@app.route('/api/stop/<job_id>', methods=['POST'])
def api_stop(job_id):
    with crawlers_lock:
        crawler = active_crawlers.get(job_id)
    if crawler:
        crawler.stop()
        return jsonify({'status': 'stopping'})
    return jsonify({'error': 'Not running'}), 404


@app.route('/api/pause/<job_id>', methods=['POST'])
def api_pause(job_id):
    with crawlers_lock:
        crawler = active_crawlers.get(job_id)
    if crawler:
        crawler.pause()
        return jsonify({'status': 'paused'})
    return jsonify({'error': 'Not running'}), 404


@app.route('/api/resume/<job_id>', methods=['POST'])
def api_resume(job_id):
    with crawlers_lock:
        crawler = active_crawlers.get(job_id)
    if crawler:
        crawler.unpause()
        return jsonify({'status': 'resumed'})

    # Try to resume a previously interrupted job from DB state
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM crawl_jobs WHERE id=%s", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        return jsonify({'error': 'Job not found'}), 404

    cur.execute(
        "SELECT COUNT(*) AS cnt FROM crawl_queue "
        "WHERE crawl_job_id=%s AND status='pending'", (job_id,),
    )
    pending = cur.fetchone()['cnt']
    conn.close()

    if pending == 0:
        return jsonify({'error': 'No pending items to resume'}), 400

    engine = CrawlerEngine(
        job_id, job['origin_url'], job['max_depth'],
        job['max_queue_depth'], job['max_rate'],
        job.get('max_urls', 0), resume=True,
    )
    engine.pages_crawled = job['pages_crawled']
    engine.pages_queued = job['pages_queued']
    engine.start()
    return jsonify({'status': 'resumed', 'pending': pending})


@app.route('/api/delete/<job_id>', methods=['POST'])
def api_delete(job_id):
    """Delete a crawl job and its related data from DB and storage files."""
    # Stop crawler if still running
    with crawlers_lock:
        crawler = active_crawlers.get(job_id)
    if crawler:
        crawler.stop()
        import time as _t
        _t.sleep(1)

    # Get origin_url before deleting (for storage cleanup)
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT origin_url FROM crawl_jobs WHERE id=%s", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        return jsonify({'error': 'Job not found'}), 404

    origin = job['origin_url']

    # Delete from DB (CASCADE removes queue, logs, visited_urls)
    cur.execute("DELETE FROM crawl_jobs WHERE id=%s", (job_id,))
    conn.commit()
    conn.close()

    # Clean storage files: remove lines matching this origin
    _clean_storage_files(origin)

    return jsonify({'status': 'deleted'})


def _clean_storage_files(origin_url):
    """Remove entries from storage files that belong to the given origin."""
    if not os.path.isdir(STORAGE_DIR):
        return
    for fname in os.listdir(STORAGE_DIR):
        if not fname.endswith('.data'):
            continue
        fpath = os.path.join(STORAGE_DIR, fname)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            kept = [l for l in lines if l.split('\t')[2:3] != [origin_url]]
            if len(kept) != len(lines):
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.writelines(kept)
        except Exception:
            pass


@app.route('/api/clear_storage', methods=['POST'])
def api_clear_storage():
    """Delete ALL storage files — wipes the entire search index."""
    if not os.path.isdir(STORAGE_DIR):
        return jsonify({'status': 'nothing to clear'})
    removed = 0
    for fname in os.listdir(STORAGE_DIR):
        if fname.endswith('.data'):
            os.remove(os.path.join(STORAGE_DIR, fname))
            removed += 1
    return jsonify({'status': 'cleared', 'files_removed': removed})


@app.route('/api/jobs')
def api_list_jobs():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM crawl_jobs ORDER BY created_at DESC")
    jobs = _serialize_rows(cur.fetchall())
    conn.close()
    return jsonify({'jobs': jobs})


# ── serve raw storage files ───────────────────────────────────────────────

@app.route('/data/storage/<filename>')
def serve_storage_file(filename):
    """Serve raw data files (e.g. data/storage/p.data) for inspection."""
    return send_from_directory(STORAGE_DIR, filename)


# ── boot ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    os.makedirs(STORAGE_DIR, exist_ok=True)
    print(f"\n  Web Crawler running on http://localhost:3600\n")
    app.run(host='0.0.0.0', port=3600, debug=False, threaded=True)
