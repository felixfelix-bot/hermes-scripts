#!/usr/bin/env python3
"""
schedule-to-kanban — Automated pipeline from .hermes/tasks/schedule.json to kanban database

This script imports tasks from schedule.json into the kanban database for dispatching.
It handles deduplication and sets appropriate defaults for Continuum tasks.

Usage:
  python3 schedule-to-kanban.py [--board torii-continuum] [--verbose]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
import uuid
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_schedule_json(schedule_path):
    """Load tasks from schedule.json"""
    if not schedule_path.exists():
        logger.info(f"Schedule file not found: {schedule_path}")
        return []
    
    try:
        with open(schedule_path) as f:
            data = json.load(f)
            return data.get('tasks', [])
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading schedule.json: {e}")
        return []

def create_task_db_connection(board_path):
    """Create connection to kanban database"""
    db_path = board_path / 'kanban.db'
    if not db_path.exists():
        logger.error(f"Kanban database not found: {db_path}")
        return None
    
    return sqlite3.connect(str(db_path))

def get_existing_task_ids(conn):
    """Get list of existing task IDs to avoid duplicates"""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tasks")
        return {row[0] for row in cursor.fetchall()}
    except sqlite3.Error as e:
        logger.error(f"Error getting existing task IDs: {e}")
        return set()

def create_task(conn, task_data, board_name, default_assignee='worker-continuum'):
    """Insert a task into the kanban database"""
    task_id = task_data.get('id', str(uuid.uuid4()))
    title = task_data.get('title', 'Untitled Task')
    body = task_data.get('body', '')
    priority = task_data.get('priority', 1)
    assignee = task_data.get('assignee', default_assignee)
    
    # Generate ID if not provided
    if 'id' not in task_data:
        # Use a predictable ID based on title for consistency
        task_id = f"{board_name[:8]}-{hash(title) & 0xffffffff:08x}"
    
    try:
        cursor = conn.cursor()
        
        # Check if task already exists
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if cursor.fetchone():
            logger.debug(f"Task already exists: {task_id} - {title}")
            return None
        
        # Insert the task
        now = int(datetime.now().timestamp())
        cursor.execute("""
            INSERT INTO tasks (
                id, title, body, assignee, status, priority, 
                created_by, created_at, goal_mode, workspace_kind,
                max_retries, goal_max_turns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, title, body, assignee, 'ready', priority,
            'schedule_import', now, 0, 'scratch', 3, 20
        ))
        
        logger.info(f"Created task: {task_id} - {title}")
        return task_id
        
    except sqlite3.Error as e:
        logger.error(f"Error creating task {title}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description='Import tasks from schedule.json to kanban database')
    parser.add_argument('--board', default='torii-continuum', 
                       help='Kanban board name (default: torii-continuum)')
    parser.add_argument('--schedule-path', 
                       default=Path.home() / '.hermes' / 'tasks' / 'schedule.json',
                       help='Path to schedule.json file')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose logging')
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Paths
    schedule_path = Path(args.schedule_path)
    boards_dir = Path.home() / '.hermes' / 'kanban' / 'boards'
    board_path = boards_dir / args.board
    
    if not board_path.exists():
        logger.error(f"Board directory not found: {board_path}")
        return 1
    
    # Load tasks from schedule.json
    logger.info(f"Loading tasks from: {schedule_path}")
    tasks = load_schedule_json(schedule_path)
    
    if not tasks:
        logger.info("No tasks found in schedule.json")
        return 0
    
    # Connect to database
    conn = create_task_db_connection(board_path)
    if not conn:
        return 1
    
    try:
        # Get existing task IDs
        existing_ids = get_existing_task_ids(conn)
        
        created_count = 0
        for task_data in tasks:
            task_id = create_task(conn, task_data, args.board)
            if task_id:
                created_count += 1
        
        conn.commit()
        logger.info(f"Import complete: {created_count} new tasks created")
        
        # Clean up schedule.json if import was successful
        if created_count > 0:
            try:
                # Create backup
                backup_path = schedule_path.with_suffix('.json.backup')
                if schedule_path.exists():
                    schedule_path.rename(backup_path)
                logger.info(f"Backed up schedule.json to: {backup_path}")
            except IOError as e:
                logger.warning(f"Could not backup schedule.json: {e}")
        
    finally:
        conn.close()
    
    return 0

if __name__ == '__main__':
    sys.exit(main())