#!/usr/bin/env bash
# Cron wrapper (lives in ~/.hermes/scripts/ per cronjob requirement).
# Delegates to the canonical, version-controlled script in the repo so there
# is a single source of truth. RP-5 STEP 1: every-5min RealtimePricing refresh.
exec bash /home/c03rad0r/merchant-routing-engine/scripts/realtime_pricing_cron.sh
