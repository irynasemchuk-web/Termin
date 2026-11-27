"""
Следит за страницей /suggest (Fahrerlaubnisbehörde Düsseldorf).

Если появились слоты:
  * слот НЕ позже 03.12.2026 (включительно)  -> сам записывается на самый ранний
    (самая ранняя дата, затем самое раннее время), после успеха отключает workflow;
  * слоты только позже 03.12.2026            -> ничего не делает, проверки идут дальше.

Личные данные берутся только из секретов GitHub (BOOKING_*), в коде их нет.
"""
import html as htmllib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

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
# Слоты обычно выкладывают около 7:00 и 9:00 (Берлин): в эти минуты проверяем чаще
BURST_INTERVAL = int(os.environ.get("BURST_INTERVAL", "3"))
BURST_CENTERS = [(7, 0), (9, 0)]

# Записываем только на слоты до этой даты ВКЛЮЧИТЕЛЬНО (до конца дня)
BOOK_UNTIL = date.fromisoformat(os.environ.get("BOOKING_UNTIL", "2026-12-03"))
ANREDE = os.environ.get("BOOKING_ANREDE", "Frau")
# Как часто напоминать о слотах ПОЗЖЕ срока (минуты, 0 = не напоминать)
LATE_NOTICE_MINUTES = int(os.environ.get("LATE_NOTICE_MINUTES", "30"))

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
    DD, MM, YYYY = f"{int(_d[0]):02d}", f"{int(_d[1]):02d}", _d[2]
    BDATE_DE, BDATE_ISO = f"{DD}.{MM}.{YYYY}", f"{YYYY}-{MM}-{DD}"
else:
    DD = MM = YYYY = ""
    BDATE_DE = BDATE_ISO = BDATE

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
TIME_ONLY = re.compile(r"^\s*(([01]?\d|2[0-3]):[0-5]\d)(\s*Uhr)?\s*$")
DATE_ANY = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
NO_SLOTS = ("keine zeiten verfügbar", "keine freien termine")
DISABLED_WORDS = ("disabled", "inactive", "unavailable", "booked", "occupied",
                  "blocked", "nofree", "not-available", "belegt", "ausgebucht")
CONFIRM_RE = re.compile(r"weiter|buchen|bestätig|reservier|absenden|abschließ|verbindlich|termin", re.I)
BACK_RE = re.compile(r"zurück|abbrech|back|cancel|löschen", re.I)
CONSENT_WORDS = ("datenschutz", "privacy", "zustimm", "einverstanden", "agb",
                 "bedingung", "akzept", "consent", "einwillig", "willig")
SUCCESS_WORDS = ("erfolgreich", "wurde gebucht", "wurde reserviert", "terminbestätigung",
                 "vorgangsnummer", "buchungsnummer", "terminnummer", "termin ist gebucht",
                 "bestätigungs", "reservierung")
ERROR_WORDS = re.compile(r"fehler|pflichtfeld|ungültig|erforderlich|bitte (geben|füllen|wählen|korrigier)", re.I)
TAKEN_RE = re.compile(r"zwischenzeitlich|nicht mehr verfügbar|bereits vergeben|ist ein fehler aufgetreten", re.I)
TEXTLIKE = ("text", "number", "tel", "email", "date", "search", "")


def redact(t: str) -> str:
    for secret in {NAME, EMAIL, BDATE, BDATE_DE, PHONE, FIRST, LAST}:
        if secret and len(secret) > 2:
            t = re.sub(re.escape(secret), "***", t, flags=re.I)
    return t


def to_text(raw: str) -> str:
    t = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def body_text(text: str) -> str:
    """Убираем шапку про cookies, чтобы в логе было видно главное."""
    return text.split("Digitales Amt", 1)[1].strip() if "Digitales Amt" in text else text


