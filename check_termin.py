"""
Следит за страницей /suggest (Fahrerlaubnisbehörde Düsseldorf).
Если появились свободные слоты:
  1) сразу шлёт push через ntfy;
  2) (если заданы BOOKING_* секреты) пытается САМ записаться на самый ранний
     доступный слот: выбирает первую доступную дату/время, заполняет личные данные,
     подтверждает запись.

Один запуск работает ~2,5 минуты и проверяет страницу каждые ~20 секунд.
Личные данные берутся только из секретов GitHub, в коде их нет.
"""
import html as htmllib
import os
import re
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

BASE = os.environ.get("TERMIN_BASE", "https://termine.duesseldorf.de")
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
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "150"))
INTERVAL = int(os.environ.get("INTERVAL", "20"))

# Личные данные (секреты GitHub)
NAME = os.environ.get("BOOKING_NAME", "").strip()          # Irina Ostromuhoff
BDATE = os.environ.get("BOOKING_BIRTHDATE", "").strip()    # 30.03.1988
EMAIL = os.environ.get("BOOKING_EMAIL", "").strip()
PHONE = os.environ.get("BOOKING_PHONE", "").strip()        # необязательно
AUTO_BOOK = bool(NAME and BDATE and EMAIL)

_parts = NAME.split()
FIRST = _parts[0] if _parts else ""
LAST = " ".join(_parts[1:]) if len(_parts) > 1 else ""

_d = re.findall(r"\d+", BDATE)
if len(_d) == 3:
    BDATE_DE = f"{int(_d[0]):02d}.{int(_d[1]):02d}.{_d[2]}"
    BDATE_ISO = f"{_d[2]}-{int(_d[1]):02d}-{int(_d[0]):02d}"
else:
    BDATE_DE = BDATE_ISO = BDATE

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
TIME_ONLY = re.compile(r"^\s*([01]?\d|2[0-3]):[0-5]\d(\s*Uhr)?\s*$")
DATE_RE = re.compile(
    r"(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag),\s*\d{2}\.\d{2}\.\d{4}"
)
NO_SLOTS = ("keine zeiten verfügbar", "keine freien termine")
DISABLED_WORDS = ("disabled", "inactive", "unavailable", "booked", "occupied",
                  "blocked", "nofree", "not-available", "belegt", "ausgebucht")
CONFIRM_RE = re.compile(r"weiter|buchen|bestätig|reservier|absenden|abschließ|verbindlich|termin", re.I)
BACK_RE = re.compile(r"zurück|abbrech|back|cancel|löschen", re.I)
CONSENT_WORDS = ("datenschutz", "privacy", "zustimm", "einverstanden", "agb",
                 "bedingung", "akzept", "consent", "einwillig")
SUCCESS_WORDS = ("erfolgreich", "wurde gebucht", "wurde reserviert", "terminbestätigung",
                 "vorgangsnummer", "buchungsnummer", "terminnummer", "termin ist gebucht")


def redact(t: str) -> str:
    for secret in {NAME, EMAIL, BDATE, BDATE_DE, PHONE, FIRST, LAST}:
        if secret and len(secret) > 2:
            t = re.sub(re.escape(secret), "***", t, flags=re.I)
    return t


def to_text(raw: str) -> str:
    t = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


