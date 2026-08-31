# Office Desk Booking

The interface is in Russian; this README is in English.

A small Flask app for booking the office's 5 desks. Bookings are stored in
SQLite and everyone sees the same calendar.

## Run

```bash
pip install -r requirements.txt
npm install          # first time only, for the CSS build
python3 app.py
```

Then open http://127.0.0.1:5001

The default port is 5001, not 5000: on macOS, **AirPlay Receiver** listens on
`*:5000` including IPv6, and a browser resolving `localhost` to `::1` reaches
AirPlay instead of this app — which answers with an empty 403, so the page looks
blank. Override with `PORT=8000 python3 app.py` if 5001 is taken.

To let colleagues on the same network reach it, run
`HOST=0.0.0.0 python3 app.py` and give them `http://<your-ip>:5001`.

## Docker

```bash
docker build -t office-booking .

docker run -d --name booking -p 5001:5001 \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  -v booking-data:/data \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges --init \
  office-booking
```

The image builds in three stages — Node compiles the stylesheet, pip builds a
virtualenv, and a slim runtime image receives only those two results — so npm and
the build tooling never reach production. It serves with gunicorn rather than the
Flask dev server, and runs as an unprivileged `uid 10001` with no home directory
and no login shell.

**`SECRET_KEY` is mandatory.** `app.py` falls back to `dev-secret-change-me` when
it is unset, which is harmless on a laptop and dangerous once the app is
reachable: that key signs Flask session cookies, so a publicly known value means
anyone can forge one. `docker-entrypoint.sh` refuses to start on the default, or
on no value at all, instead of quietly serving traffic with a known secret. Pass
the real key with `--env-file` or a secrets manager rather than inline, so it
stays out of your shell history and the container's metadata.

**`/data` is the only writable path.** The database lives at `/data/bookings.db`
(`BOOKING_DB` is already set for you), and everything else — including the app's
own code, which is owned by root and merely readable — cannot be modified by the
running app. That is what makes `--read-only` above work. Without a volume
mounted at `/data` the app cannot create its database and will not start.

The remaining flags are load-bearing, not decoration: `--cap-drop ALL` removes
every Linux capability, `--security-opt no-new-privileges` blocks setuid
escalation, `--init` supplies a real init to reap processes, and `--tmpfs /tmp`
is required precisely because the root filesystem is read-only.

### docker compose

The same container, defined as a file rather than a long command line:

```bash
cp .env.example .env     # then put a real SECRET_KEY in it
docker compose up -d     # or: docker-compose up -d
```

`docker-compose.yml` carries every hardening flag from the `docker run` above,
plus the parts a long-running deployment needs and a one-off command does not:
`restart: unless-stopped`, a 30s stop grace period, log rotation at 5 × 10MB so
an unbounded log cannot fill the disk, and CPU/memory limits so a runaway
process cannot take the host with it.

The secret is read from `.env`, which is gitignored; `.env.example` is the
committed template. Compose refuses to start if `.env` is absent, and the
container refuses to start if the key inside it is empty or the development
default. `.env` is also outside the build context — `.dockerignore` allows only
what the image needs — so the secret cannot end up in an image layer.

The database lives in the named volume `booking-data`, which survives
`docker compose down`. It is the only thing here worth backing up. To reset the
app completely, `docker compose down -v`.

### Dependencies

`requirements-prod.txt` pins every package, direct and transitive, to an exact
version and its wheel hashes. The image installs it with `--require-hashes`, so a
package that has been swapped out or tampered with on PyPI fails the build rather
than shipping. Edit `requirements-prod.in` and recompile after changing a
dependency:

```bash
uv pip compile requirements-prod.in --generate-hashes \
    --python-version 3.13 --python-platform linux \
    --output-file requirements-prod.txt
```

`requirements.txt` is deliberately left loose for local development. The lock is
compiled for the image's Linux / CPython 3.13 target, and MarkupSafe ships
platform-specific compiled wheels, so its hashes will not match an install on
macOS.

