#!/usr/bin/env python3
import json
import sqlite3
import time
import uuid

# Read the schedule
with open('/home/c03rad0r/repos/torii-continuum/.hermes/tasks/schedule.json') as f:
    schedule = json.load(f)

# Connect to board database
db_path = '/home/c03rad0r/.hermes/kanban/boards/torii-continuum/kanban.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

tasks = schedule['tasks']
now = int(time.time())

# Build ID mapping: short id -> UUID
id_map = {}
for t in tasks:
    # Use a deterministic UUID based on the short id
    id_map[t['id']] = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"continuum-browser-keygen-{t['id']}"))

# Priority mapping
priority_map = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

created = 0
skipped = 0
links = []

for t in tasks:
    task_id = id_map[t['id']]
    
    # Check if already exists
    cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
    if cursor.fetchone():
        skipped += 1
        continue
    
    # Determine status based on dependencies
    deps = t.get('dependencies', [])
    status = 'ready' if not deps else 'blocked'
    
    # Map assignee
    assignee = 'worker'  # default valid assignee
    
    # Priority
    priority = priority_map.get(t['priority'], 99)
    
    # Body with full description
    body = t['description']
    if t.get('estimated_hours'):
        body += f"\n\nEstimated: {t['estimated_hours']}h"
    
    cursor.execute('''
        INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, 
                          workspace_kind, consecutive_failures, goal_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scratch', 0, 0)
    ''', (task_id, t['title'], body, assignee, status, priority, 'felix-scheduler', now))
    
    # Record links
    for dep in deps:
        dep_id = id_map.get(dep)
        if dep_id:
            links.append((dep_id, task_id))  # parent = dep, child = this task
    
    created += 1

# Create dependency links
for parent_id, child_id in links:
    try:
        cursor.execute('INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)',
                      (parent_id, child_id))
    except sqlite3.IntegrityError:
        pass  # link already exists

conn.commit()
conn.close()

print(f"Created {created} tasks in torii-continuum board")
print(f"Skipped {skipped} (already exist)")
print(f"Created {len(links)} dependency links")
print(f"\nTask ID map (for reference):")
for t in tasks:
    short_id = t['id']
    uuid_id = id_map[t['id']]
    print(f"  {short_id} -> {uuid_id}")