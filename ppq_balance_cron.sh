#!/usr/bin/env bash
# Cron wrapper (lives in ~/.hermes/scripts/ per cronjob requirement).
# Delegates to the canonical, version-controlled script in the repo so there
# is a single source of truth. P3-PPQ STEP 2: every-5min PPQ balance collector.
exec bash /home/c03rad0r/merchant-routing-engine/scripts/ppq_balance_cron.sh