def to_date(d, m, y):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Разбор HTML
# ----------------------------------------------------------------------------
class Page(HTMLParser):
    """Собирает формы, поля, ссылки и кнопки со временем (с датой секции)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.cands, self.links = [], [], []
        self.labels = {}
        self.cur_form = None
        self.stack = []
        self.cur_date = None
        self._buf = ""

    def _new(self, tag, a):
        return {"tag": tag, "attrs": a, "text": "", "form": self.cur_form,
                "opts": [], "children": [], "label": "", "date": None}

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
            el["date"] = self.cur_date
            self.cands.append(el)

    def _close_option(self):
        if self.stack and self.stack[-1]["tag"] == "option":
            o = self.stack.pop()
            text = re.sub(r"\s+", " ", o["text"]).strip()
            for el in reversed(self.stack):
                if el["tag"] == "select":
                    val = o["attrs"]["value"] if "value" in o["attrs"] else text
                    el["opts"].append({"value": val, "text": text,
                                       "selected": "selected" in o["attrs"]})
                    break

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
            self._close_option()
            self.stack.append(self._new("option", a))

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur_form = None
            return
        if tag in ("option", "select"):
            self._close_option()
            if tag == "option":
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
        # запоминаем последнюю дату из заголовков (например «Mittwoch, 06.01.2027»)
        self._buf = (self._buf + " " + data)[-80:]
        ms = list(DATE_ANY.finditer(self._buf))
        if ms:
            m = ms[-1]
            d = to_date(m.group(1), m.group(2), m.group(3))
            if d:
                self.cur_date = d
            self._buf = self._buf[m.end():]


def parse(html_text: str) -> Page:
    p = Page()
    p.feed(html_text)
    p.close()
    return p


# ----------------------------------------------------------------------------
# Формы
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


def key_of(f, page):
    a = f["attrs"]
    return " ".join([a.get("name", ""), a.get("id", ""), a.get("placeholder", ""),
                     a.get("aria-label", ""), f.get("label", ""),
                     page.labels.get(a.get("id", ""), "")]).lower()


def tokens(key):
    return set(t for t in re.split(r"[^a-zäöüß0-9]+", key) if t)


DAY_T, MONTH_T, YEAR_T = {"tag", "day", "dd"}, {"monat", "month", "mm"}, {"jahr", "year", "yyyy", "yy"}
BIRTH_HINT = re.compile(r"geburt|birth|gebdat|dob|bday")


def assign_birth(form, page):
    """Дата рождения может быть тремя полями (Tag/Monat/Jahr). Возвращает {id(поле): значение}."""
    fields = [f for f in form["fields"] if f["tag"] == "input" and ftype(f) in TEXTLIKE
              and ftype(f) != "email"]
    res = {}
    # 1) явные Tag / Monat / Jahr
    for f in fields:
        t = tokens(key_of(f, page))
        if t & DAY_T:
            res[id(f)] = DD
        elif t & MONTH_T:
            res[id(f)] = MM
        elif t & YEAR_T:
            res[id(f)] = YYYY
    # 2) три поля с намёком на дату рождения -> по порядку
    if not res:
        grp = [f for f in fields if BIRTH_HINT.search(key_of(f, page))]
        if len(grp) == 3:
            for f, v in zip(grp, (DD, MM, YYYY)):
                res[id(f)] = v
    # 3) три подряд поля с maxlength 2, 2, 4
    if not res:
        for i in range(len(fields) - 2):
            ml = [fields[i + k]["attrs"].get("maxlength", "") for k in range(3)]
            if ml == ["2", "2", "4"]:
                for k, v in enumerate((DD, MM, YYYY)):
                    res[id(fields[i + k])] = v
                break
    return res


def guess_value(f, page):
    """Что вписать в текстовое поле. None = оставить как есть."""
    typ = ftype(f)
    key = key_of(f, page)
    if "mail" in key or typ == "email":
        return EMAIL
    if any(w in key for w in ("vorname", "firstname", "first_name", "first-name")):
        return FIRST
    if any(w in key for w in ("nachname", "lastname", "last_name", "familienname", "surname")):
        return LAST
    if BIRTH_HINT.search(key):
        return BDATE_ISO if typ == "date" else BDATE_DE
    if re.search(r"telefon|phone|mobil|handy|\btel\b", key):
        return PHONE
    if "name" in key:
        return NAME
    return None


def choose_option(f, key):
    opts = [o for o in f["opts"] if o["value"] != ""]
    if not opts:
        return None
    if re.search(r"anrede|salutation|gender|geschlecht", key):
        for o in opts:
            if ANREDE.lower() in (o["text"] or o["value"]).lower():
                return o
    for o in opts:
        if o["selected"]:
            return o
    for o in opts:
        if re.search(r"\bkeine?\b", o["text"], re.I):
            return o
    return opts[0]


def fillable_count(form, page):
    special = assign_birth(form, page)
    n = 0
    for f in form["fields"]:
        if f["tag"] == "input" and ftype(f) in TEXTLIKE:
            if special.get(id(f)) or guess_value(f, page):
                n += 1
    return n


def build_data(form, page, press=None):
    data, radios, trace = [], {}, []
    special = assign_birth(form, page) if AUTO_BOOK else {}
    boxes = [f for f in form["fields"] if ftype(f) == "checkbox"]
    sym = {EMAIL: "EMAIL", FIRST: "FIRST", LAST: "LAST", NAME: "NAME", BDATE_DE: "BDATE",
           BDATE_ISO: "BDATE_ISO", DD: "DD", MM: "MM", YYYY: "YYYY", PHONE: "PHONE"}
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
            key = key_of(f, page)
            if ("checked" in a or "required" in a or len(boxes) == 1
                    or any(w in key for w in CONSENT_WORDS)):
                data.append((name, a.get("value") or "on"))
                trace.append(f"{name}=ДА")
        elif typ == "select":
            o = choose_option(f, key_of(f, page))
            if o:
                data.append((name, o["value"]))
                trace.append(f"{name}={o['text'] or o['value']}")
        elif typ == "textarea":
            data.append((name, ""))
        elif typ == "hidden":
            data.append((name, a.get("value", "")))
        else:
            val = None
            if AUTO_BOOK:
                val = special.get(id(f))
                if val is None:
                    val = guess_value(f, page)
            data.append((name, val if val is not None else a.get("value", "")))
            if val:
                trace.append(f"{name}={sym.get(val, '?')}")
    for name, items in radios.items():
        pick = next((x for x in items if "checked" in x["attrs"]), items[0])
        data.append((name, pick["attrs"].get("value", "")))
    return data, trace


def send_form(s, base_url, form, page, press=None):
    data, trace = build_data(form, page, press)
    url = urljoin(base_url, form["action"] or base_url)
    print(f"  форма: {form['method'].upper()} {url.split('?')[0]}")
    print(f"    поля: {[d[0] for d in data]}")
    if trace:
        print(f"    заполнено: {trace}")
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


def slot_key(c):
    m = TIME_ONLY.match(c["text"])
    h, mi = map(int, m.group(1).split(":"))
    return (c["date"] or date.max, h * 60 + mi)


def eligible_slots(cands):
    """Слоты не позже BOOK_UNTIL (или с неизвестной датой — её проверим на следующем шаге)."""
    return [c for c in cands if c["date"] is None or c["date"] <= BOOK_UNTIL]


def ordered_slots(cands):
    """Доступные слоты от самого раннего (дата, затем время). Возвращает (список, пояснение)."""
    if not cands:
        return [], "нет подходящих кнопок со временем"
    enabled = [c for c in cands if not is_disabled(c)]
    if not enabled:
        return [], "все кнопки времени недоступны"
    pool, note = enabled, "доступны"
    if len(enabled) == len(cands):
        sigs = Counter(signature(c) for c in cands)
        if len(sigs) > 1:
            rare = min(sigs.values())
            minority = {sg for sg, n in sigs.items() if n == rare}
            pool = [c for c in cands if signature(c) in minority]
            note = "выбраны по редкому оформлению"
    pool = sorted(pool, key=slot_key)  # sorted стабилен: порядок на странице сохраняется
    return pool, f"{note}: {len(pool)} из {len(cands)}"


def pick_slot(cands):
    pool, note = ordered_slots(cands)
    return (pool[0] if pool else None), note


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
        fillable = fillable_count(form, page)
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


def is_success(low):
    return any(w in low for w in SUCCESS_WORDS) or ("e-mail" in low and "bestätig" in low)


def appointment_date(text):
    m = re.search(r"Termin:?\s*(\d{2})\.(\d{2})\.(\d{4})", text) or DATE_ANY.search(text)
    return to_date(m.group(1), m.group(2), m.group(3)) if m else None


def attempt_booking(s, r, page, eligible, t0=None):
    """Статусы: success | unclear | failed | late | taken."""
    pool, note = ordered_slots(eligible)
    print(f"  подходящих слотов (до {BOOK_UNTIL:%d.%m.%Y}): {len(eligible)}, доступных: {len(pool)} ({note})")
    for c in pool[:6]:
        print(f"    {c['date']} {c['text']!r} class={c['attrs'].get('class', '')!r}")
    if not pool:
        return "late", note

    r2 = None
    for slot in pool[:5]:
        lag = f"{time.time() - t0:.2f} с после загрузки страницы" if t0 else ""
        print(f"  кликаю слот {slot['date']} {slot['text']!r} ({lag})")
        r2 = activate(s, r, page, slot)
        txt = to_text(r2.text)
        if TAKEN_RE.search(txt):
            print(f"    слот уже занят: {redact(body_text(txt)[:200])}")
            r2 = None
            continue
        break
    if r2 is None:
        return "taken", "все слоты были заняты за секунды до клика"
    r = r2

    prev_text, personal_sent = "", False
    for step in range(1, 7):
        text = to_text(r.text)
        low = text.lower()
        print(f"  шаг {step}: {r.url.split('?')[0]}\n    {redact(body_text(text)[:500])}")
        if step == 1:
            d = appointment_date(body_text(text))
            if d and d > BOOK_UNTIL:
                return "late", f"на странице записи дата {d:%d.%m.%Y} позже срока"
        pg = parse(r.text)
        form = choose_form(pg)
        if form is None:
            if is_success(low):
                return "success", redact(body_text(text)[:400])
            return "unclear", redact(body_text(text)[:400])
        if text == prev_text:
            return "failed", "страница не меняется: " + redact(body_text(text)[:300])
        prev_text = text
        has_personal = fillable_count(form, pg) > 0
        if has_personal and personal_sent:
            return "failed", "форма вернулась (ошибка заполнения?): " + redact(body_text(text)[:400])
        if has_personal:
            personal_sent = True
        btn = choose_button(form)
        print("    поля формы:", [(ftype(f), f["attrs"].get("name", "")) for f in form["fields"]])
        r = send_form(s, r.url, form, pg, press=btn)
    text = to_text(r.text)
    return ("success" if is_success(text.lower()) else "unclear"), redact(body_text(text)[:400])


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


SESSION = {"s": None, "age": 0}


def is_suggest_page(raw: str) -> bool:
    low = to_text(raw).lower()
    return any(w in low for w in ("keine zeiten verfügbar", "auswahl der zeit",
                                  "terminvorschläge", "uhrzeit"))


def fetch_suggest():
    """Страница /suggest. Пока сессия жива — просто перезагружаем её (1 запрос),
    иначе проходим весь путь заново (4 запроса)."""
    s = SESSION["s"]
    if s is not None and SESSION["age"] < 10:
        try:
            r = s.get(SUGGEST_URL, timeout=30)
            if r.ok and is_suggest_page(r.text):
                SESSION["age"] += 1
                return s, r
        except Exception as e:
            print("  сессия не подошла:", e)
    s = new_session()
    s.get(START_URL, timeout=30).raise_for_status()
    r = s.get(LOCATION_URL, headers={"Referer": START_URL}, timeout=30)
    r.raise_for_status()
    r = submit_weiter(s, r)
    if "/suggest" not in r.url:
        r = s.get(SUGGEST_URL, headers={"Referer": r.url}, timeout=30)
    r.raise_for_status()
    SESSION["s"], SESSION["age"] = s, 0
    return s, r


def check_once():
    s, r = fetch_suggest()
    t0 = time.time()
    text = to_text(r.text)
    print(f"  Страница: {r.url.split('?')[0]} | {body_text(text)[:150]}")
    low = text.lower()
    if any(h in low for h in NO_SLOTS):
        return "none", None
    if TIME_RE.search(text) and ("uhrzeit" in low or "auswahl der zeit" in low):
        return "slots", (s, r, t0)
    return "unknown", body_text(text)[:500]


def notify(title: str, msg: str, priority: int = 5) -> bool:
    """Push через ntfy. Отправляем как JSON: так работают русские буквы в заголовке
    (в обычных HTTP-заголовках они вызывают ошибку и уведомление не уходит)."""
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": msg[:3500],
        "priority": priority,
        "tags": ["rotating_light"] if priority >= 4 else ["eyes"],
        "click": START_URL,
    }
    try:
        r = requests.post("https://ntfy.sh", json=payload, timeout=30)
        print(f"  ntfy: {r.status_code} {'' if r.ok else r.text[:200]}")
        return r.ok
    except Exception as e:
        print("  не удалось отправить уведомление:", e)
        return False


def notify_async(title: str, msg: str, priority: int = 5):
    """Не задерживаем запись: уведомление уходит в фоне."""
    t = threading.Thread(target=notify, args=(title, msg, priority))
    t.start()
    return t


def recently_sent(title_prefix: str, minutes: int) -> bool:
    """Было ли такое уведомление за последние N минут (чтобы не спамить)."""
    try:
        r = requests.get(f"https://ntfy.sh/{NTFY_TOPIC}/json",
                         params={"poll": "1", "since": f"{minutes}m"}, timeout=15)
        for line in r.text.splitlines():
            ev = json.loads(line)
            if ev.get("event") == "message" and (ev.get("title") or "").startswith(title_prefix):
                return True
    except Exception as e:
        print("  не удалось проверить прошлые уведомления:", e)
    return False


def disable_workflow():
    """После подтверждённой записи отключаем workflow, чтобы не записаться второй раз."""
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


def handle_slots(s, r, t0=None) -> bool:
    """True = завершить этот запуск. False = продолжать проверки."""
    page = parse(r.text)
    cands = page.cands
    dates = sorted({c["date"] for c in cands if c["date"]})
    info = "Даты: " + (", ".join(f"{d:%d.%m.%Y}" for d in dates[:5]) or "?")
    print("СЛОТЫ НА СТРАНИЦЕ.", info)

    if not AUTO_BOOK:
        notify("Termin: слоты есть!", "Появились слоты! Открой сайт и выбери время.\n\n" + info)
        return True

    eligible = eligible_slots(cands)
    slot = pick_slot(eligible)[0] if eligible else None

    if slot is None:
        # Слоты есть, но все позже срока (или недоступны): не записываемся, не останавливаемся.
        print(f"  подходящих слотов до {BOOK_UNTIL:%d.%m.%Y} нет, продолжаю проверять")
        if cands and LATE_NOTICE_MINUTES > 0 and not recently_sent("Termin: слоты позже", LATE_NOTICE_MINUTES):
            first = min(cands, key=slot_key)
            when = f"{first['date']:%d.%m.%Y} {first['text']}" if first["date"] else first["text"]
            notify("Termin: слоты позже срока",
                   f"Слоты появились, но позже {BOOK_UNTIL:%d.%m.%Y}. Самый ранний: {when}. "
                   "Не записываюсь, продолжаю следить.\n\n" + info, priority=3)
        return False

    when = f"{slot['date']:%d.%m.%Y} {slot['text']}" if slot["date"] else slot["text"]
    # Главное уведомление: приходит ДО записи, чтобы ты знала, что идёт запись именно сейчас
    first_msg = notify_async("СЛОТ НАЙДЕН! Записываюсь СЕЙЧАС",
                             f"Слот {when}. Прямо сейчас идёт автоматическая запись. "
                             "Результат пришлю следующим сообщением.\n\n" + info)
    try:
        status, detail = attempt_booking(s, r, page, eligible, t0)
    except Exception as e:
        status, detail = "failed", f"ошибка: {redact(str(e))}"
    SESSION["s"] = None  # после попытки сессия уже «испорчена»
    first_msg.join(timeout=20)
    print("РЕЗУЛЬТАТ:", status, detail[:300])

    if status == "taken":
        if not recently_sent("Termin: слот уже занят", 2):
            notify("Termin: слот уже занят", f"Слот {when} разобрали за секунды до моего клика. "
                   "Продолжаю следить.", priority=3)
        return False

    if status == "late":
        print("  слот оказался позже срока или недоступен, продолжаю проверять")
        return False
    if status == "success":
        notify("Termin ЗАПИСАНА!", "Запись выполнена. ПРОВЕРЬ ПОЧТУ: возможно, нужно нажать "
               "ссылку подтверждения.\n\n" + detail[:300])
        disable_workflow()
        return True
    if status == "unclear":
        notify("Termin: проверь запись", "Дошла до конца, но результат неясен. Проверь почту и "
               "сайт. Проверки НЕ остановлены.\n\n" + detail[:300])
        return True
    notify("Termin: запись НЕ удалась", "Слот был, автозапись не сработала. "
           "Открой сайт и запишись вручную!\n\n" + detail[:300])
    return True


def current_interval(now=None) -> float:
    """Около 7:00 и 9:00 (по Берлину) проверяем каждые несколько секунд, в остальное время реже."""
    if ZoneInfo is None:
        return INTERVAL
    try:
        now = now or datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        return INTERVAL
    for h, m in BURST_CENTERS:
        center = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if center - timedelta(minutes=1) <= now <= center + timedelta(minutes=6):
            return BURST_INTERVAL
    return INTERVAL


def main():
    if os.environ.get("NOTIFY_TEST", "").lower() == "true":
        ok = notify("Termin: тест уведомления", "Если ты это видишь на экране телефона, "
                    "уведомления работают.")
        print("Тест уведомления:", "отправлен" if ok else "НЕ отправлен")
        return
    print("Автозапись:", "ВКЛ" if AUTO_BOOK else "выкл (только уведомления)",
          f"| записываюсь до {BOOK_UNTIL:%d.%m.%Y} включительно")
    end = time.time() + LOOP_SECONDS
    ok_checks, n = 0, 0
    while True:
        n += 1
        print(f"[{time.strftime('%H:%M:%S')}] Проверка №{n}")
        try:
            status, info = check_once()
            if status == "slots":
                ok_checks += 1
                s, r, t0 = info
                if handle_slots(s, r, t0):
                    return
            elif status == "none":
                ok_checks += 1
                print("  мест нет")
            else:
                print("  страница непонятная:", info)
        except Exception as e:
            print("  ошибка:", redact(str(e)))
        wait = current_interval()
        if time.time() + wait > end:
            break
        time.sleep(wait)

    if ok_checks == 0:
        print("Ни одна проверка не дала понятный результат.")
        sys.exit(1)


if __name__ == "__main__":
    main()
