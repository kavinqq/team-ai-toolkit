#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本機週報服務:餵靜態檔 + 一支「重抓」端點。

為什麼需要它:週報是產出當下的靜態快照,`python -m http.server` 沒辦法自己重跑
build.py。這支服務只在本機跑,所以能直接呼叫 build.py 重產再讓頁面 reload。

用法:
  python3 serve.py                 # 預設 127.0.0.1:8099,餵 out/
  python3 serve.py --port 8100
  python3 serve.py --cooldown 60   # 改冷卻秒數(預設 30)

端點:
  GET  /api/rebuild -> {"available":true,"running":bool,"cooldown":剩餘秒數,"generated":"..."}
  POST /api/rebuild -> 重跑 build.py;冷卻中回 429 並附剩餘秒數
  GET  /api/order   -> {"order": {人名: {"week": 該週週一, "items": [需求名, ...]}}, "editable": bool}
                       個人頁頂端的「項目順序」;editable = 這次請求帶的 X-Order-Key 對不對
  PUT  /api/order   <- {"person": 人名, "week": "YYYY-MM-DD"(本週或下週的週一), "items": [...]}
                       items 空陣列 = 清掉這個人的順序。**必須帶 X-Order-Key**(組長專用)
  組長專區(全部要帶 X-Order-Key):
  GET    /api/admin/people           -> 名單(顯示名稱 / PMO / Taiga)、Notion 週報連結
  POST   /api/admin/people           <- {pmo, taiga, display}:驗證 PMO、Taiga 都找得到才加,加完重產週報
  DELETE /api/admin/people           <- {pmo}:移除(組長本人不能刪),移除後重產週報
  PUT    /api/admin/people/order     <- {people: [pmo, ...]}:側欄與 Notion 週報的排序
  PUT    /api/admin/settings         <- {notion_url}
  POST   /api/admin/notion           <- {start, end}:產生 Notion 週報的表格資料(頁面上預覽、複製貼到 Notion)

