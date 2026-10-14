"""Перевірка назв за допомогою Claude: орфографія, 'вік замість бренду', безглуздя.
Викликається лише для нових або змінених назв (результат кешується в стані)."""
import json
import logging
import os
import re
from pathlib import Path

from rules import Issue, WARNING, ERROR

log = logging.getLogger(__name__)

BATCH_SIZE = 80
MAX_OUTPUT_TOKENS = 2000
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


class Budget:
    """Жорсткий ліміт витрат на Claude (USD). Ціни навмисно завищені (за замовчуванням
    як для Sonnet), щоб ліміт спрацьовував із запасом; змінюються через
    AI_PRICE_IN_PER_MTOK / AI_PRICE_OUT_PER_MTOK."""

    def __init__(self, limit_usd: float, spent_usd: float = 0.0):
        self.limit = limit_usd
        self.start = spent_usd
        self.spent = spent_usd
        self.price_in = float(os.getenv("AI_PRICE_IN_PER_MTOK", "3.0"))
        self.price_out = float(os.getenv("AI_PRICE_OUT_PER_MTOK", "15.0"))

    @property
    def used_now(self) -> float:
        return self.spent - self.start

    def cost(self, in_tok: int, out_tok: int) -> float:
        return (in_tok * self.price_in + out_tok * self.price_out) / 1e6

    def worst_case(self, prompt_chars: int) -> float:
        # кирилиця токенізується погано: рахуємо 1 токен на 2 символи, вихід — по максимуму
        return self.cost(prompt_chars // 2 + 100, MAX_OUTPUT_TOKENS)

    def can_afford(self, prompt_chars: int) -> bool:
        return self.spent + self.worst_case(prompt_chars) <= self.limit

    def add(self, in_tok: int, out_tok: int) -> None:
        self.spent += self.cost(in_tok, out_tok)


def check_batch(client, model: str, items: list[tuple[int, str, str]]):
    """items: (code, name, group). Повертає (проблеми, вхідні токени, вихідні токени)."""
    listing = "\n".join(f"{code} | {name.strip()} | група: {group.strip()}"
                        for code, name, group in items)
    resp = client.messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM,
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
    usage = getattr(resp, "usage", None)
    return (out, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)


def check_names(items: list[tuple[int, str, str]], budget: Budget):
    """Повертає (проблеми, коди успішно перевірених, чи зупинились через бюджет).
    Помилка API на одній порції не зупиняє решту; недоперевірені коди підуть наступного разу."""
    if not items:
        return [], set(), False
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY не задано — AI-перевірку пропущено")
        return [], set(), False
    model = os.getenv("AI_MODEL", "claude-haiku-5-5")
    client = _client()
    issues: list[Issue] = []
    done: set[int] = set()
    out_of_budget = False
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        chars = len(SYSTEM) + sum(len(n) + len(g) + 12 for _, n, g in batch)
        if not budget.can_afford(chars):
            out_of_budget = True
            log.warning("AI: ліміт $%.2f майже вичерпано (витрачено $%.4f) — зупиняюсь, "
                        "лишилось %d назв", budget.limit, budget.spent, len(items) - i)
            break
        try:
            found, in_tok, out_tok = check_batch(client, model, batch)
            budget.add(in_tok, out_tok)
            issues.extend(found)
            done.update(code for code, _, _ in batch)
        except Exception as e:  # мережа, ліміти, модель недоступна
            log.error("AI-перевірка порції %d–%d не вдалась: %s", i, i + len(batch), e)
    log.info("AI перевірив %d назв, знайшов %d проблем, витрачено ≈$%.4f цього запуску",
             len(done), len(issues), budget.used_now)
    return issues, done, out_of_budget
