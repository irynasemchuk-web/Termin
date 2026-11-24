"""
Проверяет страницу выбора места (Fahrerlaubnisbehörde Düsseldorf).
Если после слов "2. Etage" появляется дата/время — шлёт push через ntfy.
"""
import html as htmllib
import os
import re
import sys

import requests

URL = (
    "https://termine.duesseldorf.de/location?mdt=402&select_cnc=1"
    "&cnc-5885=0&cnc-5886=0&cnc-5888=0&cnc-5860=0&cnc-5874=0&cnc-5872=0"
    "&cnc-5870=1&cnc-5871=0&cnc-5873=0&cnc-5859=0&cnc-5884=0&cnc-5879=0"
    "&cnc-5882=0&cnc-5861=0&cnc-5864=0&cnc-5866=0&cnc-5868=0&cnc-5876=0"
    "&cnc-5883=0&cnc-5869=0&cnc-5863=0&cnc-5862=0&cnc-5865=0&cnc-5875=0"
    "&cnc-5878=0&cnc-5867=0&cnc-5889=0&cnc-5858=0"
)

NTFY_TOPIC = os.environ["NTFY_TOPIC"]

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
DATE_RE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(\d{2,4})?")
MONTH_RE = re.compile(
    r"\b(Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember)\b",
    re.I,
)


START_URL = "https://termine.duesseldorf.de/select2?md=3"


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
    # 1) Стартовая страница создаёт сессию (cookies)
    s.get(START_URL, timeout=30).raise_for_status()
    # 2) Теперь открываем страницу выбора места уже с этой сессией
    r = s.get(URL, timeout=30, headers={"Referer": START_URL})
    r.raise_for_status()
    t = re.sub(r"<(script|style).*?</\1>", " ", r.text, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def notify(msg: str):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        headers={
            "Title": "Termin frei!",
            "Priority": "urgent",
            "Tags": "rotating_light",
            "Click": URL,
        },
        timeout=30,
    )


def main():
    text = page_text()

    # Страница не та (сессия, ошибка, блокировка) -> job упадёт, GitHub пришлёт письмо
    if "Fahrerlaubnisbehörde" not in text:
        print("Неожиданная страница:", text[:500])
        sys.exit(1)

    idx = text.find("Etage")
    after = text[idx + len("Etage"):][:2000] if idx >= 0 else ""
    print("Текст после 'Etage':", after[:400])

    if TIME_RE.search(after) or DATE_RE.search(after) or MONTH_RE.search(after):
        notify("Появились свободные слоты! Открой ссылку и выбери время.\n\n" + after[:200])
        print("Уведомление отправлено")
    else:
        print("Мест нет")


if __name__ == "__main__":
    main()
