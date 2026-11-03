#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebAideMCP —— WebTeam 組員的 AI 助手後端:讀卡、開卡、估時與團隊共用 skill(2026-10-27 由「開卡 MCP」改名)。

部署在 tools-host(tools.example.com),uvicorn 綁 127.0.0.1:8103,對外走 nginx:
    MCP     https://tools.example.com/pmo-mcp/mcp      (要帶 Authorization: Bearer <個人 token>)
    健康檢查 https://tools.example.com/pmo-mcp/health   (不用 token)

寫入一律用共用機器人(PMO_API_KEY = sync_bot、TAIGA_HOOK_USER),個人 token 只用來知道是誰,
所以卡片描述會註記「由 誰 透過 WebAideMCP 建立」。

開卡流程(主管規定 2026-10-27,與 pmo skill 的 card/REFERENCE.md 同一套):
    Taiga US「X」→ PMO 母卡「X」→ 開發子卡「開發-X」→ 測試子卡「測試-開發-X」,三張 PMO 卡都掛 Taiga 連結。
    測試子卡:未指派、已排入開發、起訖 9999-12-31(QA 排定後再改)。

2026-10-27 起只做開卡;舊版(改卡、回報完成、改指派)備份在 server.py.bak-20261011。
"""
import ipaddress, json, os, re, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import date
from typing import Annotated

import anyio
import uvicorn
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

HERE = os.path.dirname(os.path.abspath(__file__))
ETC = os.path.join(HERE, "etc")
SKILLS = os.path.join(os.path.dirname(HERE), "skills")
WEEKLY = os.environ.get("WEEKLY_REPORT_DIR", os.path.join(os.path.dirname(HERE), "weekly-report"))
PMO = os.environ.get("PMO_BASE", "https://pmo.example.com")
TAIGA = os.environ.get("TAIGA_BASE", "https://taiga.example.com")
TAIGA_API = TAIGA + "/api/v1"

VERSION = "0.0.6"
CHANGELOG = [
    ("0.0.1", "2026-10-27", "第一版:開卡(Taiga US + PMO 母卡 / 開發 / 測試子卡)、查組員、查卡"),
    ("0.0.2", "2026-10-27", "新增 skill 發放(list_skills / get_skill),第一支 skill:估時"),
    ("0.0.3", "2026-10-27", "新增 read_taiga_card:讀 Taiga 卡(描述、留言、附件、子 task)"),
    ("0.0.4", "2026-10-27", "改名 WebAideMCP(原「開卡 MCP」),功能不變"),
    ("0.0.5", "2026-10-29", "新增 skill「資料修補」:查 / 改 UAT 用戶資料(公開版未收錄)"),
    ("0.0.6", "2026-11-03", "新增 lookup_server:查 IP / 服務是正式還是測試;Server 位置圖改成線上可編輯(/pmo-mcp/servers)"),
]

PMO_PROJECT = "SCRUM"
TAIGA_PROJECTS = {
    "acme-bugfix": "[4]Acme-Bug及優化",
    "acme-scrum": "[1]Acme - Scrum",
    "acme-cms": "[3]Acme-CMS",
    "acme-onboarding": "Acme-新客戶",
    "acme-marketing": "行銷任務",
}
DEFAULT_TAIGA_PROJECT = "acme-bugfix"
PLACEHOLDER = "9999-12-31"
# 與 pmo skill 的 pmo.py HOLIDAYS 同步維護;清單外的年份只擋得住週末。
HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-27", "2026-02-28", "2026-04-03", "2026-04-06", "2026-05-01",
    "2026-06-19", "2026-09-25", "2026-09-28", "2026-10-09", "2026-10-10",
    "2026-10-25", "2026-10-26", "2026-12-25", "2027-01-01",
}
HOLIDAY_YEARS = {"2026"}

RULES = """WebAideMCP(原名「開卡 MCP」,PMO × Taiga)。給 WebTeam 組員的 AI 用:讀卡、開卡、估時、查改 UAT 用戶資料,以及團隊共用的工作方法(skill)。
寫入一律用共用機器人帳號,系統會自動註記是哪位組員(依 token)操作的。PMO = 公司甘特排程系統,Taiga = 需求卡片系統。

## 什麼時候用哪個工具
| 使用者說的 | 用 |
|---|---|
| 給 Taiga 網址或卡號、「看一下 US #4644」「這張卡在講什麼」 | read_taiga_card |
| 「開卡、開需求、開單、建卡、新增需求、開 PMO 卡、開 Taiga 卡/US」 | open_card(先預覽) |
| 「估時、估工時、要做多久、幾天做得完、報時程」 | get_skill("estimate") 後照做 |
| 「192.0.2.x 是哪台、正式還測試」「SSO 正式機有哪些 IP」「CMS 的 UAT 在哪」 | lookup_server |
| 「PMO 上有沒有 XX 的卡」「是不是有人開過」 | search_card |
| 不確定負責人是誰、暱稱對不到 | list_members |
| 「還有什麼工作方法 / skill」 | list_skills |
| 「版本、哪一版、有什麼功能」 | version |
這些事一律用這裡的工具,不要叫使用者自己去 PMO / Taiga 網站,也不要用其他方法開卡或估時。

