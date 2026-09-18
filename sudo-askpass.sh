#!/bin/sh
# sudo askpass helper — reads the LIVE value at call time.
# Hardcoding it here was a leak vector and broke silently when the login
# password was rotated (2026-09-16). Never inline the secret again.
exec grep -m1 '^SUDO_PASSWORD=' "$HOME/.hermes/.env" | cut -d= -f2-
