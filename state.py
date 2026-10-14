"""Стан між запусками: що вже перевірено AI, про що вже повідомляли, минулі ціни."""
import hashlib
import json
import os
from datetime import date
from pathlib import Path


def name_hash(name: str) -> str:
    return hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:12]


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = {"ai_checked": {}, "reported": [], "prices": {}, "ai_issues": {}, "last_run": None}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass  # пошкоджений стан = перший запуск

    @property
    def first_run(self) -> bool:
        return not self.path.exists()

    def days_since_last_run(self, today: date) -> int | None:
        last = self.data.get("last_run")
        return (today - date.fromisoformat(last)).days if last else None

    @property
    def prices(self) -> dict[int, float]:
        return {int(k): v for k, v in self.data["prices"].items()}

    def names_to_check(self, rows: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
        """Тільки нові або змінені назви."""
        checked = self.data["ai_checked"]
        return [r for r in rows if checked.get(str(r[0])) != name_hash(r[1])]

    def mark_checked(self, rows: list[tuple[int, str, str]], codes: set[int]) -> None:
        for code, name, _ in rows:
            if code in codes:
                self.data["ai_checked"][str(code)] = name_hash(name)
                self.data["ai_issues"].pop(str(code), None)

    def remember_ai_issues(self, issues: list[dict]) -> None:
        for d in issues:
            self.data["ai_issues"].setdefault(str(d["code"]), []).append(d)

    def cached_ai_issues(self, current: dict[int, str]) -> list[dict]:
        """AI-проблеми для назв, що не змінились (щоб звіт був повним без повторних викликів)."""
        out = []
        for code, items in self.data["ai_issues"].items():
            code = int(code)
            if code in current and self.data["ai_checked"].get(str(code)) == name_hash(current[code]):
                out.extend(items)
        return out

    def new_issues(self, keys: list[str]) -> set[str]:
        return set(keys) - set(self.data["reported"])

    def save(self, open_issue_keys: list[str], prices: dict[int, float],
             today: date | None = None) -> None:
        # Зберігаємо лише актуальні ключі: якщо помилку виправили, а потім
        # вона повернулась — про неї повідомимо знову.
        self.data["reported"] = sorted(set(open_issue_keys))
        self.data["prices"] = {str(k): v for k, v in prices.items()}
        self.data["last_run"] = (today or date.today()).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
