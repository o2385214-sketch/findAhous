# -*- coding: utf-8 -*-
"""Разовый прогон поиска вручную (расписание при этом остаётся выключенным)."""
import os, sys, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ns = {}
exec(pathlib.Path("secrets_local.py").read_text(encoding="utf-8"), ns)
os.environ["TELEGRAM_TOKEN"] = str(ns.get("TELEGRAM_TOKEN", "")).strip()
os.environ["TELEGRAM_CHAT_ID"] = str(ns.get("TELEGRAM_CHAT_ID", "")).strip()
for k in ("SCRAPER_API_KEY", "SCRAPE_PROXY"):
    if ns.get(k):
        os.environ[k] = str(ns[k]).strip()
sys.path.insert(0, ".")
import bot
print("DIRECT_MODE =", bot.DIRECT_MODE, "| страниц на категорию:", bot.PAGES_PER_SEARCH)
print("уже в seen:", len(bot.load_seen()))
stats = bot.run_once()
print("ИТОГ:", stats)