冷卻**做在 server 端**:按下重抓後頁面會 reload,前端計時器會歸零,
只做前端的話重整幾次就繞過去了。多開分頁同理。
"""
import argparse, hmac, json, os, re, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build.py")
OUT_DIR = os.path.join(HERE, "out")
OUT_FILE = os.path.join(OUT_DIR, "pmo-weekly.html")

# ---------- 項目順序(組長在頁面上手填:某人要依序做完哪些需求) ----------
# 存在 people.json 旁邊,build.py 產頁時也會嵌一份進去(靜態開啟時照樣看得到)。
# 只有組長能改(2026-10-11 使用者要求):寫入要帶 X-Order-Key,值等於環境變數 ORDER_KEY
# (放在 etc/pmo.env,由 systemd drop-in 載入)。組長用 ?leader=<key> 開一次頁面,
# 瀏覽器記在 localStorage,之後自動帶上。沒帶或帶錯一律 403;server 沒設 ORDER_KEY 則整個不給寫。
# 看(GET)不用鑰匙 —— 順序本來就是要給組員看的。
# 只收名單上的人、最多 20 項、每項 100 字,避免被塞爆或拿來存別的東西。
# 每筆順序綁一週(week = 那週的週一),只能設本週或下週;週一一過,上週的就自動失效
# (讀的時候濾掉、寫的時候順手清掉),不必有人回來刪(2026-10-11 使用者要求)。
ORDER_FILE = os.path.join(os.path.dirname(HERE), "order.json")
PEOPLE_FILE = os.path.join(HERE, "people.json")
ORDER_MAX_ITEMS, ORDER_MAX_LEN = 20, 100
_order_lock = threading.Lock()


def this_monday():
    t = time.localtime()
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(t) - t.tm_wday * 86400))


def next_monday():
    t = time.localtime()
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(t) + (7 - t.tm_wday) * 86400))


def order_key():
    return (os.environ.get("ORDER_KEY") or "").strip()


def order_key_ok(sent):
    k = order_key()
    return bool(k) and isinstance(sent, str) and hmac.compare_digest(sent.encode(), k.encode())


def load_order():
    """只回還沒過期的(week >= 本週一)。格式不對的(例:早期沒綁週的陣列)一律當過期。"""
    try:
        with open(ORDER_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    mon = this_monday()
    return {k: v for k, v in d.items()
            if isinstance(v, dict) and isinstance(v.get("week"), str) and v["week"] >= mon
            and isinstance(v.get("items"), list)}


def roster():
    try:
        with open(PEOPLE_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("people") or [])
    except (OSError, ValueError):
        return set()


def save_order(person, week, items):
    """整份讀出來改一個人再寫回(過期的順手清掉);先寫暫存檔再 rename,寫到一半當掉也不會留下半個 JSON。"""
    with _order_lock:
        d = load_order()
        if items:
            d[person] = {"week": week, "items": items}
        else:
            d.pop(person, None)
        tmp = ORDER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ORDER_FILE)
        return d

# ---------- PMO 轉發(頁面上改狀態用) ----------
# 為什麼要在這裡轉一手,而不是讓瀏覽器直接打 PMO:PMO production 的 CORS 只放行
# localhost/127.0.0.1(services/api/src/index.ts 的 resolveCorsOrigin)。要讓
# https://tools.example.com 直接打,得去改 PMO 正式機的 CORS_ORIGIN —— 那是別人的
# production,而且是全域放寬。轉一手則 PMO 零改動。
#
# 【安全邊界】這段是整支服務唯一會碰到別人憑證的地方,改之前把這四條讀完:
#
#   1. 金鑰是伺服器的 PMO_API_KEY(mr-sync 的機器人 sync_bot,manager、不過期),
#      **瀏覽器不碰金鑰**,送來的 X-PMO-Key 一律不理。2026-10-02 使用者決定:
#      大家在這頁統一用機器人的名義改,不要每個人去 PMO 建自己的金鑰;真要以本人名義改就去 PMO。
#      代價是打得到這台的人都能改狀態,而且 PMO 異動紀錄只會看到機器人。
#      金鑰不進 log、不放進例外訊息、不回給前端。
#   2. 只放行 PMO_ROUTES 那三條,**不是通用 proxy**。少了這層,任何打得到這台的人
#      都能拿它當跳板打 PMO 全部 66 支端點 —— 而且是帶著 manager 金鑰的完整權限。
#      跨站的頁面也打不進來:PATCH + JSON 會觸發 CORS preflight,這裡不回 OPTIONS。
#   3. PATCH 只讓 PATCH_FIELDS 那三個欄位過去。即使有人換掉這頁的 HTML 餵惡意 JS,
#      能做的上限也只是改狀態:改不了名稱、負責人、日期,刪不掉卡。
#      預計完成日 2026-10-02 起不開放(使用者決定:排程只由他在 PMO 排)。
#      這是縱深防禦 —— PMO 後端本身是允許那些操作的(見 routes/tasks.ts)。
#   4. PMO 的狀態碼與錯誤訊息**原樣轉回**前端。不要在這裡「美化」錯誤:
#      「你不是本專案成員」「Cannot change dates of a task that has child tasks」
#      這些是 PMO 講的實話,改寫只會讓使用者看到我們猜的版本。
PMO_BASE = os.environ.get("PMO_BASE", "https://pmo.example.com")
PMO_TIMEOUT = 30
PATCH_FIELDS = {"status", "projectStatusId"}
_ID = r"[A-Za-z0-9_-]{1,64}"
# `reorder` / `kanban-reorder` 是 tasks/ 底下的**兄弟端點**,不是任務 id,但它們的字面
# 完全符合 _ID 的形狀 —— 不排除的話 PATCH .../tasks/reorder 會被當成「改某張卡」放行。
# PMO 自己在 lib/auth.ts 的 isContributorTaskRoute() 也做了同一件事,同樣是明確列名排除。
_NOT_TASK_ID = r"(?!reorder$|kanban-reorder$)"
PMO_ROUTES = (
    ("GET", re.compile(r"^projects/%s/statuses$" % _ID)),
    ("GET", re.compile(r"^projects/%s/tasks$" % _ID)),
    ("PATCH", re.compile(r"^projects/%s/tasks/%s%s$" % (_ID, _NOT_TASK_ID, _ID))),
)


def server_key():
    return (os.environ.get("PMO_API_KEY") or "").strip()


def pmo_allowed(method, rest):
    return any(m == method and rx.match(rest) for m, rx in PMO_ROUTES)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟隨轉址。

    urllib 預設會自動跟隨 3xx,**而且會把原本的 Authorization 標頭一起帶到新位址**。
    只要有人能讓 PMO(或冒充 PMO 的中間人)回一個 302,使用者的金鑰就被送到那個位址去了。
    這裡讓 redirect_request 回 None,urllib 就會把 3xx 當成 HTTPError 丟出來 ——
    於是轉址原樣回給前端變成一個看得見的錯誤,而不是一次看不見的憑證外洩。
    """

    def redirect_request(self, *a, **kw):
        return None


_PMO_OPENER = urllib.request.build_opener(_NoRedirect)


