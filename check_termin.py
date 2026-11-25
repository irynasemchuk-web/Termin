"""
Проверяет, есть ли свободные слоты на termine.duesseldorf.de/suggest
(Umschreibung ausländischer Führerschein, Fahrerlaubnisbehörde Düsseldorf).

Один запуск работает ~4,5 минуты и проверяет страницу каждые ~20 секунд.
Если слоты найдены — шлёт push через ntfy и завершается.
"""
import html as htmllib
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

BASE = "https://termine.duesseldorf.de"
START_URL = f"{BASE}/select2?md=3"
SUGGEST_URL = f"{BASE}/suggest"
LOCATION_URL = (
    f"{BASE}/location?mdt=402&select_cnc=1"
    "&cnc-5885=0&cnc-5886=0&cnc-5888=0&cnc-5860=0&cnc-5874=0&cnc-5872=0"
    "&cnc-5870=1&cnc-5871=0&cnc-5873=0&cnc-5859=0&cnc-5884=0&cnc-5879=0"
    "&cnc-5882=0&cnc-5861=0&cnc-5864=0&cnc-5866=0&cnc-5868=0&cnc-5876=0"
    "&cnc-5883=0&cnc-5869=0&cnc-5863=0&cnc-5862=0&cnc-5865=0&cnc-5875=0"
    "&cnc-5878=0&cnc-5867=0&cnc-5889=0&cnc-5858=0"
)

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "270"))
INTERVAL = int(os.environ.get("INTERVAL", "20"))

TIME_RE = re.compile(r"\b([01]\d|2[0-3]):[0-5]\d\b")
DATE_RE = re.compile(
    r"(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag),\s*\d{2}\.\d{2}\.\d{4}"
)
NO_SLOTS = ("keine zeiten verfügbar", "keine freien termine")


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.links = [], []
        self.cur, self._a = None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "get").lower(),
                "inputs": [],
                "text": "",
            }
            self.forms.append(self.cur)
        elif tag in ("input", "button") and self.cur is not None:
            self.cur["inputs"].append(a)
        elif tag == "a":
            self._a = {"href": a.get("href", ""), "text": ""}
            self.links.append(self._a)

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None
        elif tag == "a":
            self._a = None

    def handle_data(self, data):
        if self.cur is not None:
            self.cur["text"] += data
        if self._a is not None:
            self._a["text"] += data


def to_text(raw: str) -> str:
    t = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def submit_weiter(s: requests.Session, r: requests.Response) -> requests.Response:
    """Нажимаем 'Weiter' на странице выбора места."""
    p = FormParser()
    p.feed(r.text)

    for f in p.forms:
        has_weiter = "weiter" in f["text"].lower() or any(
            "weiter" in (i.get("value") or "").lower() for i in f["inputs"]
        )
        if not has_weiter:
            continue
        data, radios = [], {}
        for i in f["inputs"]:
            name, typ = i.get("name"), (i.get("type") or "text").lower()
            if not name:
                continue
            val = i.get("value", "")
            if typ in ("submit", "button", "image"):
                if "weiter" in val.lower():
                    data.append((name, val))
            elif typ == "radio":
                radios.setdefault(name, []).append(i)
            elif typ == "checkbox":
                if "checked" in i:
                    data.append((name, val or "on"))
            else:
                data.append((name, val))
        for name, items in radios.items():
            pick = next((x for x in items if "checked" in x), items[0])
            data.append((name, pick.get("value", "")))
        url = urljoin(r.url, f["action"] or r.url)
        print(f"  Weiter-форма: {f['method'].upper()} {url} поля={[d[0] for d in data]}")
        if f["method"] == "post":
            return s.post(url, data=data, headers={"Referer": r.url}, timeout=30)
        return s.get(url, params=data, headers={"Referer": r.url}, timeout=30)

    for a in p.links:
        if "weiter" in a["text"].lower() and a["href"]:
            url = urljoin(r.url, a["href"])
            print(f"  Weiter-ссылка: {url}")
            return s.get(url, headers={"Referer": r.url}, timeout=30)

    print("  Кнопка Weiter не найдена")
    return r


def check_once():
    """Возвращает ('slots', инфо) | ('none', '') | ('unknown', текст)."""
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
    r = s.get(LOCATION_URL, headers={"Referer": START_URL}, timeout=30)
    r.raise_for_status()

    r = submit_weiter(s, r)
    if "/suggest" not in r.url:
        r = s.get(SUGGEST_URL, headers={"Referer": r.url}, timeout=30)
    r.raise_for_status()

    text = to_text(r.text)
    print(f"  Итоговая страница: {r.url}\n  Текст: {text[:300]}")
    low = text.lower()

    if any(h in low for h in NO_SLOTS):
        return "none", ""

    if TIME_RE.search(text) and ("uhrzeit" in low or "auswahl der zeit" in low):
        dates = list(dict.fromkeys(m.group(0) for m in DATE_RE.finditer(text)))
        times = list(dict.fromkeys(m.group(0) for m in TIME_RE.finditer(text)))
        info = "Даты: " + ("; ".join(dates[:5]) or "?") + "\nВремя: " + ", ".join(times[:8])
        return "slots", info

    return "unknown", text[:500]


def notify(msg: str):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        headers={
            "Title": "Termin FREI!",
            "Priority": "urgent",
            "Tags": "rotating_light",
            "Click": START_URL,
        },
        timeout=30,
    )


def main():
    end = time.time() + LOOP_SECONDS
    ok_checks = 0
    n = 0
    while True:
        n += 1
        print(f"[{time.strftime('%H:%M:%S')}] Проверка №{n}")
        try:
            status, info = check_once()
            if status == "slots":
                print("СЛОТЫ НАЙДЕНЫ!", info)
                notify("Появились свободные слоты! Открой сайт и выбери время.\n\n" + info)
                return
            if status == "none":
                ok_checks += 1
                print("  мест нет")
            else:
                print("  страница непонятная:", info)
        except Exception as e:
            print("  ошибка:", e)
        if time.time() + INTERVAL > end:
            break
        time.sleep(INTERVAL)

    if ok_checks == 0:
        print("Ни одна проверка не дала понятный результат.")
        sys.exit(1)


if __name__ == "__main__":
    main()
