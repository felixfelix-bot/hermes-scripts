#!/usr/bin/env python3
"""ppq_logger.py — Sync PPQ API call history to zai_usage.db.

Runs every 5 min via cron. Queries PPQ's /queries/history endpoint for
new calls not yet logged, inserts them into api_calls with key_name='ppq'.
"""

import os, json, urllib.request, urllib.error, sqlite3, time, sys
from pathlib import Path
from datetime import datetime, timezone

# Force output to be unbuffered
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

PPQ_API_KEY = os.environ.get('PPQ_API_KEY', '')
DB = Path.home() / '.hermes' / 'bot' / 'zai_usage.db'
STATE_FILE = Path.home() / '.local' / 'state' / 'ppq_logger_last_ts.txt'


def log_debug(msg):
    """Log with timestamp for debugging."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}")
    print(flush=True)


def fetch_ppq_history(since_ts=None, limit=100, retry_count=3):
    """Fetch PPQ query history with comprehensive error handling."""
    url = f'https://api.ppq.ai/queries/history?limit={limit}'
    
    # Convert Unix timestamp to ISO 8601 format if provided
    if since_ts:
        try:
            # Parse the Unix timestamp and format as ISO 8601
            dt = datetime.fromtimestamp(float(since_ts), tz=timezone.utc)
            iso_date = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            url += f'&start_date={iso_date}'
            log_debug(f"Fetching since: {iso_date} (Unix: {since_ts})")
        except (ValueError, TypeError) as e:
            log_debug(f"Warning: Could not parse timestamp {since_ts}: {e}")
            # Fall back to fetching last 24 hours
            pass

    headers = {
        'Authorization': f'Bearer {PPQ_API_KEY}',
        'User-Agent': 'ppq-logger/1.0'
    }
    
    log_debug(f"Request URL: {url.replace(PPQ_API_KEY, '***')}")
    
    for attempt in range(retry_count):
        try:
            req = urllib.request.Request(url, headers=headers)
            
            # Add more detailed request info
            log_debug(f"Attempt {attempt + 1}/{retry_count}: Making request...")
            
            with urllib.request.urlopen(req, timeout=30) as r:
                log_debug(f"Response status: {r.status}")
                
                if r.status == 200:
                    data = json.loads(r.read())
                    log_debug(f"Successfully retrieved {len(data.get('data', []))} records")
                    return data.get('data', [])
                    
                elif r.status == 500:
                    log_debug(f"PPQ API returned 500 error (attempt {attempt + 1}/{retry_count})")
                    if attempt < retry_count - 1:
                        sleep_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                        log_debug(f"Sleeping {sleep_time}s before retry...")
                        time.sleep(sleep_time)
                        continue
                    else:
                        log_debug("Max retries exceeded for 500 error")
                        return []
                        
                elif r.status == 429:
                    log_debug("PPQ API rate limit exceeded")
                    if attempt < retry_count - 1:
                        sleep_time = 10  # Longer sleep for rate limit
                        log_debug(f"Sleeping {sleep_time}s for rate limit...")
                        time.sleep(sleep_time)
                        continue
                    else:
                        log_debug("Max retries exceeded for rate limit")
                        return []
                        
                else:
                    log_debug(f"PPQ API returned status {r.status}")
                    return []
                    
        except urllib.error.URLError as e:
            log_debug(f"Network error (attempt {attempt + 1}/{retry_count}): {e}")
            if attempt < retry_count - 1:
                sleep_time = 2 ** attempt
                log_debug(f"Sleeping {sleep_time}s for network error...")
                time.sleep(sleep_time)
                continue
            else:
                log_debug("Max retries exceeded for network error")
                return []
                
        except Exception as e:
            log_debug(f"PPQ history fetch failed: {e}")
            # Log the full exception details for debugging
            import traceback
            log_debug(f"Full exception: {traceback.format_exc()}")
            return []
    
    log_debug("All retry attempts exhausted")
    return []


def parse_ppq_ts(ts_str):
    """Parse PPQ timestamp string to unix timestamp."""
    try:
        dt = datetime.strptime(ts_str.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S.%f%z')
        return dt.timestamp()
    except ValueError:
        try:
            # Handle timestamps without fractional seconds
            dt = datetime.strptime(ts_str.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S%z')
            return dt.timestamp()
        except ValueError:
            log_debug(f"Warning: Could not parse timestamp '{ts_str}'")
            return time.time()


def log_ppq_call(conn, ts, model, prompt_tokens, completion_tokens, total_tokens, cache_hit):
    """Insert a single PPQ call into api_calls table."""
    key_suffix = PPQ_API_KEY[-8:] if PPQ_API_KEY else 'ppq'
    now = time.time()

    conn.execute("""
        INSERT OR IGNORE INTO api_calls
        (ts, key_name, key_suffix, model, prompt_tokens,
         completion_tokens, total_tokens, tier, cache_hit,
         ollama_hit, ppq_hit, status_code, error, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts,                    # timestamp from PPQ
        'ppq',                 # key_name
        key_suffix,            # key_suffix
        model,                 # model
        prompt_tokens,         # prompt_tokens
        completion_tokens,     # completion_tokens
        total_tokens,          # total_tokens
        'ppq',                 # tier
        int(cache_hit),        # cache_hit
        0,                     # ollama_hit
        1,                     # ppq_hit
        200,                   # status_code
        None,                  # error
        2000                   # duration_ms (estimate)
    ))


