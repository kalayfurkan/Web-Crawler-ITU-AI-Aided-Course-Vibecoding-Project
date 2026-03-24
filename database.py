"""MySQL database setup and connection management."""

import os
import datetime
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get('MYSQL_HOST', 'localhost')
DB_USER = os.environ.get('MYSQL_USER', 'root')
DB_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
DB_NAME = os.environ.get('MYSQL_DATABASE', 'web_crawler')
DB_PORT = int(os.environ.get('MYSQL_PORT', 3306))


def get_connection():
    """Get a new MySQL connection to the web_crawler database."""
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset='utf8mb4',
        use_unicode=True,
    )


def init_db():
    """Create database and all required tables."""
    # First create the database if it doesn't exist
    conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD
    )
    cursor = conn.cursor()
    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.commit()
    conn.close()

    # Now create tables
    conn = get_connection()
    cursor = conn.cursor()

    tables = [
        """CREATE TABLE IF NOT EXISTS crawl_jobs (
            id VARCHAR(64) PRIMARY KEY,
            origin_url TEXT NOT NULL,
            max_depth INT NOT NULL,
            status ENUM('queued','running','completed','failed','paused','stopped')
                DEFAULT 'queued',
            pages_crawled INT DEFAULT 0,
            pages_queued INT DEFAULT 0,
            max_queue_depth INT DEFAULT 1000,
            max_rate FLOAT DEFAULT 5.0,
            max_urls INT DEFAULT 0,
            backpressure_active TINYINT(1) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS visited_urls (
            id INT AUTO_INCREMENT PRIMARY KEY,
            url VARCHAR(2048) NOT NULL,
            crawl_job_id VARCHAR(64) NOT NULL,
            origin_url TEXT NOT NULL,
            depth INT NOT NULL,
            title VARCHAR(512) DEFAULT '',
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_url_job (url(255), crawl_job_id),
            KEY idx_job (crawl_job_id),
            FOREIGN KEY (crawl_job_id) REFERENCES crawl_jobs(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS crawl_queue (
            id INT AUTO_INCREMENT PRIMARY KEY,
            url VARCHAR(2048) NOT NULL,
            crawl_job_id VARCHAR(64) NOT NULL,
            depth INT NOT NULL,
            status ENUM('pending','processing','done','failed') DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_job_status (crawl_job_id, status),
            KEY idx_job_url (crawl_job_id, url(255)),
            FOREIGN KEY (crawl_job_id) REFERENCES crawl_jobs(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS crawl_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            crawl_job_id VARCHAR(64) NOT NULL,
            message TEXT NOT NULL,
            log_level VARCHAR(20) DEFAULT 'INFO',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_job_time (crawl_job_id, created_at),
            FOREIGN KEY (crawl_job_id) REFERENCES crawl_jobs(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
    ]

    for sql in tables:
        cursor.execute(sql)

    conn.commit()

    # Add max_urls column if upgrading from older schema
    try:
        cursor.execute("ALTER TABLE crawl_jobs ADD COLUMN max_urls INT DEFAULT 0 AFTER max_rate")
    except Exception:
        pass  # column already exists

    conn.commit()
    conn.close()
    print("[DB] Database and tables initialized successfully.")


def row_to_dict(row):
    """Convert a dict-row with datetime values to JSON-serializable dict."""
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (datetime.datetime, datetime.date)):
            result[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, datetime.timedelta):
            result[key] = str(value)
    return result
