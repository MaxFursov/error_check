"""Перевірка назв за допомогою Claude: орфографія, 'вік замість бренду', безглуздя.
Викликається лише для нових або змінених назв (результат кешується в стані)."""
import json
import logging
import os
import re
from pathlib import Path

from rules import Issue, WARNING, ERROR

log = logging.getLogger(__name__)

BATCH_SIZE = 60
CONTEXT = (Path(__file__).parent / "company_context.txt").read_text(encoding="utf-8")

SYSTEM = f"""Ти — коректор каталогу товарів оптового постачальника харчових продуктів.
Тобі дають нумерований список назв товарів. Знайди ТІЛЬКИ реальні помилки в назвах.

{CONTEXT}

ПРАВИЛА ВІДПОВІДІ:
- Відповідай ЛИШЕ валідним JSON-масивом, без пояснень і markdown.
- Для кожної назви з помилкою: {{"code": <код>, "problem": "<що не так, 1 речення українською>",
  "suggestion": "<як виправити, або порожній рядок>", "severity": "error" | "warning"}}
- severity "error" — безсумнівна помилка (друкарська помилка, безглузде слово); "warning" — підозріло, потрібна перевірка людиною.
- Якщо помилок немає — поверни [].
- Краще пропустити сумнівний випадок, ніж видати хибну тривогу. Не вигадуй проблем."""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _parse(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and "code" in d and "problem" in d]


def check_batch(client, model: str, items: list[tuple[int, str, str]]) -> list[Issue]:
    """items: (code, name, group). Повертає знайдені проблеми."""
    listing = "\n".join(f"{code} | {name.strip()} | група: {group.strip()}"
                        for code, name, group in items)
    resp = client.messages.create(
        model=model, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": listing}])
    names = {code: name for code, name, _ in items}
    out = []
    for d in _parse("".join(b.text for b in resp.content if b.type == "text")):
        try:
            code = int(d["code"])
        except (TypeError, ValueError):
            continue
        if code not in names:
            continue  # модель вигадала код
        sev = ERROR if d.get("severity") == "error" else WARNING
        out.append(Issue(code, names[code], "ai_review", sev,
                         str(d["problem"]).strip(), str(d.get("suggestion", "")).strip()))
    return out


def check_names(items: list[tuple[int, str, str]]) -> tuple[list[Issue], set[int]]:
    """Повертає (проблеми, коди, які були успішно перевірені).
    Помилка API на одній порції не зупиняє решту; такі коди перевіряться наступного разу."""
    if not items:
        return [], set()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY не задано — AI-перевірку пропущено")
        return [], set()
    model = os.getenv("AI_MODEL", "claude-haiku-5-5")
    client = _client()
    issues: list[Issue] = []
    done: set[int] = set()
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        try:
            issues.extend(check_batch(client, model, batch))
            done.update(code for code, _, _ in batch)
        except Exception as e:  # мережа, ліміти, модель недоступна
            log.error("AI-перевірка порції %d–%d не вдалась: %s", i, i + len(batch), e)
    log.info("AI перевірив %d назв, знайшов %d проблем", len(done), len(issues))
    return issues, done
