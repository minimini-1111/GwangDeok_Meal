"""
학교 급식 만족도 홈페이지 (Flask + SQLite)

실행:
    pip install -r requirements.txt
    python app.py seed      # (선택) 예시 데이터 넣기
    python app.py           # http://127.0.0.1:5000 접속

환경변수:
    ADMIN_PASSWORD  급식 등록용 관리자 비밀번호 (기본값 admin1234 -> 꼭 바꾸세요)
    SECRET_KEY      세션 암호화 키
"""
import hmac
import json
import os
import re
import random
import sqlite3
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, g, make_response, redirect,
                   render_template, request, session, url_for)
from jinja2 import DictLoader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "meal.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")
WEEKDAYS = "월화수목금토일"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS menu_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    serve_date  TEXT NOT NULL,              -- YYYY-MM-DD
    name        TEXT NOT NULL,
    UNIQUE (serve_date, name)
);
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES menu_items(id),
    client_id   TEXT NOT NULL,              -- 브라우저 쿠키 (1인 1평가용)
    nickname    TEXT NOT NULL DEFAULT '익명',
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE (item_id, client_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(SCHEMA)


# --------------------------------------------------------------------------
# 브라우저 식별 쿠키 (로그인 없이 한 음식당 한 번만 평가하도록)
# --------------------------------------------------------------------------
@app.before_request
def ensure_client_id():
    g.client_id = request.cookies.get("cid")
    g.new_cid = g.client_id is None
    if g.new_cid:
        g.client_id = uuid.uuid4().hex


@app.after_request
def set_client_cookie(resp):
    if g.get("new_cid"):
        resp.set_cookie("cid", g.client_id, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="Lax")
    return resp


# --------------------------------------------------------------------------
# 유틸
# --------------------------------------------------------------------------
def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default or date.today()


def parse_month(s):
    try:
        d = datetime.strptime(s, "%Y-%m")
        return d.year, d.month
    except (TypeError, ValueError):
        t = date.today()
        return t.year, t.month


def month_shift(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


@app.template_filter("kdate")
def kdate(d):
    if isinstance(d, str):
        d = parse_date(d)
    return f"{d.month}월 {d.day}일 ({WEEKDAYS[d.weekday()]})"


@app.template_filter("stars")
def stars(avg):
    n = int(round(avg or 0))
    return "★" * n + "☆" * (5 - n)


def is_admin():
    return session.get("admin") is True


# --------------------------------------------------------------------------
# 광덕고등학교 급식표 불러오기 (NEIS 교육정보 개방포털 오픈API)
# --------------------------------------------------------------------------
SCHOOL_NAME = "광덕고등학교"
OFFICE_CODE = "F10"                          # 광주광역시교육청
NEIS_KEY = os.environ.get("NEIS_KEY", "")    # open.neis.go.kr 에서 발급 (없어도 동작은 하지만 제한적)
_school_code = os.environ.get("SCHOOL_CODE", "")
_synced_months = set()


def neis_get(service, **params):
    params.update({"Type": "json", "pIndex": 1, "pSize": 1000})
    if NEIS_KEY:
        params["KEY"] = NEIS_KEY
    url = f"https://open.neis.go.kr/hub/{service}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    if service in data:                      # 정상: [head, row]
        return data[service][1]["row"]
    return []                                # 데이터 없음 / 오류


def get_school_code():
    global _school_code
    if not _school_code:
        rows = neis_get("schoolInfo", ATPT_OFCDC_SC_CODE=OFFICE_CODE, SCHUL_NM=SCHOOL_NAME)
        rows = [r for r in rows if r["SCHUL_NM"] == SCHOOL_NAME]
        if not rows:
            raise RuntimeError(f"{SCHOOL_NAME}를 NEIS에서 찾지 못했어요.")
        _school_code = rows[0]["SD_SCHUL_CODE"]
    return _school_code


def parse_dishes(text):
    """'잡곡밥 (5.6)<br/>김치찌개 (9.13)' -> ['잡곡밥', '김치찌개']"""
    names = []
    for part in re.split(r"<br\s*/?>", text):
        name = re.sub(r"\s*\([\d\.\s]*\)", "", part)   # 알레르기 번호 제거
        name = re.sub(r"[*★☆]+", "", name).strip()
        if name:
            names.append(name)
    return names


def sync_month(year, month, db_path=None):
    """해당 월의 중식 메뉴를 NEIS에서 받아 DB에 넣고, 넣은 메뉴 수를 돌려준다."""
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    rows = neis_get(
        "mealServiceDietInfo",
        ATPT_OFCDC_SC_CODE=OFFICE_CODE, SD_SCHUL_CODE=get_school_code(),
        MLSV_FROM_YMD=f"{year:04d}{month:02d}01", MLSV_TO_YMD=f"{year:04d}{month:02d}{last:02d}",
    )
    count = 0
    with sqlite3.connect(db_path or DB_PATH) as con:
        for r in rows:
            if r.get("MMEAL_SC_CODE") != "2":        # 2 = 중식 (1 조식, 3 석식)
                continue
            ymd = r["MLSV_YMD"]
            day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            for name in parse_dishes(r["DDISH_NM"]):
                cur = con.execute("INSERT OR IGNORE INTO menu_items (serve_date, name) VALUES (?, ?)",
                                  (day, name[:30]))
                count += cur.rowcount
    return count


def ensure_month_synced(day):
    """그 달 급식표를 한 번도 안 불러왔으면 자동으로 불러온다 (실패해도 조용히 넘어감)."""
    key = (day.year, day.month)
    if key in _synced_months:
        return
    _synced_months.add(key)
    try:
        sync_month(*key)
    except Exception as e:                        # 인터넷 문제 등
        print("급식표 자동 불러오기 실패:", e)


# --------------------------------------------------------------------------
# 일별 급식 + 평가
# --------------------------------------------------------------------------
@app.route("/")
def index():
    day = parse_date(request.args.get("date"))
    ensure_month_synced(day)
    db = get_db()

    items = db.execute(
        """
        SELECT m.id, m.name, COUNT(r.id) AS cnt, AVG(r.rating) AS avg
        FROM menu_items m LEFT JOIN reviews r ON r.item_id = m.id
        WHERE m.serve_date = ?
        GROUP BY m.id ORDER BY m.id
        """,
        (day.isoformat(),),
    ).fetchall()

    ids = [i["id"] for i in items]
    reviews, mine = {i: [] for i in ids}, {}
    if ids:
        q = ",".join("?" * len(ids))
        rows = db.execute(
            f"SELECT * FROM reviews WHERE item_id IN ({q}) ORDER BY id DESC", ids
        ).fetchall()
        for r in rows:
            if r["client_id"] == g.client_id:
                mine[r["item_id"]] = r
            if r["comment"] and len(reviews[r["item_id"]]) < 5:
                reviews[r["item_id"]].append(r)

    return render_template(
        "index.html", day=day, items=items, reviews=reviews, mine=mine,
        prev_day=(day - timedelta(days=1)).isoformat(),
        next_day=(day + timedelta(days=1)).isoformat(),
        today=date.today().isoformat(),
    )


@app.post("/review")
def post_review():
    item_id = request.form.get("item_id", type=int)
    rating = request.form.get("rating", type=int)
    comment = (request.form.get("comment") or "").strip()[:200]
    nickname = (request.form.get("nickname") or "").strip()[:12] or "익명"
    back = request.form.get("back") or url_for("index")

    db = get_db()
    item = db.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    if not item or rating not in (1, 2, 3, 4, 5):
        flash("별점을 선택해 주세요.")
        return redirect(back)

    # 같은 브라우저에서 다시 제출하면 기존 평가를 수정
    db.execute(
        """
        INSERT INTO reviews (item_id, client_id, nickname, rating, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (item_id, client_id) DO UPDATE SET
            nickname = excluded.nickname, rating = excluded.rating,
            comment = excluded.comment, created_at = excluded.created_at
        """,
        (item_id, g.client_id, nickname, rating, comment,
         datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    flash(f"'{item['name']}' 평가가 저장되었어요.")
    return redirect(back + f"#item-{item_id}")


# --------------------------------------------------------------------------
# 월별 인기 메뉴
# --------------------------------------------------------------------------
@app.route("/monthly")
def monthly():
    year, month = parse_month(request.args.get("month"))
    min_reviews = max(1, request.args.get("min", 3, type=int))
    prefix = f"{year:04d}-{month:02d}-%"
    db = get_db()

    # 같은 이름의 음식은 한 달 동안 합산 (예: 여러 날 나온 '김치볶음밥')
    rows = db.execute(
        """
        SELECT m.name,
               COUNT(r.id)                  AS cnt,
               AVG(r.rating)                AS avg,
               COUNT(DISTINCT m.serve_date) AS days
        FROM menu_items m JOIN reviews r ON r.item_id = m.id
        WHERE m.serve_date LIKE ?
        GROUP BY m.name
        HAVING COUNT(r.id) >= ?
        """,
        (prefix, min_reviews),
    ).fetchall()

    by_score = sorted(rows, key=lambda r: (-r["avg"], -r["cnt"]))[:10]
    by_count = sorted(rows, key=lambda r: (-r["cnt"], -r["avg"]))[:5]
    low = sorted(rows, key=lambda r: (r["avg"], -r["cnt"]))[:3]

    py, pm = month_shift(year, month, -1)
    ny, nm = month_shift(year, month, 1)
    return render_template(
        "monthly.html", year=year, month=month, min_reviews=min_reviews,
        by_score=by_score, by_count=by_count, low=low,
        prev_month=f"{py:04d}-{pm:02d}", next_month=f"{ny:04d}-{nm:02d}",
    )


# --------------------------------------------------------------------------
# 관리자: 급식 메뉴 등록
# --------------------------------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and not is_admin():
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("비밀번호가 맞지 않아요.")
        return redirect(url_for("admin"))

    if not is_admin():
        return render_template("admin.html", logged_in=False)

    day = parse_date(request.args.get("date"))
    items = get_db().execute(
        "SELECT id, name FROM menu_items WHERE serve_date = ? ORDER BY id",
        (day.isoformat(),),
    ).fetchall()
    return render_template("admin.html", logged_in=True, day=day, items=items)


@app.post("/admin/add")
def admin_add():
    if not is_admin():
        abort(403)
    day = parse_date(request.form.get("date"))
    text = (request.form.get("foods") or "").replace(",", "\n")
    names = [n.strip() for n in text.splitlines() if n.strip()]
    db = get_db()
    for n in names:
        db.execute("INSERT OR IGNORE INTO menu_items (serve_date, name) VALUES (?, ?)",
                   (day.isoformat(), n[:30]))
    db.commit()
    flash(f"{len(names)}개 메뉴를 등록했어요.")
    return redirect(url_for("admin", date=day.isoformat()))


@app.post("/admin/delete/<int:item_id>")
def admin_delete(item_id):
    if not is_admin():
        abort(403)
    db = get_db()
    row = db.execute("SELECT serve_date FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    db.execute("DELETE FROM reviews WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    db.commit()
    return redirect(url_for("admin", date=row["serve_date"] if row else None))


@app.post("/admin/sync")
def admin_sync():
    if not is_admin():
        abort(403)
    day = parse_date(request.form.get("date"))
    try:
        n = sync_month(day.year, day.month)
        flash(f"{day.month}월 급식표에서 새 메뉴 {n}개를 불러왔어요.")
    except Exception as e:
        flash(f"불러오기에 실패했어요: {e}")
    return redirect(url_for("admin", date=day.isoformat()))


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# 템플릿 (한 파일로 보관)
# --------------------------------------------------------------------------
BASE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}오늘 급식 어땠어?{% endblock %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jua&family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#EAF1EE; --paper:#FFFFFF; --ink:#18302B; --muted:#5E7670;
  --line:#CFDDD8; --tray:#2E6F5E; --tray-dark:#1F4F42; --star:#F4A623; --star-off:#CBD6D2;
  --warn:#B4452F;
}
*{box-sizing:border-box}
html{scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:'Noto Sans KR',system-ui,sans-serif;line-height:1.6;
  padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
a{color:inherit}
.wrap{max-width:760px;margin:0 auto;padding:0 18px 60px}
header.top{background:var(--tray);color:#fff}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;padding-top:14px;padding-bottom:14px}
.logo{font-family:'Jua',sans-serif;font-size:1.5rem;text-decoration:none;letter-spacing:.01em}
nav a{display:inline-block;margin-left:6px;padding:6px 14px;border-radius:99px;text-decoration:none;font-weight:500;font-size:.95rem}
nav a.on{background:#fff;color:var(--tray-dark)}
nav a:not(.on):hover{background:rgba(255,255,255,.18)}
h1{font-family:'Jua',sans-serif;font-weight:400;font-size:2rem;margin:28px 0 4px}
.sub{color:var(--muted);margin:0 0 20px}
.flash{background:#fff;border-left:5px solid var(--tray);padding:10px 14px;border-radius:6px;margin:16px 0;font-weight:500}
.datebar{display:flex;align-items:center;gap:8px;margin:8px 0 22px;flex-wrap:wrap}
.btn,button{font:inherit;cursor:pointer;border:0;border-radius:8px;padding:8px 16px;background:var(--tray);color:#fff;font-weight:500;text-decoration:none;display:inline-block}
.btn:hover,button:hover{background:var(--tray-dark)}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.btn.ghost:hover{background:#F3F8F6}
:focus-visible{outline:3px solid var(--star);outline-offset:2px}
input[type=text],input[type=password],input[type=date],textarea{font:inherit;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:#fff;color:var(--ink);max-width:100%}
textarea{width:100%;resize:vertical}

/* 식판: 음식마다 한 칸 */
.tray{background:var(--paper);border:2px solid var(--tray);border-radius:20px;padding:6px;display:grid;gap:6px}
.cell{background:#F6FAF8;border-radius:14px;padding:16px 18px}
.cell h2{font-family:'Jua',sans-serif;font-weight:400;font-size:1.35rem;margin:0}
.avg{display:flex;align-items:baseline;gap:8px;margin:2px 0 10px;color:var(--muted);font-size:.92rem}
.avg .st{color:var(--star);font-size:1.15rem;letter-spacing:1px}
.avg b{color:var(--ink);font-size:1.1rem}
.rate-form{display:grid;gap:8px}
.rate-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.stars{display:inline-flex;flex-direction:row-reverse}
.stars input{position:absolute;opacity:0;width:1px;height:1px}
.stars label{font-size:2rem;line-height:1;color:var(--star-off);cursor:pointer;padding:0 2px}
.stars input:checked ~ label,.stars label:hover,.stars label:hover ~ label{color:var(--star)}
.stars input:focus-visible + label{outline:3px solid var(--star);border-radius:6px}
.rate-form .nick{width:110px}
.rate-form .cmt{flex:1;min-width:180px}
.mine{font-size:.85rem;color:var(--tray-dark);font-weight:500}
.cmts{list-style:none;margin:12px 0 0;padding:0;border-top:1px dashed var(--line)}
.cmts li{padding:8px 0;border-bottom:1px dashed var(--line);font-size:.93rem}
.cmts .who{color:var(--muted);font-size:.82rem;margin-right:6px}
.cmts .s{color:var(--star)}
.empty{background:#fff;border:2px dashed var(--line);border-radius:16px;padding:36px 20px;text-align:center;color:var(--muted)}

/* 월별 */
.winner{background:var(--tray);color:#fff;border-radius:20px;padding:26px 24px;margin:6px 0 24px}
.winner .crown{font-size:.95rem;opacity:.85}
.winner .nm{font-family:'Jua',sans-serif;font-size:2.6rem;line-height:1.15;margin:2px 0 6px}
.winner .st{color:var(--star);font-size:1.4rem;letter-spacing:2px}
.winner .meta{opacity:.9}
h3{font-family:'Jua',sans-serif;font-weight:400;font-size:1.3rem;margin:30px 0 10px}
.rank{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.rank li{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;background:#fff;border-radius:12px;padding:10px 14px}
.rank .n{font-family:'Jua',sans-serif;font-size:1.3rem;color:var(--tray)}
.rank .bar{height:6px;background:var(--star-off);border-radius:9px;margin-top:4px;overflow:hidden}
.rank .bar i{display:block;height:100%;background:var(--star)}
.rank .val{text-align:right;white-space:nowrap}
.rank .val small{display:block;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden}
td,th{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left}
.scroll{overflow-x:auto}
.danger{background:var(--warn)}
.danger:hover{background:#8E3321}
@media (prefers-reduced-motion:no-preference){ .stars label{transition:color .12s} }
@media (max-width:520px){ h1{font-size:1.65rem} .winner .nm{font-size:2.1rem} nav a{padding:6px 10px} }
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="{{ url_for('index') }}">광덕고 급식 어땠어?</a>
  <nav>
    <a href="{{ url_for('index') }}" class="{{ 'on' if request.endpoint=='index' }}">오늘의 급식</a>
    <a href="{{ url_for('monthly') }}" class="{{ 'on' if request.endpoint=='monthly' }}">이달의 인기 메뉴</a>
  </nav>
</div></header>
<main class="wrap">
  {% for m in get_flashed_messages() %}<div class="flash" role="status">{{ m }}</div>{% endfor %}
  {% block body %}{% endblock %}
</main>
</body>
</html>"""

INDEX = """{% extends "base.html" %}
{% block body %}
<h1>{{ day|kdate }} 급식</h1>
<p class="sub">먹은 음식에 별점과 한마디를 남겨 주세요. 다시 제출하면 내 평가가 수정돼요.</p>

<div class="datebar">
  <a class="btn ghost" href="{{ url_for('index', date=prev_day) }}" aria-label="전날">‹ 전날</a>
  <form method="get" action="{{ url_for('index') }}">
    <input type="date" name="date" value="{{ day.isoformat() }}" onchange="this.form.submit()" aria-label="날짜 선택">
  </form>
  <a class="btn ghost" href="{{ url_for('index', date=next_day) }}" aria-label="다음날">다음날 ›</a>
  {% if day.isoformat() != today %}<a class="btn ghost" href="{{ url_for('index') }}">오늘</a>{% endif %}
</div>

{% if not items %}
  <div class="empty">이 날은 등록된 급식이 없어요.<br>
    <a href="{{ url_for('admin', date=day.isoformat()) }}">관리자 페이지</a>에서 메뉴를 등록할 수 있어요.</div>
{% else %}
<div class="tray">
  {% for it in items %}
  {% set my = mine.get(it.id) %}
  <section class="cell" id="item-{{ it.id }}">
    <h2>{{ it.name }}</h2>
    <div class="avg">
      {% if it.cnt %}<span class="st">{{ it.avg|stars }}</span><b>{{ '%.1f'|format(it.avg) }}</b><span>{{ it.cnt }}명 평가</span>
      {% else %}<span>아직 평가가 없어요. 첫 평가를 남겨 주세요!</span>{% endif %}
    </div>

    <form class="rate-form" method="post" action="{{ url_for('post_review') }}">
      <input type="hidden" name="item_id" value="{{ it.id }}">
      <input type="hidden" name="back" value="{{ url_for('index', date=day.isoformat()) }}">
      <div class="rate-row">
        <div class="stars" role="radiogroup" aria-label="{{ it.name }} 별점">
          {% for n in [5,4,3,2,1] %}
          <input type="radio" name="rating" id="r{{ it.id }}-{{ n }}" value="{{ n }}" {{ 'checked' if my and my.rating==n }} required>
          <label for="r{{ it.id }}-{{ n }}" title="{{ n }}점">★<span style="position:absolute;left:-9999px">{{ n }}점</span></label>
          {% endfor %}
        </div>
        {% if my %}<span class="mine">내 평가 {{ my.rating }}점</span>{% endif %}
      </div>
      <div class="rate-row">
        <input class="nick" type="text" name="nickname" maxlength="12" placeholder="닉네임" value="{{ my.nickname if my and my.nickname != '익명' }}">
        <input class="cmt" type="text" name="comment" maxlength="200" placeholder="한줄평 (선택)" value="{{ my.comment if my }}">
        <button type="submit">{{ '수정' if my else '평가하기' }}</button>
      </div>
    </form>

    {% if reviews[it.id] %}
    <ul class="cmts">
      {% for r in reviews[it.id] %}
      <li><span class="s">{{ r.rating|stars }}</span> <span class="who">{{ r.nickname }}</span>{{ r.comment }}</li>
      {% endfor %}
    </ul>
    {% endif %}
  </section>
  {% endfor %}
</div>
{% endif %}
{% endblock %}"""

MONTHLY = """{% extends "base.html" %}
{% block body %}
<h1>{{ year }}년 {{ month }}월 인기 메뉴</h1>
<p class="sub">평가가 {{ min_reviews }}개 이상 모인 메뉴만 순위에 들어가요. 같은 메뉴가 여러 번 나오면 평가를 합쳐서 계산해요.</p>

<div class="datebar">
  <a class="btn ghost" href="{{ url_for('monthly', month=prev_month, min=min_reviews) }}">‹ 이전 달</a>
  <a class="btn ghost" href="{{ url_for('monthly', month=next_month, min=min_reviews) }}">다음 달 ›</a>
  <a class="btn ghost" href="{{ url_for('monthly') }}">이번 달</a>
</div>

{% if not by_score %}
  <div class="empty">아직 순위를 낼 만큼 평가가 모이지 않았어요.</div>
{% else %}
  {% set top = by_score[0] %}
  <div class="winner">
    <div class="crown">이달의 1위</div>
    <div class="nm">{{ top.name }}</div>
    <div><span class="st">{{ top.avg|stars }}</span> <b>{{ '%.2f'|format(top.avg) }}</b></div>
    <div class="meta">{{ top.cnt }}명 평가 · {{ top.days }}번 나온 메뉴</div>
  </div>

  <h3>평점 순위 TOP {{ by_score|length }}</h3>
  <ol class="rank">
    {% for r in by_score %}
    <li><span class="n">{{ loop.index }}</span>
      <div>{{ r.name }}<div class="bar"><i style="width:{{ (r.avg/5*100)|round(0) }}%"></i></div></div>
      <div class="val"><b>{{ '%.2f'|format(r.avg) }}</b><small>{{ r.cnt }}명</small></div></li>
    {% endfor %}
  </ol>

  <h3>가장 많이 평가된 메뉴</h3>
  <div class="scroll"><table>
    <tr><th>메뉴</th><th>평가 수</th><th>평점</th></tr>
    {% for r in by_count %}<tr><td>{{ r.name }}</td><td>{{ r.cnt }}명</td><td>{{ '%.2f'|format(r.avg) }}</td></tr>{% endfor %}
  </table></div>

  {% if low %}
  <h3>아쉬웠던 메뉴</h3>
  <div class="scroll"><table>
    <tr><th>메뉴</th><th>평가 수</th><th>평점</th></tr>
    {% for r in low %}<tr><td>{{ r.name }}</td><td>{{ r.cnt }}명</td><td>{{ '%.2f'|format(r.avg) }}</td></tr>{% endfor %}
  </table></div>
  {% endif %}
{% endif %}
{% endblock %}"""

ADMIN = """{% extends "base.html" %}
{% block body %}
<h1>급식 메뉴 관리</h1>
{% if not logged_in %}
  <p class="sub">급식 메뉴는 관리자만 등록할 수 있어요.</p>
  <form method="post" class="datebar">
    <input type="password" name="password" placeholder="관리자 비밀번호" required autofocus>
    <button type="submit">로그인</button>
  </form>
{% else %}
  <form method="post" action="{{ url_for('admin_sync') }}" class="datebar">
    <input type="hidden" name="date" value="{{ day.isoformat() }}">
    <button type="submit">{{ day.month }}월 광덕고 급식표 불러오기</button>
  </form>
  <p class="sub">날짜를 고르고 그날의 메뉴를 한 줄에 하나씩(또는 쉼표로) 적어 주세요.</p>
  <form method="post" action="{{ url_for('admin_add') }}">
    <div class="datebar"><input type="date" name="date" value="{{ day.isoformat() }}" required></div>
    <textarea name="foods" rows="6" placeholder="잡곡밥&#10;김치찌개&#10;제육볶음&#10;계란말이&#10;배추김치" required></textarea>
    <p><button type="submit">메뉴 등록</button></p>
  </form>

  <h3>{{ day|kdate }} 등록된 메뉴</h3>
  {% if items %}
  <div class="scroll"><table>
    {% for it in items %}
    <tr><td>{{ it.name }}</td><td style="text-align:right">
      <form method="post" action="{{ url_for('admin_delete', item_id=it.id) }}" onsubmit="return confirm('{{ it.name }} 메뉴와 평가를 모두 삭제할까요?')">
        <button class="danger" type="submit">삭제</button></form></td></tr>
    {% endfor %}
  </table></div>
  {% else %}<div class="empty">이 날짜에는 아직 메뉴가 없어요.</div>{% endif %}

  <p style="margin-top:28px"><form method="post" action="{{ url_for('admin_logout') }}"><button class="ghost btn" type="submit">로그아웃</button></form></p>
{% endif %}
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "index.html": INDEX,
    "monthly.html": MONTHLY, "admin.html": ADMIN,
})


# --------------------------------------------------------------------------
# 예시 데이터
# --------------------------------------------------------------------------
def seed_demo():
    menus = [
        ["잡곡밥", "김치찌개", "제육볶음", "계란말이", "배추김치"],
        ["흰쌀밥", "미역국", "돈까스", "양배추샐러드", "깍두기"],
        ["김치볶음밥", "콩나물국", "탕수육", "단무지", "요구르트"],
        ["카레라이스", "된장국", "치킨너겟", "감자샐러드", "배추김치"],
        ["비빔밥", "어묵국", "떡갈비", "과일", "깍두기"],
    ]
    comments = ["맛있어요!", "그저 그래요", "또 먹고 싶어요", "조금 짰어요", "최고", ""]
    init_db()
    today = date.today()
    first = today.replace(day=1)
    with sqlite3.connect(DB_PATH) as con:
        d, k = first, 0
        while d <= today + timedelta(days=2):
            if d.weekday() < 5:
                for name in menus[k % len(menus)]:
                    con.execute("INSERT OR IGNORE INTO menu_items (serve_date, name) VALUES (?,?)",
                                (d.isoformat(), name))
                k += 1
            d += timedelta(days=1)
        for (item_id,) in con.execute("SELECT id FROM menu_items").fetchall():
            for _ in range(random.randint(2, 6)):
                con.execute(
                    "INSERT OR IGNORE INTO reviews (item_id, client_id, nickname, rating, comment, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (item_id, uuid.uuid4().hex, random.choice(["익명", "민지", "준호", "서연"]),
                     random.choice([2, 3, 3, 4, 4, 5, 5]), random.choice(comments),
                     datetime.now().isoformat(timespec="seconds")))
    print("예시 데이터를 넣었어요.")


init_db()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed_demo()
    elif len(sys.argv) > 1 and sys.argv[1] == "sync":
        y, m = parse_month(sys.argv[2] if len(sys.argv) > 2 else None)
        print(f"{y}년 {m}월: 새 메뉴 {sync_month(y, m)}개를 불러왔어요.")
    else:
        app.run(host="0.0.0.0", port=5000, debug=True)
