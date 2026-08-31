#!/usr/bin/env bash
# bitrouter GitHub release monitor — no_agent cron script
# Alerts on: new releases, beta/stable tags, breaking changes, activity drop
# Schedule: weekly (Monday 09:00 UTC)
# Silent on no changes (empty stdout = no alert)

set -u

REPO="bitrouter/bitrouter"
STATE_FILE="${HOME}/.hermes/cron/bitrouter_state.json"
API_BASE="https://api.github.com/repos/${REPO}"
alerts=()

# --- fetch releases (ETag caching) ---
ETAG=""
if [ -f "${STATE_FILE}" ]; then
  ETAG=$(python3 -c "import json,sys; d=json.load(open('${STATE_FILE}')); print(d.get('etag',''))" 2>/dev/null || echo "")
fi

RELEASE_RESP=$(curl -sS -w "\n%{http_code}" \
  -H "Accept: application/vnd.github+json" \
  ${ETAG:+-H "If-None-Match: ${ETAG}"} \
  "${API_BASE}/releases?per_page=10" 2>/dev/null)

HTTP_CODE=$(echo "${RELEASE_RESP}" | tail -1)
BODY=$(echo "${RELEASE_RESP}" | sed '$d')

if [ "${HTTP_CODE}" = "304" ]; then
  : # silent — no change
elif [ "${HTTP_CODE}" = "200" ]; then
  NEW_ETAG=$(echo "${BODY}" | python3 -c "
import json, sys, hashlib
data = json.load(sys.stdin)
etag = hashlib.md5(json.dumps(data).encode()).hexdigest()
print(etag)
" 2>/dev/null)

  LAST_VERSION=""
  if [ -f "${STATE_FILE}" ]; then
    LAST_VERSION=$(python3 -c "import json; d=json.load(open('${STATE_FILE}')); print(d.get('last_version',''))" 2>/dev/null || echo "")
  fi

  PARSED=$(echo "${BODY}" | python3 -c "
import json, sys

data = json.load(sys.stdin)
if not isinstance(data, list) or not data:
    print('NO_RELEASES')
    sys.exit(0)

latest = data[0]
tag = latest.get('tag_name', 'unknown')
body = latest.get('body', '')
prerelease = latest.get('prerelease', True)

breaking_keywords = ['breaking', 'migration', 'api change', 'deprecated', 'removed', 'incompatible']
breaking = [kw for kw in breaking_keywords if kw in body.lower()]

is_beta = 'beta' in tag.lower()
is_stable = not prerelease and 'alpha' not in tag.lower() and 'beta' not in tag.lower()

print(f'TAG:{tag}')
print(f'PRERELEASE:{prerelease}')
print(f'IS_BETA:{is_beta}')
print(f'IS_STABLE:{is_stable}')
print(f'BREAKING:{\",\".join(breaking) if breaking else \"none\"}')
print(f'RELEASE_NOTES:{body[:500]}')
" 2>/dev/null)

  TAG=$(echo "${PARSED}" | grep '^TAG:' | cut -d: -f2-)
  IS_BETA=$(echo "${PARSED}" | grep '^IS_BETA:' | cut -d: -f2)
  IS_STABLE=$(echo "${PARSED}" | grep '^IS_STABLE:' | cut -d: -f2)
  BREAKING=$(echo "${PARSED}" | grep '^BREAKING:' | cut -d: -f2-)
  NOTES=$(echo "${PARSED}" | grep '^RELEASE_NOTES:' | cut -d: -f2-)

  if [ "${TAG}" != "${LAST_VERSION}" ] && [ -n "${TAG}" ]; then
    alerts+=("NEW RELEASE: ${TAG} (was: ${LAST_VERSION:-none})")
    [ "${IS_BETA}" = "True" ] && alerts+=("BETA TAG DETECTED — consider escalating to Phase 2")
    [ "${IS_STABLE}" = "True" ] && alerts+=("STABLE RELEASE — escalate to Felix for Phase 2 decision")
    [ "${BREAKING}" != "none" ] && alerts+=("BREAKING CHANGES: ${BREAKING}")
    alerts+=("Notes: ${NOTES}")
  fi

  python3 -c "
import json
state = {'last_version': '${TAG}', 'etag': '${NEW_ETAG}'}
json.dump(state, open('${STATE_FILE}', 'w'))
" 2>/dev/null
elif [ "${HTTP_CODE}" = "403" ]; then
  alerts+=("GitHub API rate limited — check again later")
fi

# --- check commit activity ---
COMMIT_RESP=$(curl -sS \
  -H "Accept: application/vnd.github+json" \
  "${API_BASE}/commits?per_page=1" 2>/dev/null)

LAST_COMMIT_DATE=$(echo "${COMMIT_RESP}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        date = data[0]['commit']['committer']['date']
        print(date[:10])
    else:
        print('unknown')
except:
    print('error')
" 2>/dev/null)

if [ -n "${LAST_COMMIT_DATE}" ] && [ "${LAST_COMMIT_DATE}" != "unknown" ] && [ "${LAST_COMMIT_DATE}" != "error" ]; then
  DAYS_SINCE=$(python3 -c "
from datetime import datetime, date
last = datetime.strptime('${LAST_COMMIT_DATE}', '%Y-%m-%d').date()
delta = (date.today() - last).days
print(delta)
" 2>/dev/null || echo "0")

  if [ "${DAYS_SINCE}" -gt 21 ]; then
    alerts+=("DORMANT: no commits in ${DAYS_SINCE} days (last: ${LAST_COMMIT_DATE})")
  fi

  if [ -f "${STATE_FILE}" ]; then
    python3 -c "
import json
d = json.load(open('${STATE_FILE}'))
d['last_commit_date'] = '${LAST_COMMIT_DATE}'
d['days_since_commit'] = ${DAYS_SINCE}
json.dump(d, open('${STATE_FILE}', 'w'))
" 2>/dev/null
  fi
fi

# --- output ---
if [ ${#alerts[@]} -gt 0 ]; then
  echo "# bitrouter Watch ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  echo ""
  for a in "${alerts[@]}"; do
    echo "  - ${a}"
  done
  echo ""
  echo "  Repo: https://github.com/${REPO}"
  echo "  State: ${STATE_FILE}"
fi
# Empty output = silent (no changes detected)