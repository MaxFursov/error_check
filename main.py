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
import weight_report  # noqa: E402
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


def scheduled_weight_report():
    """Щодня перевіряє, чи минуло WEIGHT_REPORT_EVERY_DAYS днів з останнього звіту про вагу."""
    try:
        state = State(os.getenv("STATE_PATH", "state.json"))
        if weight_report.adopt_existing_state(state):
            log.info("Звіт про вагу: бот оновлено — відлік %s дн. починається сьогодні",
                     os.getenv("WEIGHT_REPORT_EVERY_DAYS", "3"))
            return
        if not weight_report.due(state):
            log.info("Звіт про вагу: ще рано — пропускаю")
            return
        weight_report.run_weight_report()
    except Exception:
        log.exception("Збій звіту про вагу")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file")
    p.add_argument("--weight", action="store_true", help="звіт про позиції без середньої ваги")
    args = p.parse_args()

    if args.weight:
        weight_report.run_weight_report(args.file, dry_run=args.dry_run)
        return
    if args.once or args.dry_run or args.file:
        run_check(args.file, dry_run=args.dry_run)
        return

    hours = os.getenv("SCHEDULE_HOURS", "8")
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    sched = BlockingScheduler(timezone="Europe/Kyiv")
    sched.add_job(scheduled_check, "cron", hour=hours, minute=minute, id="check",
                  misfire_grace_time=3600, coalesce=True)
    # звіт про вагу — через 15 хв після перевірки помилок, щоб повідомлення не зливались
    sched.add_job(scheduled_weight_report, "cron", hour=hours, minute=(minute + 15) % 60,
                  id="weight", misfire_grace_time=3600, coalesce=True)
    log.info("Планувальник запущено: о %s:%02d (Europe/Kyiv), перевірка раз на %s дн.", hours, minute,
             os.getenv("CHECK_EVERY_DAYS", "2"))
    if os.getenv("RUN_ON_START", "true").lower() == "true":
        scheduled_check()
        scheduled_weight_report()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Зупинено.")


if __name__ == "__main__":
    main()
