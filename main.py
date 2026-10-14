"""Бот перевірки товарів «Ділова Ковбаса».

  python main.py                 — запуск за розкладом (Railway)
  python main.py --once          — одна перевірка зараз
  python main.py --dry-run       — перевірка без Telegram і без збереження стану
  python main.py --dry-run --file prom_export.xlsx   — з локального файлу
"""
import argparse
import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

from checker import due, run_check  # noqa: E402  (після load_dotenv)
from state import State  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def scheduled_check():
    """Тригер спрацьовує щодня, але перевірка йде раз на CHECK_EVERY_DAYS днів
    (за датою останнього успішного запуску зі стану — переживає перезапуски)."""
    try:
        if not due(State(os.getenv("STATE_PATH", "state.json"))):
            log.info("Рано: з останньої перевірки минуло менше %s дн. — пропускаю",
                     os.getenv("CHECK_EVERY_DAYS", "2"))
            return
        run_check()
    except Exception:
        log.exception("Збій перевірки")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file")
    args = p.parse_args()

    if args.once or args.dry_run or args.file:
        run_check(args.file, dry_run=args.dry_run)
        return

    hours = os.getenv("SCHEDULE_HOURS", "8")
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    sched = BlockingScheduler(timezone="Europe/Kyiv")
    sched.add_job(scheduled_check, "cron", hour=hours, minute=minute, id="check",
                  misfire_grace_time=3600, coalesce=True)
    log.info("Планувальник запущено: о %s:%02d (Europe/Kyiv), перевірка раз на %s дн.", hours, minute,
             os.getenv("CHECK_EVERY_DAYS", "2"))
    if os.getenv("RUN_ON_START", "true").lower() == "true":
        scheduled_check()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Зупинено.")


if __name__ == "__main__":
    main()
