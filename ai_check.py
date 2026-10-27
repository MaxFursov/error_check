"""Перевірка назв за допомогою Claude: орфографія, 'вік замість бренду', безглуздя.
Викликається лише для нових або змінених назв (результат кешується в стані)."""
import logging
from dataclasses import dataclass
import os
import re
from pathlib import Path

from rules import Issue, ERROR

log = logging.getLogger(__name__)

BATCH_SIZE = 40
MAX_OUTPUT_TOKENS = 1200
CONTEXT = (Path(__file__).parent / "company_context.txt").read_text(encoding="utf-8")

SYSTEM = f"""Ти — коректор каталогу товарів оптового постачальника харчових продуктів.
Тобі дають нумерований список назв товарів. Знайди ТІЛЬКИ реальні помилки в назвах.

{CONTEXT}

ФОРМАТ ВІДПОВІДІ — суворо один рядок на помилку, без JSON, без markdown, без вступів:
код | слово_з_помилкою → правильне_слово | чому (2–6 слів)
Приклади:
2680 | Camambert → Camembert | друкарська помилка
1910 | Пармеза → Пармезан | друкарська помилка
2323 | YOUNG → вік сиру, не ТМ | вік замість виробника
Якщо помилок у порції немає — відповідай одним словом: NONE

ПРАВИЛА:
- ПОВІДОМЛЯЙ ЛИШЕ коли впевнений щонайменше на 90% і можеш дати КОНКРЕТНЕ виправлення після «→».
- НЕ повідомляй про: відсутність ТМ/виробника, фасування чи жирності; регістр літер; різні варіанти
  транслітерації (Прошуто/Прошутто, Чорізо); сумніви щодо відповідності групі чи типу продукту;
  бренди, яких ти просто не знаєш; "потребує перевірки". Це не помилки.
- Краще пропустити сумнівний випадок, ніж видати хибну тривогу. Не вигадуй проблем.
- Якщо не впевнений або виправлення немає — НЕ пиши рядок взагалі. Ніколи не пиши в рядках "NONE",
  "без змін", "пропускаю", "не впевнений", "ок" — такі рядки заборонені.
- Не більше 15 рядків на порцію: обирай лише найвпевненіші випадки.
- "слово_з_помилкою" — лише одне слово або коротка фраза (до 4 слів), ніколи не вся назва."""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.+?)\s*(?:→|->)\s*(.+?)\s*(?:\|\s*(.*?))?\s*$")


# Маркери "бурмотіння" моделі: так вона пише, коли насправді виправлення немає
JUNK_MARKERS = ("none", "без змін", "пропуск", "не впевнен", "немає", "(ок", " ок)", "ок)",
                "не коригув", "незначн", "сумнів", "можлива описка", "перевірте", "ймовірна помилка", "не в назві", "невідом",
                "коректн", "назва товару", "?")


def is_plausible(name: str, wrong: str, right: str, why: str = "") -> bool:
    """Відсікає рядки без реального виправлення: «→ NONE», «без змін», виправлення,
    що лише міняє регістр, або «слово», що охоплює майже всю назву."""
    w, r = wrong.casefold().strip(), right.casefold().strip()
    if not w or not r or w == r:
        return False
    if any(m in r for m in JUNK_MARKERS) or any(m in why.casefold() for m in ("пропуск", "не впевнен", "не коригув")):
        return False
    if len(wrong) > 0.7 * len(name.strip()) and len(name.split()) > 2:
        return False                      # "помилка" — уся назва
    if len(wrong.split()) > 5:
        return False
    if len(right) > 2 * len(wrong) + 15 and "вік" not in why.casefold():
        return False                      # розлогий текст замість виправлення
    return True


def _parse(text: str) -> list[tuple[int, str, str, str]]:
    """Рядки `код | було → має бути | чому` -> [(код, було, має_бути, чому)]."""
    out = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3), m.group(4) or ""))
    return out


