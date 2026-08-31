#!/usr/bin/env python3
"""ppq_logger_freq_control.py — PPQ logger with frequency control.

Only makes API calls if at least 30 minutes have passed since last successful call.
This prevents API abuse while keeping the cron job schedule.
"""

import os, json, urllib.request, urllib.error, sqlite3, time, sys
from pathlib import Path
from datetime import datetime, timezone

PPQ_API_KEY = os.environ.get('PPQ_API_KEY', '')
DB = Path.home() / '.hermes' / 'bot' / 'zai_usage.db'
STATE_FILE = Path.home() / '.local' / 'state' / 'ppq_logger_last_ts.txt'
FREQ_STATE_FILE = Path.home() / '.local' / 'state' / 'ppq_logger_last_run.txt'

# Configuration
MIN_INTERVAL_MINUTES = 30  # Minimum 30 minutes between API calls
API_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2


def should_run_api_call():
    """Check if enough time has passed since last API call."""
    if not FREQ_STATE_FILE.exists():
        return True  # First run, allow API call
    
    try:
        last_run_ts = float(FREQ_STATE_FILE.read_text().strip())
        current_ts = time.time()
        elapsed_minutes = (current_ts - last_run_ts) / 60
        
        return elapsed_minutes >= MIN_INTERVAL_MINUTES
    except (ValueError, TypeError):
        return True  # Invalid timestamp, allow API call


def record_api_run():
    """Record that we made an API call."""
    try:
        FREQ_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        FREQ_STATE_FILE.write_text(str(time.time()))
    except Exception:
        pass  # Don't fail if we can't write state


def silent_print(msg):
    """Only print meaningful messages."""
    if os.environ.get('CRON') == 'true':
        if any(keyword in msg.lower() for keyword in ['error', 'logged', 'new', 'failed']):
            print(f"PPQ: {msg}", flush=True)
    else:
        print(f"PPQ: {msg}", flush=True)


def fetch_ppq_history(since_ts=None, limit=50):
    """Fetch PPQ history."""
    url = f'https://api.ppq.ai/queries/history?limit={limit}'
    
    if since_ts:
        try:
            dt = datetime.fromtimestamp(float(since_ts), tz=timezone.utc)
            iso_date = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            url += f'&start_date={iso_date}'
        except (ValueError, TypeError):
            pass

    headers = {
        'Authorization': f'Bearer {PPQ_API_KEY}',
        'User-Agent': 'ppq-logger-freq/1.0'
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as r:
                if r.status == 200:
                    data = json.loads(r.read())
                    return data.get('data', [])
                elif r.status == 500:
                    if attempt == MAX_RETRIES - 1:
                        silent_print("API 500 error")
                    time.sleep(2 ** attempt)
                    continue
                elif r.status == 429:
                    silent_print("API rate limit exceeded")
                    time.sleep(10)
                    continue
                else:
                    return []
                    
        except urllib.error.URLError as e:
            if attempt == MAX_RETRIES - 1:
                silent_print(f"Network error: {e}")
            time.sleep(2 ** attempt)
            continue
        except Exception as e:
            silent_print(f"Fetch failed: {e}")
            return []
    
    return []


def parse_ppq_ts(ts_str):
    """Parse PPQ timestamp."""
    try:
        dt = datetime.strptime(ts_str.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S.%f%z')
        return dt.timestamp()
    except ValueError:
        try:
            dt = datetime.strptime(ts_str.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S%z')
            return dt.timestamp()
        except ValueError:
            return None


def main():
    """Main function with frequency control."""
    
    if not PPQ_API_KEY:
        silent_print("PPQ_API_KEY not set")
        return

    # Check if we should make an API call
    if not should_run_api_call():
        return  # Silent exit - not time yet

    # Record that we're making an API call now
    record_api_run()

    # Get last synced timestamp
    last_ts = None
    if STATE_FILE.exists():
        try:
            last_ts = STATE_FILE.read_text().strip()
        except:
            pass

    # Fetch history
    queries = fetch_ppq_history(since_ts=last_ts, limit=100)
    
    if not queries:
        return  # Silent exit - no data or API error

    # Connect to database
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB))
    except Exception as e:
        silent_print(f"Database error: {e}")
        return

    # Get existing timestamps
    try:
        existing = set(r[0] for r in conn.execute(
            "SELECT ts FROM api_calls WHERE key_name='ppq'"
        ).fetchall())
    except Exception:
        existing = set()

    new_count = 0
    latest_ts = last_ts

    for q in queries:
        try:
            ts = parse_ppq_ts(q.get('timestamp', ''))
            if ts is None:
                continue

            # Check for duplicates
            if any(abs(ts - e) < 2 for e in existing):
                continue

            # Extract data
            model = q.get('model', 'unknown')
            prompt_tokens = q.get('input_count', 0) or q.get('input_tokens', 0) or 0
            completion_tokens = q.get('output_count', 0) or q.get('output_tokens', 0) or 0
            total_tokens = q.get('total_tokens', prompt_tokens + completion_tokens) or 0
            cache_hit = q.get('cached', False) or q.get('cache_hit', False)

            # Insert
            key_suffix = PPQ_API_KEY[-8:] if PPQ_API_KEY else 'ppq'
            
            conn.execute("""
                INSERT OR IGNORE INTO api_calls
                (ts, key_name, key_suffix, model, prompt_tokens,
                 completion_tokens, total_tokens, tier, cache_hit,
                 ollama_hit, ppq_hit, status_code, error, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ts, 'ppq', key_suffix, model, prompt_tokens,
                completion_tokens, total_tokens, 'ppq', int(cache_hit),
                0, 1, 200, None, 2000
            ))
            
            new_count += 1

            if not latest_ts or ts > float(latest_ts):
                latest_ts = str(ts)
                
        except Exception:
            continue

    # Finalize
    try:
        conn.commit()
        
        if new_count > 0:
            silent_print(f"Logged {new_count} new PPQ calls")
            
        if latest_ts and new_count > 0:
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(latest_ts)
            except Exception:
                pass
                
    except Exception as e:
        silent_print(f"Database error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()