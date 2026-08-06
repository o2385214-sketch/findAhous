"""
ЛОКАЛЬНЫЙ обработчик команд Telegram для бота поиска жилья.

Зачем: workflow bot-control.yml на GitHub настроен на «раз в 10 минут», но
GitHub режет частые расписания — реально он срабатывает раз в 3-4 часа (ночью
до 10). Из-за этого команды вроде /мебель выкл применялись через полдня.
Этот скрипт крутится на домашнем ПК, опрашивает Telegram непрерывно и сразу
пушит новые настройки в GitHub — команда применяется за секунды.

Workflow на GitHub оставлен как резерв на случай выключенного ПК.

Запуск: run_control_hidden.vbs (скрыто, без окна) либо `python control_daemon.py`.
Проверка живости: файл control_heartbeat.txt (метка времени в UTC, как у лид-бота).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

SECRETS = HERE / "secrets_local.py"
HEARTBEAT = HERE / "control_heartbeat.txt"
LOG = HERE / "control_daemon.log"
TRACKED = ["config.json", "tg_state.json"]

POLL_PAUSE = 2      # пауза между опросами (сам getUpdates висит до 10 сек)
BEAT_EVERY = 30     # как часто обновлять heartbeat, сек
LOG_MAX = 1_000_000  # обрезать лог, если разросся


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


# --- секреты: читаем ДО импорта control.py (он берёт их из окружения) ---
if not SECRETS.exists():
    log("НЕТ secrets_local.py — создай его и впиши TELEGRAM_TOKEN")
    sys.exit(1)

ns = {}
exec(SECRETS.read_text(encoding="utf-8"), ns)
token = str(ns.get("TELEGRAM_TOKEN", "")).strip()
chat_id = str(ns.get("TELEGRAM_CHAT_ID", "")).strip()

if not token or token.startswith("СЮДА"):
    log("В secrets_local.py не вписан TELEGRAM_TOKEN — выход")
    sys.exit(1)

os.environ["TELEGRAM_TOKEN"] = token
os.environ["TELEGRAM_CHAT_ID"] = chat_id

import control  # noqa: E402  — импорт строго после установки переменных окружения


def save_chat_id(new_id):
    """Первый раз узнали свой chat_id — записываем в secrets_local.py навсегда."""
    try:
        text = SECRETS.read_text(encoding="utf-8")
        text = text.replace('TELEGRAM_CHAT_ID = ""', f'TELEGRAM_CHAT_ID = "{new_id}"')
        SECRETS.write_text(text, encoding="utf-8")
        os.environ["TELEGRAM_CHAT_ID"] = str(new_id)
        control.TELEGRAM_CHAT_ID = str(new_id)
        log(f"определил и сохранил TELEGRAM_CHAT_ID = {new_id}")
    except OSError as e:
        log(f"не смог сохранить chat_id: {e}")


def detect_chat_id():
    """Пока chat_id неизвестен — подсматриваем его в первом входящем сообщении."""
    import requests
    try:
        r = requests.get(f"{control.API}/getUpdates",
                         params={"offset": 0, "timeout": 0, "limit": 1},
                         timeout=20)
        for upd in r.json().get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            cid = msg.get("chat", {}).get("id")
            if cid:
                return str(cid)
    except Exception as e:
        log(f"detect_chat_id: {e}")
    return None


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def push_changes():
    """Если команда изменила config.json / tg_state.json — отправляем в GitHub."""
    st = git("status", "--porcelain", "--", *TRACKED)
    if not st.stdout.strip():
        return False
    git("add", "--", *TRACKED)
    git("commit", "-m", "Update settings from Telegram (local)")
    for attempt in range(3):
        git("pull", "--rebase", "origin", "main")
        p = git("push", "origin", "main")
        if p.returncode == 0:
            log("настройки отправлены в GitHub")
            return True
        log(f"push не прошёл (попытка {attempt + 1}): {p.stderr.strip()[:200]}")
        time.sleep(3)
    return False


_lock_handle = None   # держим открытым всё время жизни процесса, иначе замок спадёт


def take_lock(lock_path):
    """Замок «только один экземпляр». Ловушка из жизни (2026-08-06): автозапуск
    Windows поднял демона, и почти одновременно его запустили руками — два
    процесса дёргали getUpdates и мешали друг другу (Telegram отдаёт 409 одному
    из них). Windows снимает замок САМ при гибели процесса, поэтому после
    taskkill залипшего состояния не остаётся."""
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


def main():
    if not take_lock(HERE / "control_daemon.lock"):
        log("демон уже запущен — второй экземпляр не нужен, выход")
        return

    log("=== локальный обработчик команд ЗАПУЩЕН ===")
    beat()
    git("pull", "--rebase", "origin", "main")   # подтянуть свежий config с GitHub

    last_beat = time.time()
    while True:
        try:
            if not os.environ.get("TELEGRAM_CHAT_ID"):
                found = detect_chat_id()
                if found:
                    save_chat_id(found)

            control.main()          # опрос Telegram + применение команд к config.json
            push_changes()
        except Exception as e:
            log(f"ошибка цикла: {type(e).__name__}: {e}")
            time.sleep(5)

        now = time.time()
        if now - last_beat >= BEAT_EVERY:
            beat()
            last_beat = now
        time.sleep(POLL_PAUSE)


if __name__ == "__main__":
    main()
