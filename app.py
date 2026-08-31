"""Office desk booking app.

The office has five identical desks, so a booking is simply a claim on one of
the five slots for a given day -- no desk numbers. Bookings are stored in
SQLite and shared by everyone: a month overview is the front page, and each day
drills down to a view where desks are booked.
"""

import calendar
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BOOKING_DB", os.path.join(BASE_DIR, "bookings.db"))

CAPACITY = 5
MAX_RANGE_DAYS = 366  # a booking range may not span more than a year

# Public holidays come from Nager.Date -- free, no API key, no rate limit.
# v3 is used rather than v4 because only v3 returns localName (the Russian
# name); v4 is English-only. Holidays are cached in SQLite, so the app works
# offline and calls the service at most once per year of calendar viewed.
HOLIDAY_COUNTRY = os.environ.get("HOLIDAY_COUNTRY", "RU")
HOLIDAY_API = os.environ.get(
    "HOLIDAY_API", "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
)
HOLIDAY_SYNC = os.environ.get("HOLIDAY_SYNC", "on").lower() != "off"
HOLIDAY_TIMEOUT = float(os.environ.get("HOLIDAY_TIMEOUT", "5"))
REFRESH_OK_DAYS = 30      # re-check a year we already have, occasionally
RETRY_FAILED_HOURS = 6    # but do not hammer the service after a failure

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_date TEXT    NOT NULL,
    surname      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (booking_date, surname COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings (booking_date);

CREATE TABLE IF NOT EXISTS holidays (
    holiday_date TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    name_en      TEXT
);

CREATE TABLE IF NOT EXISTS holiday_sync (
    year       INTEGER PRIMARY KEY,
    fetched_at TEXT    NOT NULL,
    ok         INTEGER NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    try:
        columns = [
            r[1] for r in db.execute("PRAGMA table_info(bookings)").fetchall()
        ]
        if "workspace" in columns:
            # Older per-desk schema: keep the bookings, drop the desk numbers.
            # Two desks held by one surname on one day collapse into one row.
            db.executescript(
                """
                BEGIN;
                ALTER TABLE bookings RENAME TO bookings_old;
                CREATE TABLE bookings (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_date TEXT    NOT NULL,
                    surname      TEXT    NOT NULL,
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (booking_date, surname COLLATE NOCASE)
                );
                INSERT OR IGNORE INTO bookings (booking_date, surname, created_at)
                    SELECT booking_date, surname, created_at
                    FROM bookings_old ORDER BY id;
                DROP TABLE bookings_old;
                COMMIT;
                """
            )
        db.executescript(SCHEMA)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Russian text
# --------------------------------------------------------------------------

# Month names are needed in two cases: nominative for a heading ("Сентябрь
# 2026") and genitive for a date ("1 сентября"). Python's locale support for
# this is unreliable across machines, so the names are spelled out here.
MONTHS_NOMINATIVE = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_SHORT = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]
WEEKDAYS = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def surname_key(surname):
    """Sort key for Russian surnames.

    Code-point order is almost the Russian alphabet, except ё (U+0451) lands
    after я instead of next to е. Russian collation treats ё as equal to е and
    only uses it to break a tie between otherwise identical words, so the
    primary key folds ё to е and the original spelling breaks ties.
    """
    folded = surname.casefold()
    return (folded.replace("ё", "е"), folded)


def plural(n, one, few, many):
    """Russian plural: 1 день, 2 дня, 5 дней."""
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def ru_month(day):
    """'Сентябрь 2026' -- for a heading."""
    return "%s %d" % (MONTHS_NOMINATIVE[day.month - 1], day.year)


def ru_date(day):
    """'1 сентября 2026'."""
    return "%d %s %d" % (day.day, MONTHS_GENITIVE[day.month - 1], day.year)


def ru_day_month(day):
    """'1 сентября' -- no year."""
    return "%d %s" % (day.day, MONTHS_GENITIVE[day.month - 1])


def ru_short(day):
    """'1 сен' -- compact, for listing skipped days."""
    return "%d %s" % (day.day, MONTHS_SHORT[day.month - 1])


def ru_weekday(day):
    """'Понедельник'."""
    return WEEKDAYS[day.weekday()]


def ru_full(day):
    """'Понедельник, 1 сентября' -- the popup heading."""
    return "%s, %s" % (ru_weekday(day), ru_day_month(day))


app.jinja_env.filters.update(
    ru_month=ru_month,
    ru_date=ru_date,
    ru_day_month=ru_day_month,
    ru_weekday=ru_weekday,
    ru_full=ru_full,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_date(value, fallback):
    """Parse an ISO date string, falling back on anything unparseable."""
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def month_of(value, fallback):
    """Parse a YYYY-MM string into the first of that month."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m").date().replace(day=1)
        except ValueError:
            pass
    return fallback.replace(day=1)


def shift_month(first, delta):
    """First day of the month `delta` months away from `first`."""
    index = (first.year * 12 + first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def bookings_in_range(start, end):
    """Return {iso_date: [{id, surname}, ...]} for bookings in [start, end]."""
    rows = get_db().execute(
        "SELECT id, booking_date, surname FROM bookings"
        " WHERE booking_date >= ? AND booking_date <= ?"
        " ORDER BY id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["booking_date"], []).append(
            {"id": r["id"], "surname": r["surname"]}
        )
    for names in by_day.values():
        names.sort(key=lambda b: surname_key(b["surname"]))
    return by_day


def bookings_for_day(day):
    """Return the day's booking rows, sorted by surname."""
    rows = get_db().execute(
        "SELECT id, surname FROM bookings WHERE booking_date = ?",
        (day.isoformat(),),
    ).fetchall()
    return sorted(rows, key=lambda r: surname_key(r["surname"]))


def day_payload(day, bookings=None, holiday=-1):
    """The state of one day, as the popup needs it."""
    if holiday == -1:
        holiday = holiday_on(day)
    if bookings is None:
        bookings = [
            {"id": r["id"], "surname": r["surname"]}
            for r in bookings_for_day(day)
        ]
    return {
        "iso": day.isoformat(),
        "label": ru_full(day),
        "bookings": bookings,
        "free": CAPACITY - len(bookings),
        "total": CAPACITY,
        "past": day < date.today(),
        "holiday": holiday,
    }


def fetch_holidays(year):
    """Ask the holiday service for one year. Returns [] on any failure."""
    url = HOLIDAY_API.format(year=year, country=HOLIDAY_COUNTRY)
    req = urllib.request.Request(url, headers={"User-Agent": "office-booking/1.0"})
    with urllib.request.urlopen(req, timeout=HOLIDAY_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    found = []
    for item in payload:
        day = item.get("date")
        if not day:
            continue
        # v3 gives localName (Russian) and name (English); tolerate either
        # being absent so a change upstream degrades instead of breaking.
        local = item.get("localName") or item.get("name")
        if not local:
            continue
        found.append((day, local, item.get("name")))
    return found


def sync_holidays(years):
    """Cache holidays for `years`, skipping years fetched recently.

    Network problems are swallowed on purpose: the calendar must render with
    or without the holiday service.
    """
    if not HOLIDAY_SYNC:
        return
    db = get_db()
    now = datetime.now()
    known = {
        r["year"]: (r["fetched_at"], r["ok"])
        for r in db.execute("SELECT year, fetched_at, ok FROM holiday_sync")
    }

    for year in sorted(set(years)):
        if year in known:
            try:
                age = now - datetime.strptime(known[year][0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                age = None
            if age is not None:
                fresh_ok = known[year][1] and age < timedelta(days=REFRESH_OK_DAYS)
                backoff = not known[year][1] and age < timedelta(hours=RETRY_FAILED_HOURS)
                if fresh_ok or backoff:
                    continue
        try:
            found = fetch_holidays(year)
            ok = 1
        except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
            app.logger.warning("holiday sync failed for %s: %s", year, exc)
            found, ok = [], 0

        if ok:
            db.execute(
                "DELETE FROM holidays WHERE holiday_date BETWEEN ? AND ?",
                ("%d-01-01" % year, "%d-12-31" % year),
            )
            db.executemany(
                "INSERT OR REPLACE INTO holidays (holiday_date, name, name_en)"
                " VALUES (?, ?, ?)",
                found,
            )
        db.execute(
            "INSERT OR REPLACE INTO holiday_sync (year, fetched_at, ok)"
            " VALUES (?, ?, ?)",
            (year, now.strftime("%Y-%m-%d %H:%M:%S"), ok),
        )
        db.commit()


def holidays_in_range(start, end):
    """Return {iso_date: {"name": ..., "name_en": ...}} for cached holidays."""
    rows = get_db().execute(
        "SELECT holiday_date, name, name_en FROM holidays"
        " WHERE holiday_date >= ? AND holiday_date <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {
        r["holiday_date"]: {"name": r["name"], "name_en": r["name_en"]}
        for r in rows
    }


def holiday_on(day):
    """The holiday falling on `day`, or None."""
    return holidays_in_range(day, day).get(day.isoformat())


def payloads_for(dates):
    """day_payload() for several dates, without a query per date."""
    if not dates:
        return {}
    booked = bookings_in_range(min(dates), max(dates))
    holidays = holidays_in_range(min(dates), max(dates))
    return {
        d.isoformat(): day_payload(
            d, booked.get(d.isoformat(), []), holidays.get(d.isoformat())
        )
        for d in dates
    }


def humanise(dates, limit=3):
    """'5 сен, 12 сен и ещё 2' -- for summarising skipped days."""
    shown = [ru_short(d) for d in dates[:limit]]
    extra = len(dates) - len(shown)
    if extra > 0:
        shown.append("ещё %d" % extra)
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + " и " + shown[-1]


def wants_json():
    """True when the caller is the popup's fetch(), not a browser navigation."""
    return request.headers.get("X-Requested-With") == "fetch"


def respond(day_field, message, category):
    """Reply to a book/cancel: JSON for the popup, a redirect for plain forms."""
    if wants_json():
        return jsonify(
            ok=(category == "success"),
            message=message,
            category=category,
            day=day_payload(parse_date(day_field, date.today())),
        )
    flash(message, category)
    return redirect(url_for("day_view", day=day_field or None))


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    """Month overview: how full each day is, at a glance."""
    today = date.today()
    first = month_of(request.args.get("month"), today)

    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(
        first.year, first.month
    )
    sync_holidays({weeks[0][0].year, weeks[-1][-1].year})
    booked = bookings_in_range(weeks[0][0], weeks[-1][-1])
    holidays = holidays_in_range(weeks[0][0], weeks[-1][-1])

    month_grid = []
    days = {}
    for week in weeks:
        row = []
        for d in week:
            names = booked.get(d.isoformat(), [])
            holiday = holidays.get(d.isoformat())
            days[d.isoformat()] = day_payload(d, names, holiday)
            row.append(
                {
                    "date": d,
                    "iso": d.isoformat(),
                    "in_month": d.month == first.month,
                    "is_today": d == today,
                    "is_past": d < today,
                    "is_weekend": d.weekday() >= 5,
                    "weekday": WEEKDAYS_SHORT[d.weekday()],
                    "names": [b["surname"] for b in names],
                    "free": CAPACITY - len(names),
                    "holiday": holiday,
                }
            )
        month_grid.append(row)

    return render_template(
        "month.html",
        month_grid=month_grid,
        days=days,
        first=first,
        total=CAPACITY,
        weekday_names=WEEKDAYS_SHORT,
        prev_month=shift_month(first, -1).strftime("%Y-%m"),
        next_month=shift_month(first, 1).strftime("%Y-%m"),
        this_month=today.strftime("%Y-%m"),
        is_this_month=(first.year, first.month) == (today.year, today.month),
    )


@app.route("/day")
def day_view():
    """One day: who has a desk, and how many are left."""
    today = date.today()
    day = parse_date(request.args.get("day"), today)

    sync_holidays({day.year})
    booked = bookings_for_day(day)

    return render_template(
        "day.html",
        holiday=holiday_on(day),
        day=day,
        today=today,
        booked=booked,
        free_count=CAPACITY - len(booked),
        total=CAPACITY,
        is_past=day < today,
        prev_day=(day - timedelta(days=1)).isoformat(),
        next_day=(day + timedelta(days=1)).isoformat(),
        today_iso=today.isoformat(),
        month=day.strftime("%Y-%m"),
    )


@app.route("/book", methods=["POST"])
def book():
    surname = (request.form.get("surname") or "").strip()
    day_field = (request.form.get("date") or "").strip()
    parsed = parse_date(day_field, None)

    if not surname:
        return respond(day_field, "Введите фамилию.", "error")
    if len(surname) > 60:
        return respond(day_field, "Фамилия слишком длинная (максимум 60 символов).", "error")
    if parsed is None:
        return respond(day_field, "Некорректная дата.", "error")
    if parsed < date.today():
        return respond(day_field, "Эта дата уже прошла.", "error")

    db = get_db()
    try:
        # BEGIN IMMEDIATE takes the write lock up front, so the capacity
        # check and the insert cannot interleave with another booking.
        db.execute("BEGIN IMMEDIATE")
        taken = db.execute(
            "SELECT COUNT(*) FROM bookings WHERE booking_date = ?",
            (parsed.isoformat(),),
        ).fetchone()[0]
        if taken >= CAPACITY:
            db.rollback()
            return respond(
                day_field,
                "На %s все %d мест заняты."
                % (ru_day_month(parsed), CAPACITY),
                "error",
            )
        db.execute(
            "INSERT INTO bookings (booking_date, surname) VALUES (?, ?)",
            (parsed.isoformat(), surname),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return respond(day_field, "На этот день уже есть бронь: %s." % surname, "error")

    return respond(
        day_field,
        "Место забронировано на %s: %s. Осталось %d."
        % (ru_day_month(parsed), surname, CAPACITY - taken - 1),
        "success",
    )


@app.route("/cancel", methods=["POST"])
def cancel():
    """Release a desk. One click -- no confirmation, as the office asked."""
    booking_id = request.form.get("booking_id", "")
    day_field = request.form.get("day") or ""

    db = get_db()
    row = db.execute(
        "SELECT surname FROM bookings WHERE id = ?", (booking_id,)
    ).fetchone()

    if row is None:
        return respond(day_field, "Этой брони больше нет.", "error")

    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    return respond(day_field, "Бронь отменена: %s." % row["surname"], "success")


@app.route("/book-range", methods=["POST"])
def book_range():
    """Book every matching weekday between two dates, best effort."""
    surname = (request.form.get("surname") or "").strip()
    start = parse_date(request.form.get("start"), None)
    end = parse_date(request.form.get("end"), None)
    month_field = request.form.get("month") or ""

    try:
        weekdays = {int(w) for w in request.form.getlist("weekday")}
    except ValueError:
        weekdays = set()
    weekdays = {w for w in weekdays if 0 <= w <= 6}

    def fail(message):
        if wants_json():
            return jsonify(ok=False, message=message, category="error", days={})
        flash(message, "error")
        return redirect(url_for("index", month=month_field or None))

    if not surname:
        return fail("Введите фамилию.")
    if len(surname) > 60:
        return fail("Фамилия слишком длинная (максимум 60 символов).")
    if start is None or end is None:
        return fail("Укажите корректные даты начала и окончания.")
    if end < start:
        return fail("Дата окончания раньше даты начала.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return fail("Период не может быть длиннее года.")
    if not weekdays:
        return fail("Выберите хотя бы один день недели.")

    today = date.today()
    span = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    matching = [d for d in span if d.weekday() in weekdays]
    past = [d for d in matching if d < today]
    wanted = [d for d in matching if d >= today]

    if not matching:
        return fail("В этом периоде нет выбранных дней недели.")

    booked, full, already = [], [], []
    db = get_db()
    try:
        # One write transaction for the whole range, so the capacity counts
        # cannot drift while we are deciding what fits.
        db.execute("BEGIN IMMEDIATE")
        if wanted:
            isos = [d.isoformat() for d in wanted]
            marks = ",".join("?" * len(isos))
            counts = dict(
                db.execute(
                    "SELECT booking_date, COUNT(*) FROM bookings"
                    " WHERE booking_date IN (%s) GROUP BY booking_date" % marks,
                    isos,
                ).fetchall()
            )
            mine = {
                r[0]
                for r in db.execute(
                    "SELECT booking_date FROM bookings"
                    " WHERE booking_date IN (%s)"
                    " AND surname = ? COLLATE NOCASE" % marks,
                    isos + [surname],
                ).fetchall()
            }
            for d in wanted:
                iso = d.isoformat()
                if iso in mine:
                    already.append(d)
                elif counts.get(iso, 0) >= CAPACITY:
                    full.append(d)
                else:
                    counts[iso] = counts.get(iso, 0) + 1
                    booked.append(d)
            db.executemany(
                "INSERT INTO bookings (booking_date, surname) VALUES (?, ?)",
                [(d.isoformat(), surname) for d in booked],
            )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise

    parts = []
    if booked:
        parts.append(
            "%s %d %s: %s."
            % (
                plural(len(booked), "Забронирован", "Забронировано", "Забронировано"),
                len(booked),
                plural(len(booked), "день", "дня", "дней"),
                surname,
            )
        )
    else:
        parts.append("Ничего не забронировано.")
    if full:
        parts.append("Уже занято: %s." % humanise(full))
    if already:
        parts.append("Уже забронировано (%s): %s." % (surname, humanise(already)))
    if past:
        parts.append(
            "%s %d %s в прошлом."
            % (
                plural(len(past), "Пропущен", "Пропущено", "Пропущено"),
                len(past),
                plural(len(past), "день", "дня", "дней"),
            )
        )
    message = " ".join(parts)
    category = "success" if booked else "error"

    if wants_json():
        return jsonify(
            ok=bool(booked),
            message=message,
            category=category,
            days=payloads_for(wanted),
        )
    flash(message, category)
    return redirect(url_for("index", month=month_field or None))


init_db()

if __name__ == "__main__":
    # Port 5001 by default: on macOS, AirPlay Receiver holds *:5000 including
    # IPv6, so a browser resolving "localhost" to ::1 reaches AirPlay (an empty
    # 403) instead of this app. Override with HOST/PORT if you need to.
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5001")),
        debug=True,
    )