# ---------- Taiga 轉發(頁面上改 Taiga 卡狀態用) ----------
# 帳號是 mr-sync 的機器人(TAIGA_HOOK_USER / TAIGA_HOOK_PASSWORD),跟 PMO 那把同一個決定。
# 名稱刻意不叫 TAIGA_USERNAME:build.py 讀 Taiga 用的是使用者本人的帳號(假 security),
# 兩組混在一起就分不清報表是誰的視角、改卡又是誰的名義。
# 跟 PMO 不同,這裡**不轉發路徑**:只有 /api/taiga/item 一支,由伺服器自己組 Taiga 的 URL,
# 可寫欄位只有 status,version 由伺服器當下讀(Taiga 的樂觀鎖,前端帶來的一律不用)。
TAIGA_BASE = os.environ.get("TAIGA_BASE", "https://taiga.example.com") + "/api/v1"
TAIGA_CARD = re.compile(r"^https?://[^/]+/project/([a-z0-9][a-z0-9-]{0,99})/(us|task|issue|epic)/(\d{1,7})(?:[?#]|$)", re.ASCII)
TAIGA_KIND = {"us": ("userstories", "userstory-statuses"), "task": ("tasks", "task-statuses"),
              "issue": ("issues", "issue-statuses"), "epic": ("epics", "epic-statuses")}
SECRET_ENV = ("PMO_API_KEY", "TAIGA_HOOK_USER", "TAIGA_HOOK_PASSWORD", "ORDER_KEY")


def taiga_creds():
    user = (os.environ.get("TAIGA_HOOK_USER") or "").strip()
    pw = os.environ.get("TAIGA_HOOK_PASSWORD") or ""
    return (user, pw) if user and pw else None