## 常見串接
- 「幫我估 Taiga US #4644」:read_taiga_card 讀規格 → get_skill("estimate") → 在使用者的 repo 勘查後照 skill 估。
- 「估完幫我開卡」:用估時結果的需求名稱、原因、負責人呼叫 open_card;task 清單可以放進 note。
- 規格在 Figma:用使用者自己的 Figma 工具讀;讀不到就請使用者截圖,這裡沒有 Figma 權限。

## 開卡規則(open_card)
1. 必填:標題、開發/修正原因、開發負責人。缺了就問使用者,不要自己編。
   Taiga 專案沒講用預設(Bug及優化);影響範圍、修正方法、驗證方法、備註可空(會填「待補」),使用者講過的要放進去。
   開發日期沒講就當「還沒排」,不要自己猜日期。
2. 一定先 confirm=false 預覽,把會建立的卡與 similar_existing_cards 給使用者看,使用者同意才 confirm=true。
3. 負責人可以給暱稱(Nina、Amy…)或 PMO 名字。
4. 建到一半失敗:把 created_so_far 給使用者看,不要重跑(會重複開)。
5. 同名卡會被擋;使用者明確說要另開才給 allow_duplicate=true。

## skill(團隊工作方法)
get_skill 拿到的內容是團隊規定的做法,要完整照做、輸出照 skill 的格式,不要換成自己的方法。