class Budget:
    """Жорсткий ліміт витрат на Claude (USD). Ціни навмисно завищені (за замовчуванням
    як для Sonnet), щоб ліміт спрацьовував із запасом; змінюються через
    AI_PRICE_IN_PER_MTOK / AI_PRICE_OUT_PER_MTOK."""

    def __init__(self, limit_usd: float, spent_usd: float = 0.0, max_out: int | None = None):
        self.limit = limit_usd
        self.max_out = max_out or MAX_OUTPUT_TOKENS
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
        return self.cost(prompt_chars // 2 + 100, self.max_out)

    def can_afford(self, prompt_chars: int) -> bool:
        return self.spent + self.worst_case(prompt_chars) <= self.limit

    def add(self, in_tok: int, out_tok: int) -> None:
        self.spent += self.cost(in_tok, out_tok)


@dataclass
class BatchResult:
    issues: list
    in_tok: int
    out_tok: int
    truncated: bool
    covered: set  # коди, які модель точно встигла переглянути


def check_batch(client, model: str, items: list[tuple[int, str, str]]) -> BatchResult:
    """items: (code, name, group). Якщо відповідь обрізана (max_tokens), порція зарахована лише
    частково: модель відповідає в порядку вхідного списку, тож усе до останнього згаданого коду
    (не включно — рядок міг обірватись) вважається переглянутим, решта піде наступного разу."""
    listing = "\n".join(f"{code} | {name.strip()} | група: {group.strip()}"
                        for code, name, group in items)
    resp = client.messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM,
        # без "міркування": воно з'їдає вихідні токени (гроші) і обрізає відповідь
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": listing}])
    names = {code: name for code, name, _ in items}
    order = [code for code, _, _ in items]
    text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = _parse(text)
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"
    if truncated:
        mentioned = [order.index(c) for c, *_ in parsed if c in names]
        covered = set(order[:max(mentioned)]) if mentioned else set()
        parsed = [p for p in parsed if p[0] in covered]
    else:
        covered = set(order)
    out = []
    for code, wrong, right, why in parsed:
        if code not in names or not is_plausible(names[code], wrong, right, why):
            continue  # модель вигадала код або видала рядок без реального виправлення
        out.append(Issue(code, names[code], "ai_review", ERROR,
                         f"«{wrong}»: {why or 'ймовірна помилка'}", f"{wrong} → {right}"))
    usage = getattr(resp, "usage", None)
    return BatchResult(out, getattr(usage, "input_tokens", 0) or 0,
                       getattr(usage, "output_tokens", 0) or 0, truncated, covered)


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
    truncated_in_row = 0
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        chars = len(SYSTEM) + sum(len(n) + len(g) + 12 for _, n, g in batch)
        if not budget.can_afford(chars):
            out_of_budget = True
            log.warning("AI: ліміт $%.2f майже вичерпано (витрачено $%.4f) — зупиняюсь, "
                        "лишилось %d назв", budget.limit, budget.spent, len(items) - i)
            break
        try:
            res = check_batch(client, model, batch)
            budget.add(res.in_tok, res.out_tok)  # гроші витрачено в будь-якому разі
            issues.extend(res.issues)
            done.update(res.covered)
            if res.truncated:
                log.warning("AI: відповідь на порцію %d–%d обрізана — зараховано %d з %d назв",
                            i, i + len(batch), len(res.covered), len(batch))
                truncated_in_row = truncated_in_row + 1 if not res.covered else 0
                if truncated_in_row >= 2:
                    log.error("AI: дві порції поспіль без жодного результату — зупиняюсь, "
                              "щоб не палити токени")
                    break
            else:
                truncated_in_row = 0
        except Exception as e:  # мережа, ліміти, модель недоступна
            log.error("AI-перевірка порції %d–%d не вдалась: %s", i, i + len(batch), e)
    log.info("AI перевірив %d назв, знайшов %d проблем, витрачено ≈$%.4f цього запуску",
             len(done), len(issues), budget.used_now)
    return issues, done, out_of_budget