def main():
    log_debug("PPQ logger starting")
    
    # Environment check
    if not PPQ_API_KEY:
        log_debug('PPQ_API_KEY not set, skipping')
        return

    log_debug(f"PPQ_API_KEY length: {len(PPQ_API_KEY)}")
    log_debug(f"Database path: {DB}")
    log_debug(f"State file path: {STATE_FILE}")

    # Get last synced timestamp
    last_ts = None
    if STATE_FILE.exists():
        try:
            last_ts = STATE_FILE.read_text().strip()
            log_debug(f"Last synced timestamp: {last_ts}")
        except Exception as e:
            log_debug(f"Warning: Could not read state file: {e}")
            # Create state file with current time - 24 hours
            last_ts = None

    # Fetch PPQ history (last 24h, or since last sync)
    now = time.time()
    if last_ts:
        since_ts = last_ts
    else:
        # Start with last 24 hours if no state file
        since_ts = datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        log_debug(f"No state file, starting with last 24 hours: {since_ts}")

    log_debug(f"Fetching PPQ history since: {since_ts}")
    
    # First try with date filter
    queries = fetch_ppq_history(since_ts=since_ts, limit=200)
    
    if not queries:
        log_debug('No PPQ history data returned with date filter')
        # As a fallback, try fetching recent history without date filter
        log_debug("Trying fallback: fetch recent history without date filter")
        queries = fetch_ppq_history(limit=50)
        if not queries:
            log_debug("Fallback also returned no data")
            return
    
    log_debug(f"Total queries retrieved: {len(queries)}")
    
    # Ensure database exists
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB))
        log_debug("Database connection successful")
    except Exception as e:
        log_debug(f"Could not connect to database: {e}")
        return

    # Get existing PPQ timestamps to avoid duplicates
    try:
        existing = set(r[0] for r in conn.execute(
            "SELECT ts FROM api_calls WHERE key_name='ppq'"
        ).fetchall())
        log_debug(f"Found {len(existing)} existing PPQ records in database")
    except Exception as e:
        log_debug(f"Warning: Could not query existing records: {e}")
        existing = set()

    new_count = 0
    latest_ts = last_ts

    for i, q in enumerate(queries):
        try:
            log_debug(f"Processing query {i+1}/{len(queries)}")
            
            ts = parse_ppq_ts(q.get('timestamp', ''))
            
            # Check if we already have this call
            # Use a 2-second tolerance since timestamps may not be exact
            if any(abs(ts - e) < 2 for e in existing):
                log_debug(f"Skipping duplicate record at timestamp {ts}")
                continue

            model = q.get('model', 'unknown')
            prompt_tokens = q.get('input_count', 0) or q.get('input_tokens', 0) or 0
            completion_tokens = q.get('output_count', 0) or q.get('output_tokens', 0) or 0
            total_tokens = q.get('total_tokens', prompt_tokens + completion_tokens) or 0
            cache_hit = q.get('cached', False) or q.get('cache_hit', False)

            log_ppq_call(conn, ts, model, prompt_tokens, completion_tokens,
                         total_tokens, cache_hit)
            new_count += 1
            log_debug(f"Logged PPQ call: {model}, {prompt_tokens}+{completion_tokens} tokens")

            # Track latest timestamp
            if not latest_ts or ts > float(latest_ts):
                latest_ts = str(ts)
                
        except Exception as e:
            log_debug(f"Warning: Error processing query {i}: {e}")
            continue

    try:
        conn.commit()
        log_debug("Database commit successful")
    except Exception as e:
        log_debug(f"Database commit failed: {e}")

    if new_count > 0:
        log_debug(f'Logged {new_count} new PPQ calls')
    else:
        log_debug(f'No new PPQ calls to log (processed {len(queries)} queries)')

    # Save state
    if latest_ts:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(latest_ts)
            log_debug(f"Updated state file with timestamp: {latest_ts}")
        except Exception as e:
            log_debug(f"Warning: Could not write state file: {e}")

    try:
        conn.close()
        log_debug("Database connection closed")
    except Exception as e:
        log_debug(f"Warning: Could not close database: {e}")
        
    log_debug("PPQ logger completed successfully")


if __name__ == '__main__':
    main()