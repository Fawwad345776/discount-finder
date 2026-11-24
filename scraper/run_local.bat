@echo off
REM Instagram + Facebook scraper for your laptop (home internet is blocked less than GitHub).
REM Put service-account.json in this "scraper" folder first. Never upload that file anywhere.
cd /d "%~dp0"
python -m pip install --quiet requests cryptography
set ONLY_SOCIAL=1
set MAX_MINUTES=60
python scrape.py