Gunicorn is started with `--no-control-socket`: since version 26 it opens a unix
control socket under `$HOME` by default, which errors on every start for a user
with no home, and is a control channel into the running server that this app has
no use for.

### Known gaps

The base images are pinned by tag (`python:3.13-slim-bookworm`), not by digest,
so a rebuild can pick up a rebuilt upstream image. `docker buildx imagetools
inspect python:3.13-slim-bookworm` prints the digest to pin if you want the build
fully reproducible.

The `SECRET_KEY` fallback is guarded only at the container boundary. Running
`python3 app.py` directly on a reachable host still serves with the development
key.

## Styling

The UI uses [daisyUI 5](https://daisyui.com) on Tailwind CSS 4. The stylesheet is
built once into `static/app.css` and committed, so running the app needs only
Python — Node is required only to rebuild it.

```bash
npm run css          # rebuild after editing templates
npm run css:watch    # rebuild on every change while working
```

The UI is built from daisyUI components: `navbar`, `card`, `table`, `modal`,
`list`, `stats`, `alert`, `badge`, `status`, `tooltip`, `input`, `fieldset`,
`join`, `checkbox`, `button` and `link`.

`src/app.css` restricts daisyUI to the components the templates actually use and
to a custom `evgroup` theme (defined in `@plugin "daisyui/theme"` and set on
`<html data-theme="evgroup">` — change both together); the full library is ~390KB
of CSS, the trimmed build is ~49KB. `month.html` assembles some class names in
JavaScript, where Tailwind's scanner cannot see them, so those are listed in
`@source inline(...)` — add to that list if you introduce new dynamic classes.

The import uses `source(none)`, which turns off Tailwind's automatic content
detection, and the sources are then declared explicitly. Without it Tailwind
scans the whole project and finds `.claude/skills/daisyui/components/*.md` —
daisyUI's own documentation, whose examples demonstrate every variant of every
component — adding ~23KB of CSS for classes nothing here uses.

## Font

The base face is **TT Hoves Pro Trial** (TypeType), set as `--font-sans` in
`src/app.css`, which daisyUI and every component inherit.

Four weights are self-hosted from `static/fonts/` — Regular 400, Medium 500,
DemiBold 600, Bold 700, each as `woff2` with a `woff` fallback. Each
`@font-face` also tries `local()` first, so an installed copy is used without a
network fetch. If the files were ever missing the stack falls through to the
system UI font.

**Licence:** these are *trial* files, licensed by TypeType for evaluation and
non-commercial use. Serving them to the whole office from this app is outside
those terms — buy the licence, or swap `--font-sans` for an openly-licensed
face, before this goes into daily use.

## Language

The UI is Russian. Interface strings live in the templates and in the message
strings in `app.py` — there is no translation framework, since the app is
single-language.

Dates are formatted by helpers in `app.py` rather than `strftime`, because
Russian needs two forms of each month name — nominative for a heading
("Сентябрь 2026") and genitive for a date ("1 сентября") — and Python's locale
support for this is unreliable across machines. `plural()` handles the
1 / 2–4 / 5+ agreement ("1 день", "2 дня", "5 дней"), including the verb
("Забронирован 1 день" vs "Забронировано 6 дней").

Surnames are never inflected: a message puts them in the nominative after a
colon or in brackets ("Бронь отменена: Иванов"), because declension cannot be
done reliably in code — Иванов → Иванова, but Черных → Черных.

Holiday names come from the API's `localName`, so they are already Russian and
the English name is no longer shown.

## Colours

The palette is taken from [ev.group](https://ev.group)'s own stylesheet, not
guessed from a screenshot:

| Role | Value | Where it comes from |
| --- | --- | --- |
| `primary` | `#0053cf` | their brand blue, the most-used colour on the site |
| `base-content` | `#181f25` | their body text |
| `base-200` | `#f0f4f8` | their pale section background |
| `base-300` | `#dcddde` | their `rgba(24,31,37,.15)` border, flattened on white |
| `neutral` | `#121821` | their dark header/footer bar |
| `secondary` | `#15846f` | their teal `#199c83`, darkened for contrast |
| `accent` | `#62e5cc` | their mint |
| `info` | `#1369ea` | their lighter blue |
| `error` | `#e02523` | their red `#f22c2b`, darkened for contrast |

Two colours are not verbatim. Their teal (3.43:1 on white) and red (4.06:1)
only reach WCAG AA for *large* text, and this app uses both at 10–12px — holiday
names and Cancel links. They are darkened on the same hue to clear 4.5:1.

Surname chips copy their chip pattern — brand-blue text on a pale blue fill
(`badge-soft badge-primary`) — and the header bar is dark like theirs. Holidays
use the teal so they read as a different kind of thing from bookings.

## How it works

- The front page is a **month calendar**. Each day lists the surnames holding a
  desk that day. Arrows move between months.
- Click any day to open a **popup** for it, without leaving the page: it lists
  who has a desk and the free slots. Type a surname and press **Book** and the
  popup and the month cell both update in place.
- **Cancel** next to a name releases that desk immediately — one click, no
  confirmation.
- **Book several days** (top right) books a repeating pattern: a surname, a
  from/to period, and the weekdays you come in — e.g. every Tuesday and Friday
  in September. It is best effort: days that are already full, or that you
  already hold, are skipped and named in the summary, as are any days in the
  past. Ranges are capped at one year.
- **Russian public holidays are dimmed** in the month grid with their name shown
  in the cell; opening the day popup shows the full commentary (Russian and
  English, e.g. "День России — Russia Day"). Holidays are still bookable — the
  app flags them, it does not block them.
- Past days are read-only. A surname can hold at most one desk per day.
- Your surname is remembered in the browser's localStorage so repeat bookings
  are quicker. It is not used for authentication.
- With JavaScript disabled, clicking a day falls back to a full page at
  `/day?day=YYYY-MM-DD` that does the same thing with plain form posts.

## Data

SQLite file `bookings.db` is created next to `app.py` on first run. Override the
location with the `BOOKING_DB` environment variable — the container image uses it
to put the database on the `/data` volume, which is its only writable path.

```sql
CREATE TABLE bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_date TEXT    NOT NULL,
    surname      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (booking_date, surname COLLATE NOCASE)
);
```

The `UNIQUE` constraint stops one person taking two desks on the same day. The
5-desk capacity is enforced in `book()` inside a `BEGIN IMMEDIATE` transaction,
so two people claiming the last desk at the same moment cannot both succeed.

An older version of this app numbered the desks. If `bookings.db` still has the
old `workspace` column, `init_db()` migrates it on startup: desk numbers are
dropped, and a surname holding two desks on one day collapses to a single row.

## Public holidays

Holidays come from [Nager.Date](https://date.nager.at) — free, no API key, no
rate limit. The app calls `GET /api/v3/PublicHolidays/{year}/{country}` and
caches the result in the `holidays` table, so the service is contacted at most
once per year of calendar you actually look at. `v3` is used rather than the
newer `v4` because only `v3` returns `localName`, the Russian name.

If the service is unreachable the calendar renders normally without holidays,
and the failure is recorded in `holiday_sync` so it is retried at most every
6 hours rather than on every page load. Cached years are refreshed after 30 days.

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOLIDAY_COUNTRY` | `RU` | ISO country code to fetch |
| `HOLIDAY_API` | Nager.Date v3 URL | Override the endpoint (`{year}`, `{country}`) |
| `HOLIDAY_SYNC` | `on` | Set to `off` to disable all outbound calls |
| `HOLIDAY_TIMEOUT` | `5` | Seconds to wait for the service |

To refresh early, delete the cached year: `sqlite3 bookings.db "DELETE FROM
holiday_sync WHERE year = 2026"`.

## Notes

There is no login, and cancelling takes one click, so anyone with the page open
can release anyone's desk. That matches a whiteboard in the hallway — it is
convenience, not access control, and there is no undo. If this needs to be
tamper-proof, put it behind SSO or add per-user accounts.

For anything beyond a single office, run it under a production WSGI server
rather than the Flask dev server, and set a real `SECRET_KEY` — see
[Docker](#docker), which does both.
