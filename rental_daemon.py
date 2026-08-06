"""
ЛОКАЛЬНЫЙ запуск бота поиска жилья на домашнем ПК.

Зачем: на GitHub Actions Property24 доступен только через ScraperAPI, а его
бесплатный тариф (~1000 запросов/мес) при прогоне раз в 4 часа кончается
примерно за неделю. После этого Property24 молча отваливается, и бот перестаёт
присылать объявления — при том, что все прогоны формально «успешны».

С домашнего IP Property24 открывается НАПРЯМУЮ, без ScraperAPI и без лимитов.
Этот скрипт крутится на домашнем ПК, сам запускает bot.run_once() раз в
RUN_EVERY_HOURS часов и пушит обновлённый seen_listings.json в GitHub.
Тот же принцип, что у control_daemon.py.

Workflow rental-bot.yml на GitHub оставлен резервом (раз в сутки) — на случай
выключенного ПК.

Запуск: run_rental_hidden.vbs (скрыто, без окна) либо `python rental_daemon.py`.
Проверка живости: файл rental_heartbeat.txt (метка времени в UTC).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

SECRETS = HERE / "secrets_local.py"
HEARTBEAT = HERE / "rental_heartbeat.txt"
LOG = HERE / "rental_daemon.log"
# Выключатель: появился файл STOP рядом со скриптом — демон аккуратно выходит.
# Убить процесс со стороны (из другой сессии Windows) нельзя, а файл создать можно.
STOP_FLAG = HERE / "STOP"
TRACKED = ["seen_listings.json"]

RUN_EVERY_HOURS = 4     # как часто искать объявления
RETRY_MINUTES = 45      # если Property24 отдал 503 всему IP — повтор через столько
BEAT_EVERY = 30         # как часто обновлять heartbeat, сек
LOG_MAX = 1_000_000     # обрезать лог, если разросся


def log(msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            LOG.write_text("", encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def beat():
    try:
        HEARTBEAT.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


_lock_handle = None   # держим открытым всё время жизни процесса, иначе замок спадёт


def take_lock(lock_path):
    """Замок «только один экземпляр». Windows снимает его САМ, когда процесс
    умирает, — поэтому после taskkill не остаётся залипшего состояния (проверка
    по heartbeat этим как раз плоха: файл ещё «свежий», а процесса уже нет).
    Возвращает True, если замок наш."""
    global _lock_handle
    try:
        import msvcrt
    except ImportError:
        return True   # не Windows — проверку пропускаем
    try:
        _lock_handle = open(lock_path, "a+")
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


# --- секреты: читаем ДО импорта bot.py (он берёт их из окружения) ---
if not SECRETS.exists():
    log("НЕТ secrets_local.py — создай его и впиши TELEGRAM_TOKEN")
    sys.exit(1)

ns = {}
exec(SECRETS.read_text(encoding="utf-8"), ns)
token = str(ns.get("TELEGRAM_TOKEN", "")).strip()
chat_id = str(ns.get("TELEGRAM_CHAT_ID", "")).strip()

if not token or not chat_id:
    log("В secrets_local.py не заполнены TELEGRAM_TOKEN / TELEGRAM_CHAT_ID — выход")
    sys.exit(1)

os.environ["TELEGRAM_TOKEN"] = token
os.environ["TELEGRAM_CHAT_ID"] = chat_id
# ВАЖНО: ключ ScraperAPI намеренно НЕ задаём — с домашнего IP ходим напрямую,
# бесплатно и без лимитов. Так bot.py включает DIRECT_MODE.
os.environ.pop("SCRAPER_API_KEY", None)
os.environ.pop("SCRAPE_PROXY", None)
os.environ["ALERT_ON_SOURCE_DOWN"] = "1"

import bot  # noqa: E402  — импорт строго после установки переменных окружения


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def push_changes():
    """Отправляем обновлённый seen_listings.json в GitHub, чтобы резервный
    прогон на Actions не слал те же объявления повторно."""
    st = git("status", "--porcelain", "--", *TRACKED)
    if not st.stdout.strip():
        return False
    git("add", "--", *TRACKED)
    git("commit", "-m", "Update seen listings (local)")
    for attempt in range(3):
        git("pull", "--rebase", "origin", "main")
        p = git("push", "origin", "main")
        if p.returncode == 0:
            log("seen_listings.json отправлен в GitHub")
            return True
        log(f"push не прошёл (попытка {attempt + 1}): {p.stderr.strip()[:200]}")
        time.sleep(3)
    return False


def do_run(p24_down_streak=0):
    log("--- прогон поиска начат ---")
    # Предупреждаем в Telegram только если Property24 молчит ВТОРОЙ прогон подряд:
    # одиночный 503 — обычное дело (сайт временно придерживает частые запросы),
    # дёргать пользователя из-за него незачем.
    bot.ALERT_ON_SOURCE_DOWN = p24_down_streak >= 1
    # Подтянуть свежие настройки и seen с GitHub (их мог изменить
    # control_daemon или резервный прогон на Actions).
    git("pull", "--rebase", "origin", "main")
    bot._apply_config()

    stats = bot.run_once()
    if stats:
        log(f"Property24 карточек={stats['p24_cards']} · "
            f"Private Property={stats['pp_cards']} · "
            f"новых кандидатов={stats['candidates']} · "
            f"прошло фильтр={stats['passed']} · отправлено={stats['sent']} · "
            f"отложено={stats['postponed']}")
    push_changes()
    log("--- прогон завершён ---")
    return stats


def main():
    if not take_lock(HERE / "rental_daemon.lock"):
        log("демон уже запущен — второй экземпляр не нужен, выход")
        return

    log("=== локальный поиск жилья ЗАПУЩЕН ===")
    beat()

    next_run = 0.0          # первый прогон — сразу при старте
    last_beat = 0.0
    p24_down_streak = 0     # сколько прогонов подряд Property24 молчит
    while True:
        if STOP_FLAG.exists():
            try:
                STOP_FLAG.unlink()
            except OSError:
                pass
            log("получен сигнал остановки (файл STOP) — выхожу")
            return

        now = time.time()
        if now >= next_run:
            stats = None
            try:
                stats = do_run(p24_down_streak)
            except Exception as e:
                log(f"ошибка прогона: {type(e).__name__}: {e}")

            # Если Property24 временно закрылся (503 на весь IP) — не ждём
            # полные 4 часа, пробуем ещё раз через RETRY_MINUTES.
            if stats and stats["p24_cards"] == 0:
                p24_down_streak += 1
                next_run = time.time() + RETRY_MINUTES * 60
                log(f"Property24 не ответил ({p24_down_streak}-й раз подряд) — "
                    f"повтор через {RETRY_MINUTES} мин")
            else:
                p24_down_streak = 0
                next_run = time.time() + RUN_EVERY_HOURS * 3600
            log(f"следующий прогон примерно в "
                f"{time.strftime('%H:%M', time.localtime(next_run))}")

        if now - last_beat >= BEAT_EVERY:
            beat()
            last_beat = now
        time.sleep(10)


if __name__ == "__main__":
    main()
