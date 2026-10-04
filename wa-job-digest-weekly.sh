#!/usr/bin/env bash
# Cron entrypoint for the weekly job digest.
#
# The real work lives in the private repo (it needs the posting store, the
# feedback file and the source modules):
#   /home/c03rad0r/repos/work-and-apartment/scripts/wa_job_digest_weekly.sh
#
# That script prints the GROUP-READY digest on stdout — or, when the sweep
# collected nothing, exactly one error line. A no_agent cron delivers stdout
# verbatim, so empty/failed runs never masquerade as good news.
#
# Knobs are documented in the repo script; they are read-only for this wrapper.
exec /home/c03rad0r/repos/work-and-apartment/scripts/wa_job_digest_weekly.sh
