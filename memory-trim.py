#!/usr/bin/env python3
"""Safe memory trimmer: kills non-essential swap hogs, surfaces doubts."""

import argparse, json, os, sys
from pathlib import Path

SAFE = {'pyright','typescript-language-server','rust-analyzer','gopls',
        'lua-language-server','pylance','jedi-language-server',
        'vitest','jest','mocha','playwright','pytest',
        'npm','yarn','deno','bun'}
DOUBTFUL = {'hermes','agent','python3','signal-cli','java'}
UNSAFE = {'gnome-terminal','firefox','chrome','chromium','code',
          'sshd','bash','zsh','wireguard','openvpn','netbird'}

def get_swap_hogs(threshold_mb=300):
    hogs = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit(): continue
        try:
            status = Path(f'/proc/{pid}/status').read_text()
        except: continue
        swap = 0; name = '?'
        for line in status.split('\n'):
            if line.startswith('VmSwap:'):
                parts = line.split()
                if len(parts)>=2 and parts[1].isdigit(): swap = int(parts[1])
            elif line.startswith('Name:'):
                parts = line.split('\t')
                if len(parts)>=2: name = parts[-1].strip()
        if swap <= threshold_mb*1024: continue
        try:
            cmd = Path(f'/proc/{pid}/cmdline').read_text().replace('\x00',' ').strip()[:200]
        except: cmd = ''
        hogs.append({'pid':int(pid),'name':name,'swap_mb':round(swap/1024,1),'cmdline':cmd})
    hogs.sort(key=lambda x:x['swap_mb'],reverse=True)
    return hogs

def cat(proc):
    n=proc['name'].lower(); c=proc['cmdline'].lower()
    for p in UNSAFE: 
        if p in n or p in c: return 'UNSAFE','Protected: '+p
    for p in SAFE: 
        if p in n or p in c: return 'SAFE','Safe: '+p
    for p in DOUBTFUL: 
        if p in n or p in c: return 'DOUBTFUL','Review: '+p
    return ('DOUBTFUL','High swap, unsure') if proc['swap_mb']>500 else ('SAFE','Low swap')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--threshold',type=float,default=300)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--json',action='store_true')
    a=p.parse_args()
    
    hogs=get_swap_hogs(a.threshold)
    groups={'safe':[],'doubtful':[],'unsafe':[]}
    total=0
    
    for h in hogs:
        c,r=cat(h)
        groups[c.lower()].append(h)
        total+=h['swap_mb']
        icon={'SAFE':'[OK]','DOUBTFUL':'[?]','UNSAFE':'[NO]'}[c]
        print(f"{icon} PID {h['pid']:<6} {h['name']:<20} {h['swap_mb']:>8.1f}MB  {r}")
        if h['cmdline']: print(f"     {h['cmdline'][:120]}")
    
    freed=0
    for h in groups['safe']:
        if a.dry_run:
            print(f"  [DRY] kill {h['pid']} ({h['name']}, {h['swap_mb']}MB)")
            freed+=h['swap_mb']
        else:
            try: os.kill(h['pid'],15); freed+=h['swap_mb']; print(f"  Killed {h['name']} PID {h['pid']} (+{h['swap_mb']}MB)")
            except: print(f"  FAILED: kill {h['pid']}")
    
    if freed>0: print(f"\nFreed {freed:.1f}MB swap\n")
    
    if groups['doubtful']:
        print(f"DOUBTFUL ({len(groups['doubtful'])}) - needs your review:")
        for h in groups['doubtful']:
            print(f"  kill {h['pid']}  # {h['name']} ({h['swap_mb']}MB)")
    
    if groups['unsafe']:
        print(f"UNSAFE ({len(groups['unsafe'])}) - NOT killed:")
        for h in groups['unsafe']:
            print(f"  {h['name']} PID {h['pid']} ({h['swap_mb']}MB)")
    
    if a.json: print(json.dumps({'hogs':hogs,'freed_mb':freed,'doubtful':groups['doubtful']},indent=2))

if __name__=='__main__': main()