# ----------------------------------------------------------------------------
# Разбор HTML
# ----------------------------------------------------------------------------
class Page(HTMLParser):
    """Собирает формы, поля, ссылки и кнопки со временем."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.cands, self.links = [], [], []
        self.labels = {}
        self.cur_form = None
        self.stack = []

    def _new(self, tag, a):
        return {"tag": tag, "attrs": a, "text": "", "form": self.cur_form,
                "opts": [], "children": [], "label": ""}

    def _in_label(self, el):
        for lab in self.stack:
            if lab["tag"] == "label":
                lab["children"].append(el)

    def _register(self, el):
        tag, a = el["tag"], el["attrs"]
        typ = (a.get("type") or "").lower()
        if tag in ("input", "button", "select", "textarea") and self.cur_form is not None:
            self.cur_form["fields"].append(el)
        if tag == "a":
            self.links.append(el)
        is_click = tag in ("a", "button") or (tag == "input" and typ in ("submit", "button"))
        if is_click and TIME_ONLY.match(el["text"] or ""):
            self.cands.append(el)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self.cur_form = {"action": a.get("action", ""),
                             "method": (a.get("method") or "get").lower(),
                             "fields": [], "text": ""}
            self.forms.append(self.cur_form)
        elif tag == "input":
            el = self._new("input", a)
            el["text"] = a.get("value", "")
            self._in_label(el)
            self._register(el)
        elif tag in ("select", "textarea", "button", "a", "label"):
            el = self._new(tag, a)
            if tag in ("select", "textarea"):
                self._in_label(el)
            self.stack.append(el)
        elif tag == "option":
            for el in reversed(self.stack):
                if el["tag"] == "select":
                    el["opts"].append({"value": a.get("value", ""), "selected": "selected" in a})
                    break

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur_form = None
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                el = self.stack.pop(i)
                el["text"] = re.sub(r"\s+", " ", el["text"]).strip()
                if tag == "label":
                    if el["attrs"].get("for"):
                        self.labels[el["attrs"]["for"]] = el["text"]
                    for ch in el["children"]:
                        ch["label"] = el["text"]
                else:
                    self._register(el)
                break

    def handle_data(self, data):
        for el in self.stack:
            el["text"] += data
        if self.cur_form is not None:
            self.cur_form["text"] += data


def parse(html_text: str) -> Page:
    p = Page()
    p.feed(html_text)
    p.close()
    return p


# ----------------------------------------------------------------------------
# Работа с формами
# ----------------------------------------------------------------------------
def ftype(f):
    a = f["attrs"]
    if f["tag"] == "input":
        return (a.get("type") or "text").lower()
    if f["tag"] == "button":
        return (a.get("type") or "submit").lower()
    return f["tag"]


def btn_label(f):
    return ((f["attrs"].get("value") or "") + " " + (f["text"] or "")).strip()


def guess_value(f, page):
    """Что вписать в текстовое поле. None = оставить как есть."""
    a = f["attrs"]
    typ = ftype(f)
    key = " ".join([a.get("name", ""), a.get("id", ""), a.get("placeholder", ""),
                    a.get("aria-label", ""), f.get("label", ""),
                    page.labels.get(a.get("id", ""), "")]).lower()
    if "mail" in key or typ == "email":
        return EMAIL
    if any(w in key for w in ("vorname", "firstname", "first_name", "first-name")):
        return FIRST
    if any(w in key for w in ("nachname", "lastname", "last_name", "familienname", "surname")):
        return LAST
    if any(w in key for w in ("geburt", "birth", "gebdat", "dob")):
        return BDATE_ISO if typ == "date" else BDATE_DE
    if any(w in key for w in ("telefon", "phone", "mobil", "handy", "tel")):
        return PHONE
    if "name" in key:
        return NAME
    return None


def build_data(form, page, press=None):
    data, radios = [], {}
    for f in form["fields"]:
        a = f["attrs"]
        name = a.get("name")
        typ = ftype(f)
        if not name:
            continue
        if typ in ("submit", "button", "image", "reset"):
            if f is press:
                data.append((name, a.get("value", f["text"])))
        elif typ == "radio":
            radios.setdefault(name, []).append(f)
        elif typ == "checkbox":
            key = (name + " " + a.get("id", "") + " " + f.get("label", "") + " "
                   + page.labels.get(a.get("id", ""), "")).lower()
            if "checked" in a or any(w in key for w in CONSENT_WORDS):
                data.append((name, a.get("value") or "on"))
        elif typ == "select":
            opts = [o for o in f["opts"] if o["value"]]
            pick = next((o for o in opts if o["selected"]), opts[0] if opts else None)
            if pick:
                data.append((name, pick["value"]))
        elif typ == "textarea":
            data.append((name, ""))
        elif typ == "hidden":
            data.append((name, a.get("value", "")))
        else:
            val = guess_value(f, page) if AUTO_BOOK else None
            data.append((name, val if val is not None else a.get("value", "")))
    for name, items in radios.items():
        pick = next((x for x in items if "checked" in x["attrs"]), items[0])
        data.append((name, pick["attrs"].get("value", "")))
    return data


def send_form(s, base_url, form, page, press=None):
    data = build_data(form, page, press)
    url = urljoin(base_url, form["action"] or base_url)
    print(f"  форма: {form['method'].upper()} {url.split('?')[0]} поля={[d[0] for d in data]}")
    if form["method"] == "post":
        return s.post(url, data=data, headers={"Referer": base_url}, timeout=30)
    return s.get(url, params=data, headers={"Referer": base_url}, timeout=30)


def submit_weiter(s, r):
    """Нажимаем 'Weiter' на странице выбора места."""
    page = parse(r.text)
    for form in page.forms:
        for f in form["fields"]:
            if ftype(f) in ("submit", "button", "image") and "weiter" in btn_label(f).lower():
                return send_form(s, r.url, form, page, press=f)
    for a in page.links:
        if "weiter" in a["text"].lower() and a["attrs"].get("href"):
            url = urljoin(r.url, a["attrs"]["href"])
            print(f"  Weiter-ссылка: {url}")
            return s.get(url, headers={"Referer": r.url}, timeout=30)
    print("  Кнопка Weiter не найдена")
    return r


# ----------------------------------------------------------------------------
# Выбор слота и запись
# ----------------------------------------------------------------------------
def is_disabled(c):
    a = c["attrs"]
    if "disabled" in a or a.get("aria-disabled", "").lower() == "true":
        return True
    if c["tag"] == "a" and not a.get("href") and not a.get("onclick"):
        return True
    cls = (a.get("class", "") + " " + a.get("id", "")).lower()
    return any(w in cls for w in DISABLED_WORDS)


def signature(c):
    a = c["attrs"]
    return (c["tag"], a.get("class", ""), a.get("type", ""), a.get("style", ""))


def pick_slot(cands):
    """Первый (самый ранний) доступный слот. Возвращает (элемент, пояснение)."""
    if not cands:
        return None, "нет кнопок со временем"
    enabled = [c for c in cands if not is_disabled(c)]
    if not enabled:
        return None, "все кнопки времени недоступны"
    if len(enabled) < len(cands):
        return enabled[0], f"доступных {len(enabled)} из {len(cands)} (по признаку disabled)"
    sigs = Counter(signature(c) for c in cands)
    if len(sigs) > 1:
        rare = min(sigs.values())
        minority = {sg for sg, n in sigs.items() if n == rare}
        pool = [c for c in cands if signature(c) in minority]
        return pool[0], f"выбрана по редкому оформлению ({len(pool)} из {len(cands)})"
    return cands[0], f"все {len(cands)} кнопок одинаковые, беру первую"


def activate(s, r, page, c):
    if c["tag"] == "a":
        href = c["attrs"].get("href", "")
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            raise RuntimeError("у кнопки времени нет ссылки (работает через JavaScript)")
        url = urljoin(r.url, href)
        print(f"  клик по ссылке времени: {url.split('?')[0]}")
        return s.get(url, headers={"Referer": r.url}, timeout=30)
    if c["form"] is None:
        raise RuntimeError("кнопка времени вне формы")
    return send_form(s, r.url, c["form"], page, press=c)


def choose_form(page):
    best, best_score = None, 0
    for form in page.forms:
        fillable = sum(
            1 for f in form["fields"]
            if ftype(f) not in ("hidden", "submit", "button", "image", "reset", "checkbox", "radio")
            and guess_value(f, page) not in (None, "")
        )
        has_confirm = any(
            ftype(f) in ("submit", "button") and CONFIRM_RE.search(btn_label(f))
            and not BACK_RE.search(btn_label(f)) for f in form["fields"]
        )
        score = fillable * 10 + (1 if has_confirm else 0)
        if score > best_score:
            best, best_score = form, score
    return best


def choose_button(form):
    subs = [f for f in form["fields"] if ftype(f) in ("submit", "button", "image")]
    good = [f for f in subs if CONFIRM_RE.search(btn_label(f)) and not BACK_RE.search(btn_label(f))]
    if good:
        return good[-1]
    ok = [f for f in subs if not BACK_RE.search(btn_label(f))]
    return ok[-1] if ok else None


def attempt_booking(s, r):
    """Возвращает (статус, текст). Статусы: success, unclear, failed."""
    page = parse(r.text)
    print(f"  кнопок со временем на странице: {len(page.cands)}")
    for c in page.cands[:6]:
        print(f"    {c['text']!r} tag={c['tag']} class={c['attrs'].get('class','')!r} "
              f"disabled={is_disabled(c)}")
    slot, note = pick_slot(page.cands)
    if slot is None:
        return "failed", note
    print(f"  выбран слот {slot['text']!r}: {note}")

    r = activate(s, r, page, slot)
    prev_text = ""
    for step in range(1, 7):
        text = to_text(r.text)
        low = text.lower()
        print(f"  шаг {step}: {r.url.split('?')[0]}\n    {redact(text[:300])}")
        pg = parse(r.text)
        form = choose_form(pg)
        if any(w in low for w in SUCCESS_WORDS) and form is None:
            return "success", redact(text[:400])
        if form is None:
            return "unclear", redact(text[:400])
        if text == prev_text:
            return "failed", "страница не меняется (возможно, ошибка в форме): " + redact(text[:300])
        prev_text = text
        btn = choose_button(form)
        print("    поля формы:", [(ftype(f), f["attrs"].get("name", "")) for f in form["fields"]])
        r = send_form(s, r.url, form, pg, press=btn)
    text = to_text(r.text)
    return "unclear", redact(text[:400])


# ----------------------------------------------------------------------------
def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                       "Mobile/15E148 Safari/604.1"),
        "Accept-Language": "de-DE,de;q=0.9",
    })
    return s


def check_once():
    s = new_session()
    s.get(START_URL, timeout=30).raise_for_status()
    r = s.get(LOCATION_URL, headers={"Referer": START_URL}, timeout=30)
    r.raise_for_status()
    r = submit_weiter(s, r)
    if "/suggest" not in r.url:
        r = s.get(SUGGEST_URL, headers={"Referer": r.url}, timeout=30)
    r.raise_for_status()

    text = to_text(r.text)
    print(f"  Итоговая страница: {r.url}\n  Текст: {text[:200]}")
    low = text.lower()
    if any(h in low for h in NO_SLOTS):
        return "none", None
    if TIME_RE.search(text) and ("uhrzeit" in low or "auswahl der zeit" in low):
        return "slots", (s, r, text)
    return "unknown", text[:500]


def notify(title: str, msg: str, priority="urgent"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "rotating_light",
                     "Click": START_URL},
            timeout=30,
        )
    except Exception as e:
        print("  не удалось отправить уведомление:", e)


def disable_workflow():
    """После записи отключаем workflow, чтобы не записаться второй раз."""
    tok, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        print("  GH_TOKEN/GITHUB_REPOSITORY не заданы, workflow не отключён")
        return
    try:
        resp = requests.put(
            f"https://api.github.com/repos/{repo}/actions/workflows/termin.yml/disable",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        print("  отключение workflow:", resp.status_code)
    except Exception as e:
        print("  не удалось отключить workflow:", e)


def handle_slots(s, r, text) -> bool:
    """True = завершить работу (запись сделана или передана тебе)."""
    dates = list(dict.fromkeys(m.group(0) for m in DATE_RE.finditer(text)))
    info = "Даты: " + ("; ".join(dates[:5]) or "?")
    print("СЛОТЫ НАЙДЕНЫ!", info)
    if not AUTO_BOOK:
        notify("Termin FREI!", "Появились слоты! Открой сайт и выбери время.\n\n" + info)
        return True

    notify("Termin: слоты найдены", "Пробую записаться автоматически...\n" + info)
    try:
        status, detail = attempt_booking(s, r)
    except Exception as e:
        status, detail = "failed", f"ошибка: {e}"
    print("РЕЗУЛЬТАТ:", status, detail[:300])

    if status == "success":
        notify("Termin ЗАПИСАНА!", "Похоже, запись выполнена. ПРОВЕРЬ ПОЧТУ (возможно, нужно "
               "подтвердить письмом).\n\n" + detail[:300])
        disable_workflow()
        return True
    if status == "unclear":
        notify("Termin: проверь запись", "Дошла до последнего шага, но результат неясен. "
               "Проверь почту и сайт.\n\n" + detail[:300])
        disable_workflow()
        return True
    notify("Termin: запись НЕ удалась", "Слоты были, автозапись не сработала. "
           "Открой сайт и запишись вручную!\n\n" + detail[:300])
    return True


def main():
    print("Автозапись:", "ВКЛ" if AUTO_BOOK else "выкл (только уведомления)")
    end = time.time() + LOOP_SECONDS
    ok_checks, n = 0, 0
    while True:
        n += 1
        print(f"[{time.strftime('%H:%M:%S')}] Проверка №{n}")
        try:
            status, info = check_once()
            if status == "slots":
                s, r, text = info
                if handle_slots(s, r, text):
                    return
            elif status == "none":
                ok_checks += 1
                print("  мест нет")
            else:
                print("  страница непонятная:", info)
        except Exception as e:
            print("  ошибка:", redact(str(e)))
        if time.time() + INTERVAL > end:
            break
        time.sleep(INTERVAL)

    if ok_checks == 0:
        print("Ни одна проверка не дала понятный результат.")
        sys.exit(1)


if __name__ == "__main__":
    main()