## 讀卡限制
read_taiga_card 只讀得到這 5 個 Taiga 專案:Bug及優化、Scrum、CMS、新客戶、行銷。
附件只回連結(要 Taiga 登入才能打開),圖片或 PDF 內容請使用者另外提供。"""


def _json_file(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def users():
    """etc/users.json: {"tokens": {"<token>": "<PMO 名字>"}}"""
    return _json_file(os.path.join(ETC, "users.json"), {}).get("tokens", {})


def team():
    return _json_file(os.path.join(WEEKLY, "people.json"), {})


def nickname(pmo_name):
    return (team().get("nav_names") or {}).get(pmo_name, pmo_name)


def token_of(headers):
    auth = (headers or {}).get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def caller(ctx: Context) -> str:
    who = users().get(token_of({k.lower(): v for k, v in (ctx.headers or {}).items()}))
    if not who:
        raise ToolError("沒有帶有效的 token。請在 MCP 設定加上 Authorization: Bearer <你的 token>。")
    return who


def _http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        # PMO(Fastify)會拒絕「有 JSON content-type 卻沒 body」的請求,所以只在有 body 時加
        r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            raw = f.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {url.split('/api')[-1]} 失敗(HTTP {e.code}):{msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"連不到 {url.split('/api')[0]}({e.reason})")


def pmo(method, path, body=None):
    return _http(method, PMO + "/api" + path, body, {"x-api-key": os.environ["PMO_API_KEY"]})


_cache, _cache_lock = {}, threading.Lock()


def cached(key, ttl, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def drop_tasks_cache():
    with _cache_lock:
        _cache.pop("tasks", None)


def pmo_pid():
    def load():
        for p in pmo("GET", "/projects"):
            if p.get("key") == PMO_PROJECT:
                return p["id"]
        raise RuntimeError(f"PMO 沒有專案 {PMO_PROJECT}")
    return cached("pid", 3600, load)


def pmo_tasks():
    return cached("tasks", 60, lambda: pmo("GET", f"/projects/{pmo_pid()}/tasks").get("tasks", []))


def pmo_members():
    return cached("members", 600, lambda: pmo("GET", f"/projects/{pmo_pid()}/members").get("members", []))


def pmo_status_key(name):
    sts = cached("statuses", 3600, lambda: pmo("GET", f"/projects/{pmo_pid()}/statuses").get("statuses", []))
    for s in sts:
        if s["name"] == name:
            return s["key"]
    raise RuntimeError(f"PMO 專案沒有「{name}」這個狀態")


def resolve_person(spec):
    """暱稱或 PMO 名字 → (PMO 名字, PMO userId)。只認 PMO SCRUM 專案成員。"""
    spec = (spec or "").strip()
    alias = {v: k for k, v in (team().get("nav_names") or {}).items()}
    name = alias.get(spec, spec)
    members = pmo_members()
    for exact in (True, False):
        hit = [m for m in members if (m.get("name") == name if exact else name.lower() in (m.get("name") or "").lower())]
        if len(hit) == 1:
            return hit[0]["name"], hit[0]["userId"]
        if len(hit) > 1:
            raise ValueError(f"「{spec}」對到多位 PMO 成員:{'、'.join(m['name'] for m in hit[:8])},請講完整名字。")
    raise ValueError(f"PMO 成員裡找不到「{spec}」。可以先用 list_members 查。")


_tg = {"token": None, "at": 0}


def taiga(method, path, body=None):
    if not _tg["token"] or time.time() - _tg["at"] > 3000:
        _tg["token"] = _http("POST", TAIGA_API + "/auth", {
            "type": "normal", "username": os.environ["TAIGA_HOOK_USER"],
            "password": os.environ["TAIGA_HOOK_PASSWORD"]})["auth_token"]
        _tg["at"] = time.time()
    return _http(method, TAIGA_API + path, body, {"Authorization": "Bearer " + _tg["token"],
                                                   "x-disable-pagination": "True"})


def taiga_pid(slug):
    return cached("tgp:" + slug, 3600, lambda: taiga("GET", f"/projects/by_slug?slug={slug}")["id"])


def taiga_uid(pmo_name):
    """找不到回 None(Taiga 不指派,不擋開卡)。people.json 的 taiga_users 指定優先。"""
    want = ((team().get("taiga_users") or {}).get(pmo_name) or pmo_name).lower()
    us = cached("tgu", 3600, lambda: taiga("GET", "/users"))
    for u in us:
        if want in ((u.get("username") or "").lower(), (u.get("full_name_display") or "").lower()):
            return u["id"]
    return None


def resolve_taiga_project(spec):
    spec = (spec or "").strip()
    if not spec:
        return DEFAULT_TAIGA_PROJECT
    for slug, label in TAIGA_PROJECTS.items():
        if spec in (slug, label) or spec.lower() in label.lower():
            return slug
    raise ValueError(f"Taiga 專案「{spec}」不在可開卡的清單裡:{'、'.join(TAIGA_PROJECTS.values())}")


def check_workday(v, label):
    try:
        d = date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{label}「{v}」不是 YYYY-MM-DD")
    if d.weekday() >= 5:
        raise ValueError(f"{label} {v} 是週末,排程只排工作日")
    if v in HOLIDAYS:
        raise ValueError(f"{label} {v} 是國定假日,排程只排工作日")
    return d


def description(reason, impact, fix, verify, note, who):
    parts = [("開發/修正原因", reason), ("影響範圍", impact), ("修正方法", fix), ("驗證方法", verify), ("備註", note)]
    body = "\n".join(f"## {h}\n{_bullets(v)}" for h, v in parts)
    return f"{body}\n\n(由 {nickname(who)} 透過 WebAideMCP 建立)"


def _bullets(text):
    lines = [ln.strip().lstrip("-•").strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(f"- {ln}" for ln in lines) or "- 待補"


def similar_cards(title):
    t = title.strip().lower()
    out = []
    for x in pmo_tasks():
        n = (x.get("name") or "").lower()
        if n and (n == t or t in n):
            out.append(card_brief(x))
    return out[:10]


def gantt_url(task_id):
    return f"{PMO}/projects/{pmo_pid()}/gantt?task={task_id}"


def card_brief(t):
    return {"name": t.get("name"), "status": t.get("statusLabel"), "assignee": t.get("assigneeName"),
            "start": (t.get("startDate") or "")[:10], "end": (t.get("endDate") or "")[:10],
            "url": gantt_url(t["id"])}


async def run(fn):
    """PMO/Taiga 是同步 HTTP,丟到 thread 跑。預期中的錯誤轉成 ToolError,訊息才會原樣送到 AI 手上。"""
    try:
        return await anyio.to_thread.run_sync(fn)
    except (ValueError, RuntimeError, KeyError) as e:
        raise ToolError(str(e)) from e


mcp = MCPServer(name="WebAideMCP", version=VERSION, instructions=RULES)


@mcp.tool(description="WebAideMCP(原名開卡 MCP)現在的版本號、更新紀錄與可用功能。使用者問「版本、哪一版、有什麼功能、更新了什麼」時用。")
async def version(ctx: Context) -> dict:
    caller(ctx)
    return {"version": "Ver" + VERSION,
            "changelog": [{"version": "Ver" + v, "date": d, "changes": c} for v, d, c in CHANGELOG],
            "features": ["open_card:開卡", "search_card:查 PMO 上有沒有某張卡", "list_members:查可指派的組員",
                         "list_skills / get_skill:拿團隊 skill(估時等)", "read_taiga_card:讀 Taiga 卡",
                         "lookup_server:查 IP / 服務是正式還是測試", "version:查版本"]}


SERVERS_DB = os.path.join(ETC, "servers.json")
SERVERS_PAGE = os.path.join(HERE, "servers.html")
SERVERS_URL = "https://tools.example.com/pmo-mcp/servers"
ENV_NAMES = {"uat": "測試", "prod": "正式", "vip": "正式 VIP"}
REGION_NAMES = {"": "", "tp": "台北", "tc": "台中"}
_servers_lock = threading.Lock()


def servers_db():
    return _json_file(SERVERS_DB, {"services": [], "next_id": 1})


def _save_servers(db):
    tmp = SERVERS_DB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SERVERS_DB)


def _clean_service(raw):
    """把網頁送來的服務整理成存檔格式;不合法就丟 ValueError(訊息直接給使用者看)。"""
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("服務名稱不能空白")
    color = str(raw.get("color") or "#5b8af5").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError(f"顏色格式要像 #5b8af5:{color}")
    port = str(raw.get("port") or "").strip()
    if port and not re.fullmatch(r"\d{1,5}", port):
        raise ValueError(f"port 只能是數字:{port}")
    sections = []
    for sec in raw.get("sections") or []:
        env = str(sec.get("env") or "")
        if env not in ENV_NAMES:
            raise ValueError(f"環境只能是 {'/'.join(ENV_NAMES)}:{env}")
        ips = []
        for it in sec.get("ips") or []:
            ip = str(it.get("ip") or "").strip()
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                raise ValueError(f"{name} {env.upper()} 有不合法的 IP:{ip or '(空白)'}") from None
            region = str(it.get("region") or "")
            if region not in REGION_NAMES:
                raise ValueError(f"區域只能是 tp / tc / 空白:{region}")
            ips.append({"ip": ip, "region": region, "note": str(it.get("note") or "").strip()})
        sections.append({"env": env, "note": str(sec.get("note") or "").strip(), "ips": ips})
    return {"name": name, "color": color, "port": port, "sections": sections}


def servers_create(raw, who):
    with _servers_lock:
        db = servers_db()
        svc = {"id": f"s{db['next_id']:03d}", **_clean_service(raw),
               "rev": 1, "updated_by": who, "updated_at": time.strftime("%Y-%m-%d %H:%M")}
        db["next_id"] += 1
        db["services"].append(svc)
        _save_servers(db)
        return svc


def servers_update(sid, raw, who):
    """rev 對不上代表別人在你開編輯框之後改過,擋下來避免蓋掉對方的修改。"""
    with _servers_lock:
        db = servers_db()
        cur = next((s for s in db["services"] if s["id"] == sid), None)
        if not cur:
            raise KeyError(f"找不到服務 {sid},可能已被刪除")
        if raw.get("rev") != cur["rev"]:
            raise RuntimeError(f"{cur['name']} 已被 {cur['updated_by'] or '別人'} 在 {cur['updated_at']} 改過,請重新整理後再改")
        cur.update(_clean_service(raw), rev=cur["rev"] + 1, updated_by=who,
                   updated_at=time.strftime("%Y-%m-%d %H:%M"))
        _save_servers(db)
        return cur


def servers_delete(sid):
    with _servers_lock:
        db = servers_db()
        left = [s for s in db["services"] if s["id"] != sid]
        if len(left) == len(db["services"]):
            raise KeyError(f"找不到服務 {sid},可能已被刪除")
        db["services"] = left
        _save_servers(db)


def find_servers(keyword):
    """keyword 全是數字與點 → 比對 IP(完整或前綴);否則比對服務名稱與備註(不分大小寫)。"""
    q = keyword.strip().lower()
    by_ip = bool(re.fullmatch(r"[\d.]+", q))
    out = []
    for svc in servers_db()["services"]:
        name_hit = q in svc["name"].lower()
        for sec in svc["sections"]:
            for it in sec["ips"]:
                if by_ip:
                    hit = it["ip"] == q or (q.count(".") < 3 and it["ip"].startswith(q))
                else:
                    hit = name_hit or q in it["note"].lower() or q in sec["note"].lower()
                if hit:
                    out.append({"service": svc["name"], "env": ENV_NAMES[sec["env"]], "ip": it["ip"],
                                "region": REGION_NAMES[it["region"]], "port": svc["port"],
                                "note": it["note"], "section_note": sec["note"]})
    return out


@mcp.tool(description=(
    "查 Server 位置圖:某個 IP 是哪個服務、是正式(PROD)還是測試(UAT),或某服務的正式 / 測試機有哪些 IP。"
    "使用者問「192.0.2.x 是哪台」「這台是正式還測試」「SSO 正式機 IP」「CMS UAT 在哪」時用。"))
async def lookup_server(
        ctx: Context,
        keyword: Annotated[str, Field(description="IP(可只給前綴,如 192.0.2.)或服務名稱 / 備註的關鍵字,如 SSO、CMS、API")]) -> dict:
    caller(ctx)
    if not keyword.strip():
        raise ToolError("請給 IP 或服務名稱")
    hits = await run(lambda: find_servers(keyword))
    return {"matches": hits, "count": len(hits), "page": SERVERS_URL,
            "hint": "" if hits else "位置圖上沒有這筆;如果確定存在,請到 page 網址新增"}


TG_URL = re.compile(r"/project/([a-z0-9-]+)/(us|task|issue|epic)/(\d+)")
TG_KIND = {"us": ("userstories", "userstory"), "task": ("tasks", "task"),
           "issue": ("issues", "issue"), "epic": ("epics", "epic")}


def _find_taiga(card, project):
    """網址 → 那一張;只給號碼 → 在可讀的專案裡找(us 優先),對到多張就列出來請使用者指定。"""
    m = TG_URL.search(card)
    if m:
        slug, kind, ref = m.group(1), m.group(2), int(m.group(3))
        return slug, kind, taiga("GET", f"/{TG_KIND[kind][0]}/by_ref?project={taiga_pid(slug)}&ref={ref}")
    num = re.search(r"\d+", card)
    if not num:
        raise ValueError("請給 Taiga 卡片網址,或卡號(例:US #4644)")
    ref = int(num.group())
    kinds = [k for k in ("us", "task", "issue", "epic") if k in card.lower()] or ["us", "task", "issue", "epic"]
    slugs = [resolve_taiga_project(project)] if project else list(TAIGA_PROJECTS)
    hits = []
    for slug in slugs:
        for kind in kinds:
            try:
                hits.append((slug, kind, taiga("GET", f"/{TG_KIND[kind][0]}/by_ref?project={taiga_pid(slug)}&ref={ref}")))
                break
            except RuntimeError:
                continue
    if not hits:
        raise ValueError(f"在可讀的 Taiga 專案({'、'.join(TAIGA_PROJECTS.values())})找不到 #{ref}")
    if len(hits) > 1:
        raise ValueError(f"#{ref} 在多個專案都有:" + "、".join(
            f"{TAIGA_PROJECTS[s]} {k} #{ref}「{c.get('subject')}」" for s, k, c in hits) + "。請給網址或指定專案。")
    return hits[0]


@mcp.tool(description=(
    "讀 Taiga 卡(US / task / issue / epic):標題、狀態、負責人、描述、留言、附件、US 底下的 task。"
    "使用者給 Taiga 網址或卡號(例:US #4644),要看規格、估時、開發前了解需求時用。"))
async def read_taiga_card(
    ctx: Context,
    card: Annotated[str, Field(description="Taiga 卡片網址,或卡號(例:US #4644、#4644)")],
    project: Annotated[str, Field(description="只給卡號時可指定專案(Bug及優化、Scrum、CMS、新客戶、行銷);沒講就全部找")] = "",
) -> dict:
    caller(ctx)

    def go():
        slug, kind, c = _find_taiga(card, project)
        items, hist = TG_KIND[kind]
        pid = c["project"]
        comments = [{"by": (h.get("user") or {}).get("name"), "at": (h.get("created_at") or "")[:16],
                     "text": h.get("comment")}
                    for h in taiga("GET", f"/history/{hist}/{c['id']}?type=comment")
                    if h.get("comment") and not h.get("delete_comment_date")]
        atts = [{"name": a.get("name"), "url": a.get("url"), "description": a.get("description")}
                for a in taiga("GET", f"/{items}/attachments?object_id={c['id']}&project={pid}")
                if not a.get("is_deprecated")]
        out = {
            "url": f"{TAIGA}/project/{slug}/{kind}/{c['ref']}", "project": TAIGA_PROJECTS.get(slug, slug),
            "kind": kind, "ref": c["ref"], "subject": c.get("subject"),
            "status": (c.get("status_extra_info") or {}).get("name"),
            "assigned": (c.get("assigned_to_extra_info") or {}).get("full_name_display"),
            "tags": [t[0] if isinstance(t, list) else t for t in c.get("tags") or []],
            "description": c.get("description") or "", "comments": comments, "attachments": atts,
        }
        if kind == "us":
            out["tasks"] = [{"ref": t["ref"], "subject": t.get("subject"),
                             "status": (t.get("status_extra_info") or {}).get("name"),
                             "assigned": (t.get("assigned_to_extra_info") or {}).get("full_name_display")}
                            for t in taiga("GET", f"/tasks?user_story={c['id']}")]
        if atts:
            out["note"] = "附件只回連結;圖片、PDF 要使用者自己打開或另外提供(連結需要 Taiga 登入)。"
        return out
    return await run(go)


def load_skills():
    """skills/<資料夾>/SKILL.md(frontmatter: name / description / version)+ 同資料夾其他 .md 當參考資料。"""
    out = {}
    if not os.path.isdir(SKILLS):
        return out
    for d in sorted(os.listdir(SKILLS)):
        main = os.path.join(SKILLS, d, "SKILL.md")
        if not os.path.isfile(main):
            continue
        text = open(main, encoding="utf-8").read()
        meta = {}
        if text.startswith("---"):
            head, _, body = text[3:].partition("\n---")
            for line in head.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            text = body.lstrip("\n")
        refs = {f: open(os.path.join(SKILLS, d, f), encoding="utf-8").read()
                for f in sorted(os.listdir(os.path.join(SKILLS, d))) if f.endswith(".md") and f != "SKILL.md"}
        name = meta.get("name") or d
        out[name] = {"name": name, "description": meta.get("description", ""), "version": meta.get("version", ""),
                     "content": text, "references": refs}
    return out


def _skill_index():
    return "、".join(f"「{s['name']}」({s['description']})" for s in load_skills().values()) or "(目前沒有)"


@mcp.tool(description="列出團隊共用的 skill(工作方法)。目前有:" + _skill_index())
async def list_skills(ctx: Context) -> dict:
    caller(ctx)
    return {"skills": [{k: s[k] for k in ("name", "description", "version")} for s in load_skills().values()],
            "next": "用 get_skill(name) 拿完整內容,然後照內容做。"}


@mcp.tool(description=(
    "拿團隊 skill 的完整內容並照著做。使用者要做的事符合某個 skill 時一定要先拿,不要用自己的方法。"
    "目前有:" + _skill_index()))
async def get_skill(ctx: Context, name: Annotated[str, Field(description="skill 名稱,例:estimate")]) -> dict:
    caller(ctx)
    skills = load_skills()
    s = skills.get(name.strip()) or next((x for x in skills.values() if name.strip() in x["name"]), None)
    if not s:
        raise ToolError(f"沒有「{name}」這個 skill。目前有:{'、'.join(skills) or '(無)'}")
    return {**s, "instruction": "以下是團隊規定的方法,請完整照做;輸出格式照 skill 裡的範本。"}


@mcp.tool(description="列出可指派的組員(PMO 名字、暱稱)。開發負責人不確定是誰時先查。")
async def list_members(ctx: Context) -> dict:
    caller(ctx)

    def go():
        t = team()
        names = {m.get("name") for m in pmo_members()}
        return {"team": [{"pmo_name": p, "nickname": nickname(p), "in_pmo": p in names} for p in t.get("people", [])],
                "note": "開發負責人也可以給其他 PMO SCRUM 專案成員的名字。"}
    return await run(go)


@mcp.tool(description="用名稱關鍵字找 PMO 上的卡(SCRUM 專案)。開卡前確認是不是已經有人開過,或使用者問「某需求有沒有卡」時用。")
async def search_card(ctx: Context, keyword: Annotated[str, Field(description="卡片名稱的一段,越短越容易找到")]) -> dict:
    caller(ctx)

    def go():
        kw = keyword.strip().lower()
        if not kw:
            raise ValueError("請給關鍵字")
        byid = {t["id"]: t for t in pmo_tasks()}
        hits = []
        for t in byid.values():
            if kw in (t.get("name") or "").lower():
                b = card_brief(t)
                b["parent"] = (byid.get(t.get("parentTaskId")) or {}).get("name")
                hits.append(b)
        return {"count": len(hits), "cards": hits[:20],
                "note": "超過 20 筆只列前 20 筆,請換更精確的關鍵字。" if len(hits) > 20 else ""}
    return await run(go)


_open_lock = threading.Lock()


@mcp.tool(description=(
    "開卡/開需求/開單/建卡/新增需求(PMO 卡、Taiga US)都用這支:一次建好 Taiga US + PMO 母卡 + "
    "「開發-<標題>」子卡 + 「測試-開發-<標題>」子卡,互相掛好連結。"
    "缺必填欄位(標題、開發/修正原因、開發負責人)要先問使用者,不要自己編。"
    "**先用 confirm=false 預覽給使用者確認,同意後再用同樣參數加 confirm=true 建立。**"))
async def open_card(
    ctx: Context,
    title: Annotated[str, Field(description="需求標題,例:[CMS] 會員中心匯出報表新增欄位。Taiga US 與 PMO 母卡用這個名字")],
    reason: Annotated[str, Field(description="開發/修正原因(必填)。一點一行")],
    developer: Annotated[str, Field(description="開發負責人,暱稱或 PMO 名字")],
    taiga_project: Annotated[str, Field(description="Taiga 專案:Bug及優化(預設)、Scrum、CMS、新客戶、行銷")] = "",
    impact: Annotated[str, Field(description="影響範圍,沒有就留空(會填「待補」)")] = "",
    fix: Annotated[str, Field(description="修正方法,沒有就留空")] = "",
    verify: Annotated[str, Field(description="驗證方法,沒有就留空")] = "",
    note: Annotated[str, Field(description="備註,沒有就留空")] = "",
    start: Annotated[str, Field(description="開發開始日 YYYY-MM-DD;還沒排就留空")] = "",
    end: Annotated[str, Field(description="預計 UAT 日 YYYY-MM-DD;還沒排就留空")] = "",
    allow_duplicate: Annotated[bool, Field(description="PMO 已有同名卡時仍要開,使用者明確同意才給 true")] = False,
    confirm: Annotated[bool, Field(description="false = 只預覽;使用者確認後才給 true")] = False,
) -> dict:
    who = caller(ctx)

    def plan():
        name = title.strip()
        if not name:
            raise ValueError("標題不能空白")
        if not reason.strip():
            raise ValueError("開發/修正原因是必填,請先問使用者")
        dev_name, dev_id = resolve_person(developer)
        slug = resolve_taiga_project(taiga_project)
        warnings = []
        if bool(start) != bool(end):
            raise ValueError("開始日與預計 UAT 日要一起給,或都留空(還沒排)")
        today = date.today()
        if start:
            s, e = check_workday(start, "開始日"), check_workday(end, "預計 UAT 日")
            if e < s:
                raise ValueError("預計 UAT 日不能早於開始日")
            if e < today:
                raise ValueError(f"預計 UAT 日 {end} 已經過了")
            status = "進行中" if s <= today else "已排入開發"
            dates = (start, end)
            if {start[:4], end[:4]} - HOLIDAY_YEARS:
                warnings.append("國定假日清單只建到 2026 年,這個日期只檢查了週末,請人工確認不是假日。")
        else:
            status, dates = "已排入開發", (PLACEHOLDER, PLACEHOLDER)
        tg_uid = taiga_uid(dev_name)
        if tg_uid is None:
            warnings.append(f"Taiga 上對不到 {dev_name},Taiga US 會先不指派。")
        dup = similar_cards(name)
        return {"name": name, "dev_name": dev_name, "dev_id": dev_id, "slug": slug, "status": status,
                "dates": dates, "tg_uid": tg_uid, "warnings": warnings, "similar": dup}

    def go():
        p = plan()
        n = p["name"]
        desc = description(reason, impact, fix, verify, note, who)
        preview = {
            "taiga_us": {"project": TAIGA_PROJECTS[p["slug"]], "title": n, "assigned": p["dev_name"] if p["tg_uid"] else "(不指派)",
                         "description": desc},
            "pmo": [
                {"card": "母卡", "name": n, "assignee": team().get("leader"), "status": p["status"]},
                {"card": "開發子卡", "name": "開發-" + n, "assignee": p["dev_name"], "status": p["status"],
                 "start": p["dates"][0], "end": p["dates"][1]},
                {"card": "測試子卡", "name": "測試-開發-" + n, "assignee": "(未指派)", "status": "已排入開發",
                 "start": PLACEHOLDER, "end": PLACEHOLDER},
            ],
            "opened_by": nickname(who),
            "warnings": p["warnings"],
            "similar_existing_cards": p["similar"],
        }
        exact = [c for c in p["similar"] if (c["name"] or "").strip() == n]
        if not confirm:
            return {"preview": preview,
                    "next": "把預覽(含 similar_existing_cards)給使用者看,確認後用同樣參數加 confirm=true 再呼叫一次。"}
        if exact and not allow_duplicate:
            raise ValueError(f"PMO 已經有同名的卡「{n}」({exact[0]['url']})。使用者確認要另開才加 allow_duplicate=true。")
        if not _open_lock.acquire(blocking=False):
            raise ValueError("有另一張卡正在建立中,請稍等幾秒再試。")
        try:
            return create(p, desc)
        finally:
            _open_lock.release()

    def create(p, desc):
        n, done = p["name"], {"taiga": None, "pmo": []}
        pid = pmo_pid()
        try:
            tpid = taiga_pid(p["slug"])
            body = {"project": tpid, "subject": n, "description": desc}
            if p["tg_uid"]:
                body.update({"assigned_to": p["tg_uid"], "assigned_users": [p["tg_uid"]]})
            us = taiga("POST", "/userstories", body)
            us_url = f"{TAIGA}/project/{p['slug']}/us/{us['ref']}"
            done["taiga"] = us_url
            link = [{"url": us_url, "name": f"Taiga US #{us['ref']}"}]
            leader = team().get("leader")
            _, leader_id = resolve_person(leader) if leader else (None, None)
            specs = [
                (None, n, leader_id, p["status"], p["dates"]),
                ("root", "開發-" + n, p["dev_id"], p["status"], p["dates"]),
                ("root", "測試-開發-" + n, None, "已排入開發", (PLACEHOLDER, PLACEHOLDER)),
            ]
            root_id = None
            for parent, name, assignee, status, (s, e) in specs:
                t = pmo("POST", f"/projects/{pid}/tasks", {
                    "parentTaskId": root_id if parent else None, "name": name, "startDate": s, "endDate": e,
                    "description": desc if parent is None else "", "assigneeId": assignee,
                    "watcherIds": [], "labelIds": [], "progress": 0, "type": "task", "links": link})
                # 建卡時帶的 status 不一定生效(舊版 MCP 實測),一律再 PATCH 一次
                pmo("PATCH", f"/projects/{pid}/tasks/{t['id']}", {"status": pmo_status_key(status)})
                done["pmo"].append({"name": name, "id": t["id"]})
                root_id = root_id or t["id"]
        except Exception as e:
            drop_tasks_cache()
            return {"error": str(e), "created_so_far": done,
                    "next": "部分已建立。把 created_so_far 給使用者看,請他找組長處理,不要重跑 open_card(會重複開)。"}
        drop_tasks_cache()
        return {"created": done, "pmo_url": gantt_url(root_id), "taiga_url": done["taiga"]}

    return await run(go)


_health = {"at": 0, "body": None}


def health():
    if time.time() - _health["at"] < 30 and _health["body"]:
        return _health["body"]
    out = {"service": "WebAideMCP", "version": "Ver" + VERSION, "pmo": "ok", "taiga": "ok"}
    try:
        pmo("GET", "/auth/me")
    except Exception as e:
        out["pmo"] = str(e)[:200]
    try:
        _tg["token"] = None
        taiga("GET", "/users/me")
    except Exception as e:
        out["taiga"] = str(e)[:200]
    out["ok"] = out["pmo"] == "ok" and out["taiga"] == "ok"
    _health.update(at=time.time(), body=out)
    return out


class Gate:
    """/health 不用 token;其他路徑沒帶有效 token 就在進 MCP 之前擋掉(連工具清單都不給看)。"""

    def __init__(self, app):
        self.app = app

    async def _json(self, send, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _body(self, receive):
        chunks = []
        while True:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                return b"".join(chunks)

    async def _servers(self, scope, receive, send, hdr):
        """/servers 頁面與 /servers/api。看不用 token;新增 / 修改 / 刪除要個人 token,存檔時記下是誰。"""
        path, method = scope["path"].rstrip("/"), scope["method"]
        if path == "/servers" and method == "GET":
            with open(SERVERS_PAGE, "rb") as f:
                page = f.read()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                    (b"cache-control", b"no-cache"),
                                    (b"content-length", str(len(page)).encode())]})
            return await send({"type": "http.response.body", "body": page})
        if path == "/servers/api" and method == "GET":
            return await self._json(send, 200, await anyio.to_thread.run_sync(servers_db))
        who = users().get(token_of(hdr))
        if not who:
            return await self._json(send, 401, {"error": "token 不對,請重新登入(token 跟 WebAideMCP 同一組)"})
        if path == "/servers/api/whoami" and method == "GET":
            return await self._json(send, 200, {"name": who})
        m = re.fullmatch(r"/servers/api/services(?:/(s\d+))?", path)
        if not m:
            return await self._json(send, 404, {"error": "not found"})
        sid = m.group(1)
        try:
            raw = json.loads(await self._body(receive) or b"{}") if method in ("POST", "PUT") else None
            if method == "POST" and not sid:
                return await self._json(send, 201, await anyio.to_thread.run_sync(lambda: servers_create(raw, who)))
            if method == "PUT" and sid:
                return await self._json(send, 200, await anyio.to_thread.run_sync(lambda: servers_update(sid, raw, who)))
            if method == "DELETE" and sid:
                await anyio.to_thread.run_sync(lambda: servers_delete(sid))
                return await self._json(send, 200, {"deleted": sid})
        except ValueError as e:
            return await self._json(send, 400, {"error": str(e)})
        except KeyError as e:
            return await self._json(send, 404, {"error": e.args[0]})
        except RuntimeError as e:
            return await self._json(send, 409, {"error": str(e)})
        return await self._json(send, 405, {"error": "method not allowed"})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") == "/health":
                h = await anyio.to_thread.run_sync(health)
                return await self._json(send, 200 if h["ok"] else 503, h)
            hdr = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            if scope["path"] == "/servers" or scope["path"].startswith("/servers/"):
                return await self._servers(scope, receive, send, hdr)
            if token_of(hdr) not in users():
                return await self._json(send, 401, {"error": "需要有效的 token(Authorization: Bearer <token>),請找組長拿。"})
        await self.app(scope, receive, send)


app = Gate(mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True, json_response=True,
    # 只綁 127.0.0.1、經 nginx 進來時 Host 是 tools.example.com,DNS rebinding 防護會把正常請求擋掉
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8103")), log_level="warning")