class TaigaError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def _taiga_call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(TAIGA_BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _PMO_OPENER.open(req, timeout=PMO_TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read() or b"{}")
        except ValueError:
            j = {}
        raise TaigaError(e.code, j.get("_error_message") or j.get("detail") or f"Taiga HTTP {e.code}")
    except urllib.error.URLError as e:
        raise TaigaError(502, "連不到 Taiga(%s)。是否在公司內網/VPN?" % (e.reason,))


def taiga_item(url, new_status=None):
    """讀(new_status=None)或改一張 Taiga 卡的狀態。url 是卡片網址,前提是已通過 TAIGA_CARD。"""
    slug, kind, ref = TAIGA_CARD.match(url).groups()
    items, statuses = TAIGA_KIND[kind]
    user, pw = taiga_creds()
    token = _taiga_call("POST", "/auth", body={"type": "normal", "username": user, "password": pw})["auth_token"]
    pid = _taiga_call("GET", f"/projects/by_slug?slug={slug}", token)["id"]
    card = _taiga_call("GET", f"/{items}/by_ref?project={pid}&ref={ref}", token)
    if new_status is None:
        sts = sorted(_taiga_call("GET", f"/{statuses}?project={pid}", token), key=lambda s: s.get("order", 0))
        return {"status": card.get("status"),
                "statuses": [{"id": s["id"], "name": s["name"], "closed": bool(s.get("is_closed"))} for s in sts]}
    out = _taiga_call("PATCH", f"/{items}/{card['id']}", token,
                      {"status": new_status, "version": card["version"]})
    return {"status": out.get("status"), "statusName": (out.get("status_extra_info") or {}).get("name", "")}


TAIGA_HISTORY = {"us": "userstory", "task": "task", "issue": "issue", "epic": "epic"}
TAIGA_HOST = TAIGA_BASE[:-len("/api/v1")]


def _tg_status(info):
    info = info or {}
    return {"name": info.get("name") or "", "color": info.get("color") or "", "closed": bool(info.get("is_closed"))}


def _tg_child(c, url, tag):
    return {"subject": c.get("subject") or "", "status": _tg_status(c.get("status_extra_info")),
            "assignee": (c.get("assigned_to_extra_info") or {}).get("full_name_display") or "",
            "url": url, "tag": tag}


def taiga_view(url):
    """抽屜裡「Taiga 預覽」要的整包唯讀資料。url 前提是已通過 TAIGA_CARD。

    description_html / comment_html 是 Taiga 自己轉好、清過的 HTML,前端仍會再過一次白名單。
    圖片與附件網址帶 Taiga 簽發的限時 token,不登入 Taiga 也打得開(2026-10-12 實測:
    帶 token 200、拿掉 403),所以原樣交給前端,不經這裡轉。"""
    slug, kind, ref = TAIGA_CARD.match(url).groups()
    items = TAIGA_KIND[kind][0]
    user, pw = taiga_creds()
    token = _taiga_call("POST", "/auth", body={"type": "normal", "username": user, "password": pw})["auth_token"]
    proj = _taiga_call("GET", f"/projects/by_slug?slug={slug}", token)
    card = _taiga_call("GET", f"/{items}/by_ref?project={proj['id']}&ref={ref}", token)
    cid = card["id"]

    jobs = {"attachments": f"/{items}/attachments?object_id={cid}&project={proj['id']}",
            "comments": f"/history/{TAIGA_HISTORY[kind]}/{cid}?type=comment"}
    child_path = {"us": f"/tasks?user_story={cid}", "epic": f"/epics/{cid}/related_userstories"}.get(kind)
    if child_path:
        jobs["children"] = child_path
    # 彼此不相依;依序打要多等兩三輪來回,而抽屜是點了才載入,等待時間使用者看得到
    got = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {k: pool.submit(_taiga_call, "GET", p, token) for k, p in jobs.items()}
        for k, f in futs.items():
            try:
                got[k] = f.result()
            except TaigaError:
                got[k] = []

    if kind == "epic":
        children = [_tg_child(c, f"{TAIGA_HOST}/project/{(c.get('project_extra_info') or {}).get('slug') or slug}"
                                 f"/us/{c.get('ref')}", f"US{c.get('ref')}") for c in got.get("children") or []]
    else:
        children = [_tg_child(c, f"{TAIGA_HOST}/project/{slug}/task/{c.get('ref')}", f"TK{c.get('ref')}")
                    for c in got.get("children") or []]

    names = {m.get("id"): m.get("full_name_display") or m.get("username") or "" for m in proj.get("members") or []}
    a_ids = list(card.get("assigned_users") or [])
    if card.get("assigned_to") and card["assigned_to"] not in a_ids:
        a_ids.insert(0, card["assigned_to"])

    return {
        "kind": kind, "ref": card.get("ref"), "subject": card.get("subject") or "",
        "project": proj.get("name") or slug,
        "status": _tg_status(card.get("status_extra_info")),
        "assigned": [names.get(i) or f"#{i}" for i in a_ids],
        "owner": (card.get("owner_extra_info") or {}).get("full_name_display") or "",
        "created": (card.get("created_date") or "")[:10],
        "modified": (card.get("modified_date") or "")[:10],
        "due": card.get("due_date") or "",
        "tags": [t[0] if isinstance(t, list) else t for t in card.get("tags") or []],
        "description_html": card.get("description_html") or "",
        "children": children,
        "attachments": [{"name": a.get("name") or "", "size": a.get("size") or 0, "url": a.get("url") or "",
                         "thumb": a.get("thumbnail_card_url") or ""}
                        for a in got.get("attachments") or [] if not a.get("is_deprecated")],
        "comments": [{"user": (h.get("user") or {}).get("name") or "", "date": h.get("created_at") or "",
                      "html": h.get("comment_html") or ""}
                     for h in got.get("comments") or [] if h.get("comment") and not h.get("delete_comment_date")],
    }


_lock = threading.Lock()
_state = {"last": 0.0, "running": False, "cooldown": 30}


def generated_at():
    """從產出的 HTML 讀「更新於」時間,讓前端可以顯示這份資料多新。"""
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            head = f.read(200000)
        m = re.search(r'"generated"\s*:\s*"([^"]+)"', head)
        return m.group(1) if m else ""
    except OSError:
        return ""


def cooldown_left():
    left = _state["cooldown"] - (time.time() - _state["last"])
    return max(0, int(left + 0.999))


# ---------- 組長專區:人員管理 / Notion 週報 / 設定 ----------
# 名單只存在 people.json 一個地方(people 排序 + nav_names 顯示名稱 + taiga_users 帳號),
# 週報、側欄、Notion 週報都從這裡讀,不再另外維護 roster.json。
WEEKLY_DIR = HERE
NOTION_DEFAULT = "https://www.notion.so/your-weekly-report-page"   # 工作週報
_people_lock = threading.Lock()


def load_people_cfg():
    with open(PEOPLE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_people_cfg(cfg):
    tmp = PEOPLE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PEOPLE_FILE)


def people_view(cfg):
    nav, tg = cfg.get("nav_names") or {}, cfg.get("taiga_users") or {}
    return {"people": [{"pmo": p, "display": nav.get(p, p), "taiga": tg.get(p, ""),
                        "leader": p == cfg.get("leader")} for p in cfg.get("people") or []],
            "notion_url": cfg.get("notion_url") or NOTION_DEFAULT}


def _weekly():
    """weekly.py(PMO 登入、抓任務、roll-up)與 taiga.py。登入靠假 security,systemd 的 PATH 已經帶好。"""
    for d in (WEEKLY_DIR, HERE):
        if d not in sys.path:
            sys.path.insert(0, d)
    import weekly, taiga
    return weekly, taiga


def pmo_names():
    """PMO 上所有叫得出名字的人:有任務的負責人 + 各專案成員(新人可能還沒有任務)。"""
    W, _ = _weekly()
    op = W.connect()
    _, _, names = W.load_all_tasks(op)
    names = set(n for n in names if n)
    for p in W.api(op, "/api/projects"):
        try:
            names |= {m.get("name") for m in W.api(op, f"/api/projects/{p['id']}/members").get("members", []) if m.get("name")}
        except SystemExit:
            pass
    return names


def taiga_lookup(account):
    """Taiga 帳號 -> (username, 顯示名稱);找不到回 None。帳號或顯示名稱完全相同都算。"""
    _, TG = _weekly()
    t = TG.Taiga()
    if not t.connect():
        raise RuntimeError(t.reason or "Taiga 登入失敗")
    a = account.strip().lower()
    for u in t.users().values():
        if a in ((u.get("username") or "").lower(), (u.get("name") or "").lower()):
            return u["username"], u["name"]
    return None


def rebuild_now():
    """名單或設定改完立刻重產,不吃重抓鈕的冷卻(那是防一般人連按);正在重抓就回報稍後。"""
    with _lock:
        if _state["running"]:
            return False, "週報正在重抓,名單已存好,等它跑完會用新名單"
        _state["running"] = True
    try:
        return run_build()
    finally:
        with _lock:
            _state["running"] = False
            _state["last"] = time.time()


def notion_tables(start, end):
    """跟 weekly.py notion 同一套算法(時間軸交集 + 母子 roll-up),名單改從 people.json 讀。
    組長那張表沒有「預計 UAT 日期」欄(照 Notion 上的既有格式)。"""
    W, _ = _weekly()
    cfg = load_people_cfg()
    nav = cfg.get("nav_names") or {}
    op = W.connect()
    tasks, byid, names = W.load_all_tasks(op)
    today = time.strftime("%Y-%m-%d")
    out = []
    order = cfg.get("people") or []
    lead = cfg.get("leader")
    order = ([lead] if lead in order else []) + [p for p in order if p != lead]
    for p in order:
        mine = [t for t in tasks if t.get("assigneeName") == p
                and W.overlaps(t.get("startDate"), t.get("endDate"), start, end)]
        rows = [{"name": g["name"], "status": g["status"],
                 "uat": W.md_date(g["end"]) if g["status"] != "Done" and g["end"] else ""}
                for g in W.rollup(mine, byid, today)]
        rows.sort(key=lambda r: r["name"])
        out.append({"title": nav.get(p, p), "pmo": p, "uat_col": p != lead, "rows": rows})
    return out


def run_build():
    """重跑 build.py。回傳 (ok, 訊息)。"""
    try:
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        p = subprocess.run([sys.executable, BUILD], capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return False, "重抓逾時(超過 5 分鐘)——PMO 或 Taiga 可能沒回應"
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        return False, "build.py 失敗:" + (tail[-1] if tail else f"exit {p.returncode}")
    first = (p.stdout or "").strip().splitlines()
    return True, (first[1].strip() if len(first) > 1 else "已重新產生")


class Handler(SimpleHTTPRequestHandler):
    # keep-alive:cloudflared 之類的前置會重用連線,HTTP/1.0 每次回完就關,
    # 對方再送下一個請求就會拿到 EOF。所有回應都有 Content-Length,所以可以開。
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=OUT_DIR, **kw)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _pmo(self, method):
        """用伺服器的 PMO_API_KEY 把一個請求轉給 PMO。金鑰不放進 log 或任何例外訊息。"""
        rest = self.path.split("?")[0][len("/api/pmo/"):]
        if not pmo_allowed(method, rest):
            # 不說明「為什麼不合法」:白名單的形狀本身沒有對外交代的必要
            return self._json(404, {"error": "not found"})

        key = server_key()
        if not key:
            return self._json(503, {"error": "週報服務沒有設定 PMO 金鑰(PMO_API_KEY)"})

        body = None
        if method == "PATCH":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                sent = json.loads(self.rfile.read(n) if n else b"{}")
            except (ValueError, TypeError):
                return self._json(400, {"error": "request body 不是合法 JSON"})
            if not isinstance(sent, dict):
                return self._json(400, {"error": "request body 必須是 JSON 物件"})
            # 縱深防禦:只讓白名單欄位過去(見檔頭安全邊界第 3 條)
            keep = {k: v for k, v in sent.items() if k in PATCH_FIELDS}
            if not keep:
                return self._json(400, {"error": "只接受這些欄位:" + "、".join(sorted(PATCH_FIELDS))})
            body = json.dumps(keep).encode()

        req = urllib.request.Request(PMO_BASE + "/api/" + rest, data=body, method=method)
        req.add_header("Authorization", "Bearer " + key)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _PMO_OPENER.open(req, timeout=PMO_TIMEOUT) as r:
                code, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            # PMO 的狀態碼與訊息原樣轉回(安全邊界第 4 條)。它講的比我們猜的準:
            # 「你不是本專案成員」/「Cannot change dates of a task that has child tasks」
            code, payload = e.code, e.read()
        except urllib.error.URLError as e:
            return self._json(502, {"error": "連不到 PMO(%s)。是否在公司內網/VPN?" % (e.reason,)})

        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _taiga(self, method):
        if method == "GET":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            url, status = (qs.get("url") or [""])[0], None
        else:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                sent = json.loads(self.rfile.read(n) if n else b"{}")
            except (ValueError, TypeError):
                return self._json(400, {"error": "request body 不是合法 JSON"})
            if not isinstance(sent, dict):
                return self._json(400, {"error": "request body 必須是 JSON 物件"})
            url, status = sent.get("url") or "", sent.get("status")
            if not isinstance(status, int) or isinstance(status, bool):
                return self._json(400, {"error": "status 必須是 Taiga 狀態 id(整數)"})
        if not isinstance(url, str) or not TAIGA_CARD.match(url):
            return self._json(400, {"error": "不是 Taiga 卡片網址"})
        if not taiga_creds():
            return self._json(503, {"error": "週報服務沒有設定 Taiga 帳號(TAIGA_HOOK_USER / TAIGA_HOOK_PASSWORD)"})
        try:
            return self._json(200, taiga_item(url, status))
        except TaigaError as e:
            return self._json(e.code, {"error": e.msg})

    def _taiga_view(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        url = (qs.get("url") or [""])[0]
        if not TAIGA_CARD.match(url):
            return self._json(400, {"error": "不是 Taiga 卡片網址"})
        if not taiga_creds():
            return self._json(503, {"error": "週報服務沒有設定 Taiga 帳號(TAIGA_HOOK_USER / TAIGA_HOOK_PASSWORD)"})
        try:
            return self._json(200, taiga_view(url))
        except TaigaError as e:
            return self._json(e.code, {"error": e.msg})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            sent = json.loads(self.rfile.read(n) if n else b"{}")
        except (ValueError, TypeError):
            return None
        return sent if isinstance(sent, dict) else None

    def _admin(self, method):
        """組長專區。全部要帶對 X-Order-Key;錯誤訊息寫成使用者看得懂、知道怎麼改的句子。"""
        path = self.path.split("?")[0]
        if not order_key():
            return self._json(503, {"error": "週報服務沒有設定組長鑰匙(ORDER_KEY)"})
        if not order_key_ok(self.headers.get("X-Order-Key")):
            return self._json(403, {"error": "只有組長可以使用組長專區"})
        sent = {} if method == "GET" else self._body()
        if sent is None:
            return self._json(400, {"error": "request body 必須是 JSON 物件"})
        try:
            if path == "/api/admin/people" and method == "GET":
                return self._json(200, people_view(load_people_cfg()))

            if path == "/api/admin/people" and method == "POST":
                pmo = (sent.get("pmo") or "").strip()
                acct = (sent.get("taiga") or "").strip()
                disp = (sent.get("display") or "").strip() or pmo
                if not pmo or not acct:
                    return self._json(400, {"error": "PMO 名字和 Taiga 帳號都要填", "field": "pmo" if not pmo else "taiga"})
                if len(disp) > 20:
                    return self._json(400, {"error": "顯示名稱最多 20 字", "field": "display"})
                cfg = load_people_cfg()
                if pmo in (cfg.get("people") or []):
                    return self._json(400, {"error": f"「{pmo}」已經在名單裡了", "field": "pmo"})
                names = pmo_names()
                if pmo not in names:
                    near = sorted(n for n in names if pmo.lower() in n.lower() or n.lower() in pmo.lower())[:5]
                    return self._json(400, {"error": f"PMO 上找不到「{pmo}」" +
                                            (f",是不是:{'、'.join(near)}?" if near else ",名字要跟 PMO 上一模一樣"),
                                            "field": "pmo"})
                hit = taiga_lookup(acct)
                if not hit:
                    return self._json(400, {"error": f"Taiga 上找不到帳號「{acct}」(填 Taiga 的 username)", "field": "taiga"})
                with _people_lock:
                    cfg = load_people_cfg()
                    cfg.setdefault("people", []).append(pmo)
                    cfg.setdefault("nav_names", {})
                    if disp != pmo:
                        cfg["nav_names"][pmo] = disp
                    cfg.setdefault("taiga_users", {})[pmo] = hit[0]
                    save_people_cfg(cfg)
                ok, msg = rebuild_now()
                return self._json(200, {**people_view(load_people_cfg()), "taiga_name": hit[1],
                                        "rebuilt": ok, "message": msg})

            if path == "/api/admin/people" and method == "DELETE":
                pmo = (sent.get("pmo") or "").strip()
                with _people_lock:
                    cfg = load_people_cfg()
                    if pmo == cfg.get("leader"):
                        return self._json(400, {"error": "組長不能從名單移除"})
                    if pmo not in (cfg.get("people") or []):
                        return self._json(404, {"error": f"名單裡沒有「{pmo}」"})
                    cfg["people"] = [p for p in cfg["people"] if p != pmo]
                    for k in ("nav_names", "taiga_users"):
                        (cfg.get(k) or {}).pop(pmo, None)
                    save_people_cfg(cfg)
                ok, msg = rebuild_now()
                return self._json(200, {**people_view(load_people_cfg()), "rebuilt": ok, "message": msg})

            if path == "/api/admin/people/order" and method == "PUT":
                want = sent.get("people")
                with _people_lock:
                    cfg = load_people_cfg()
                    if not isinstance(want, list) or sorted(want) != sorted(cfg.get("people") or []):
                        return self._json(400, {"error": "排序名單跟目前名單對不上,重新整理頁面再試"})
                    cfg["people"] = want
                    save_people_cfg(cfg)
                ok, msg = rebuild_now()
                return self._json(200, {**people_view(load_people_cfg()), "rebuilt": ok, "message": msg})

            if path == "/api/admin/settings" and method == "PUT":
                url = (sent.get("notion_url") or "").strip()
                if not re.match(r"^https://([a-z0-9-]+\.)*notion\.(so|site|com)/", url):
                    return self._json(400, {"error": "請貼 Notion 頁面的網址(https://…notion.so/… 或 notion.com/…)", "field": "notion_url"})
                with _people_lock:
                    cfg = load_people_cfg()
                    cfg["notion_url"] = url
                    save_people_cfg(cfg)
                ok, msg = rebuild_now()
                return self._json(200, {**people_view(load_people_cfg()), "rebuilt": ok, "message": msg})

            if path == "/api/admin/notion" and method == "POST":
                st, en = sent.get("start") or "", sent.get("end") or ""
                if not (re.match(r"^\d{4}-\d{2}-\d{2}$", st) and re.match(r"^\d{4}-\d{2}-\d{2}$", en)) or st > en:
                    return self._json(400, {"error": "日期格式要是 YYYY-MM-DD,而且開始不能晚於結束"})
                return self._json(200, {"start": st, "end": en, "title": f"WebTeam {st.replace('-', '')}-{en.replace('-', '')}",
                                        "tables": notion_tables(st, en)})
        except SystemExit as e:          # weekly.py 的 die() 是 sys.exit
            return self._json(502, {"error": f"PMO 讀取失敗:{e}"})
        except (OSError, RuntimeError, ValueError) as e:
            return self._json(502, {"error": f"處理失敗:{e}"})
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.split("?")[0].startswith("/api/admin/"):
            return self._admin("DELETE")
        return self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.split("?")[0].startswith("/api/admin/"):
            return self._admin("PUT")
        if self.path.split("?")[0] != "/api/order":
            return self._json(404, {"error": "not found"})
        if not order_key():
            return self._json(503, {"error": "週報服務沒有設定組長鑰匙(ORDER_KEY)"})
        if not order_key_ok(self.headers.get("X-Order-Key")):
            return self._json(403, {"error": "只有組長可以修改項目順序"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            sent = json.loads(self.rfile.read(n) if n else b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"error": "request body 不是合法 JSON"})
        if not isinstance(sent, dict):
            return self._json(400, {"error": "request body 必須是 JSON 物件"})
        person, items, week = sent.get("person"), sent.get("items"), sent.get("week")
        if not isinstance(person, str) or person not in roster():
            return self._json(400, {"error": "名單上沒有這個人"})
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            return self._json(400, {"error": "items 必須是字串陣列"})
        if items and week not in (this_monday(), next_monday()):
            return self._json(400, {"error": "只能設定本週或下週的順序(週別過期了?重新整理頁面再試)"})
        items = [x.strip() for x in items if x.strip()]
        if len(items) > ORDER_MAX_ITEMS or any(len(x) > ORDER_MAX_LEN for x in items):
            return self._json(400, {"error": f"最多 {ORDER_MAX_ITEMS} 項、每項 {ORDER_MAX_LEN} 字"})
        try:
            d = save_order(person, week, items)
        except OSError as e:
            return self._json(500, {"error": f"寫入失敗:{e.strerror}"})
        got = d.get(person) or {}
        return self._json(200, {"person": person, "week": got.get("week"), "items": got.get("items", [])})

    def do_PATCH(self):
        if self.path.split("?")[0] == "/api/taiga/item":
            return self._taiga("PATCH")
        if not self.path.startswith("/api/pmo/"):
            return self._json(404, {"error": "not found"})
        return self._pmo("PATCH")

    def do_GET(self):
        # 頁面沒有 favicon,瀏覽器每次都會要一次;直接回 204 省掉一輪 404
        if self.path.split("?")[0] == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/api/pmo/"):
            return self._pmo("GET")
        if self.path.split("?")[0] == "/api/taiga/item":
            return self._taiga("GET")
        if self.path.split("?")[0] == "/api/taiga/view":
            return self._taiga_view()
        if self.path.split("?")[0].startswith("/api/admin/"):
            return self._admin("GET")
        if self.path.split("?")[0] == "/api/order":
            return self._json(200, {"order": load_order(),
                                    "editable": order_key_ok(self.headers.get("X-Order-Key"))})
        if self.path.split("?")[0] == "/api/rebuild":
            return self._json(200, {"available": True, "running": _state["running"],
                                    "cooldown": cooldown_left(), "generated": generated_at(),
                                    "edit": bool(server_key()), "taiga_edit": bool(taiga_creds()),
                                    "taiga_view": bool(taiga_creds())})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0].startswith("/api/admin/"):
            return self._admin("POST")
        if self.path.split("?")[0] != "/api/rebuild":
            return self._json(404, {"error": "not found"})
        # 冷卻與併發都在這裡擋:前端計時器擋不住重整與多分頁
        with _lock:
            if _state["running"]:
                return self._json(429, {"error": "正在重抓,請稍候", "cooldown": cooldown_left()})
            left = cooldown_left()
            if left > 0:
                return self._json(429, {"error": f"冷卻中,還要 {left} 秒", "cooldown": left})
            _state["running"] = True
        try:
            ok, msg = run_build()
        finally:
            with _lock:
                _state["running"] = False
                _state["last"] = time.time()   # 失敗也起算,免得一直重試打 PMO
        return self._json(200 if ok else 500,
                          {"ok": ok, "message": msg, "cooldown": cooldown_left(),
                           "generated": generated_at()})

    def end_headers(self):
        """靜態檔(週報 HTML)一律 no-cache:沒這行時瀏覽器會自己猜快取多久,
        部署後可能拿著舊版 HTML 不回來問 —— 2026-10-11 組長專區就因此看到舊畫面。
        no-cache 仍可用 304,只是每次都會先問一聲,不會多傳一份。"""
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        """只印重抓相關的行,其餘安靜。

        注意:`send_error` 會用 log_error("code %d, message %s", HTTPStatus.X, msg) 呼叫進來,
        args[0] 是 HTTPStatus 不是字串 —— 早期版本直接對 args[0] 做 `in` 判斷,
        於是每個 404(favicon 首當其衝)都在寫出回應前就丟 TypeError、連線被硬斷,
        前面掛 cloudflared 時對方看到的就是一整排 EOF。所以這裡先安全地格式化成字串。
        """
        try:
            line = fmt % args
        except Exception:
            line = str(fmt)
        if "/api/rebuild" in line:
            sys.stderr.write("[serve] %s\n" % line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--cooldown", type=int, default=30)
    a = ap.parse_args()
    _state["cooldown"] = a.cooldown
    if not os.path.exists(OUT_FILE):
        sys.exit(f"[錯誤] 找不到 {OUT_FILE},先跑一次 build.py")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"週報服務:http://{a.host}:{a.port}/pmo-weekly.html")
    print(f"重抓冷卻:{a.cooldown} 秒(server 端強制)  Ctrl-C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
