"""Бот перевірки товарів «Ділова Ковбаса».

  python main.py                 — запуск за розкладом (Railway)
  python main.py --once          — одна перевірка зараз
  python main.py --dry-run       — перевірка без Telegram і без збереження стану
  python main.py --dry-run --file prom_export.xlsx   — з локального файлу
  python main.py --descriptions  — згенерувати чернетки описів і надіслати xlsx (--dry-run: лише показати)
  python main.py --descriptions-prepare out.txt   — записати запит для сесії Claude (режим без API-ключа)
  python main.py --descriptions-ingest reply.txt  — прийняти відповідь сесії, перевірити, зберегти, зібрати xlsx
"""
import argparse
import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

from checker import due, run_check  # noqa: E402  (після load_dotenv)
import descriptions  # noqa: E402
from descriptions import run_descriptions  # noqa: E402
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


def scheduled_descriptions():
    """Тригер спрацьовує щодня, але генерація йде раз на DESC_EVERY_DAYS днів (дата — у descriptions.json)."""
    try:
        if not descriptions.due(descriptions.DescStore(descriptions.store_path())):
            log.info("Описи: рано — з останньої генерації минуло менше %s дн.", os.getenv("DESC_EVERY_DAYS", "3"))
            return
        run_descriptions()
    except Exception:
        log.exception("Збій генерації описів")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file")
    p.add_argument("--descriptions", action="store_true")
    p.add_argument("--descriptions-prepare", metavar="OUT")
    p.add_argument("--descriptions-ingest", metavar="REPLY")
    p.add_argument("--codes", help="для --descriptions-prepare: коди товарів через кому")
    p.add_argument("--limit", type=int, help="скільки описів згенерувати (для --descriptions)")
    args = p.parse_args()

    if args.descriptions_prepare:
        descriptions.prepare(args.file, args.descriptions_prepare, args.limit,
                             [int(c) for c in args.codes.split(",")] if args.codes else None)
        return
    if args.descriptions_ingest:
        descriptions.ingest(args.descriptions_ingest, args.file, dry_run=args.dry_run)
        return
    if args.descriptions:
        run_descriptions(args.file, dry_run=args.dry_run, limit=args.limit)
        return
    if args.once or args.dry_run or args.file:
        run_check(args.file, dry_run=args.dry_run)
        return

    hours = os.getenv("SCHEDULE_HOURS", "8")
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    sched = BlockingScheduler(timezone="Europe/Kyiv")
    sched.add_job(scheduled_check, "cron", hour=hours, minute=minute, id="check",
                  misfire_grace_time=3600, coalesce=True)
    if os.getenv("DESC_ENABLED", "false").lower() == "true":
        sched.add_job(scheduled_descriptions, "cron", hour=os.getenv("DESC_SCHEDULE_HOURS", "9"), minute=minute,
                      id="descriptions", misfire_grace_time=3600, coalesce=True)
        log.info("Чернетки описів: о %s:%02d раз на %s дн., по %s шт.", os.getenv("DESC_SCHEDULE_HOURS", "9"), minute,
                 os.getenv("DESC_EVERY_DAYS", "3"), os.getenv("DESC_PER_RUN", "50"))
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
