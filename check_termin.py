"""
Следит за страницей выбора места (Fahrerlaubnisbehörde Düsseldorf).
Если на странице хоть что-то изменилось по сравнению с прошлой проверкой —
шлёт push через ntfy. Прошлое состояние хранится в файле baseline.txt.
"""
import difflib
import html as htmllib
import os
import re
import sys

import requests

START_URL = "https://termine.duesseldorf.de/select2?md=3"
URL = (
    "https://termine.duesseldorf.de/location?mdt=402&select_cnc=1"
    "&cnc-5885=0&cnc-5886=0&cnc-5888=0&cnc-5860=0&cnc-5874=0&cnc-5872=0"
    "&cnc-5870=1&cnc-5871=0&cnc-5873=0&cnc-5859=0&cnc-5884=0&cnc-5879=0"
    "&cnc-5882=0&cnc-5861=0&cnc-5864=0&cnc-5866=0&cnc-5868=0&cnc-5876=0"
    "&cnc-5883=0&cnc-5869=0&cnc-5863=0&cnc-5862=0&cnc-5865=0&cnc-5875=0"
    "&cnc-5878=0&cnc-5867=0&cnc-5889=0&cnc-5858=0"
)

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
BASELINE = "baseline.txt"

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def page_text() -> str:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "de-DE,de;q=0.9",
        }
    )
    s.get(START_URL, timeout=30).raise_for_status()
    r = s.get(URL, timeout=30, headers={"Referer": START_URL})
    r.raise_for_status()
    t = re.sub(r"<(script|style).*?</\1>", " ", r.text, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def notify(title: str, msg: str):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "rotating_light",
            "Click": URL,
        },
        timeout=30,
    )


def main():
    text = page_text()

    if "Fahrerlaubnisbehörde" not in text:
        print("Неожиданная страница:", text[:500])
        sys.exit(1)

    # Отбрасываем всё до заголовка шага (баннер cookies и т.п.)
    start = text.find("Schritt")
    if start >= 0:
        text = text[start:]

    print("Текст страницы:", text[:800])

    if not os.path.exists(BASELINE):
        with open(BASELINE, "w", encoding="utf-8") as f:
            f.write(text)
        print("Первый запуск: состояние сохранено, уведомления нет.")
        return

    with open(BASELINE, encoding="utf-8") as f:
        old = f.read()

    if old == text:
        print("Без изменений.")
        return

    diff = list(difflib.ndiff(old.split(), text.split()))
    added = " ".join(d[2:] for d in diff if d.startswith("+ "))
    removed = " ".join(d[2:] for d in diff if d.startswith("- "))
    print("Добавлено:", added)
    print("Удалено:", removed)

    title = "Termin: Seite geaendert"
    if TIME_RE.search(added):
        title = "Termin: SLOTS frei?"
    msg = f"Страница изменилась!\n\nНовое: {added[:250] or '-'}\nУбрано: {removed[:150] or '-'}"
    notify(title, msg)

    with open(BASELINE, "w", encoding="utf-8") as f:
        f.write(text)
    print("Уведомление отправлено, состояние обновлено.")


if __name__ == "__main__":
    main()
