#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PMO 週報網頁產生器 —— 抓 PMO 排程,產出「單一自帶樣式的 HTML」。

單檔的用意:file:// 雙擊能開、任何 static server 能餵、要上內網文件站就是
`tech-docs/scripts/publish.sh 那個檔` 一行,不必改任何東西。

用法:
  build.py                        # 名單全員,以今天為基準的四週(上/本/下/下下)
  build.py --anchor 2026-09-07    # 換基準週(該日所在的那一週當「本週」)
  build.py --people "A,B"         # 這次只看某幾人(不動名單)
  build.py --add "lily"           # 加人進 people.json(先跟 PMO 對名字,打錯會擋)
  build.py --remove "lily"        # 減人
  build.py --list                 # 列出目前名單
  build.py -o ~/x.html            # 指定輸出
  build.py --serve 8099           # 產完起 http server(Ctrl-C 停)
  build.py --open                 # 產完用瀏覽器打開

完成的定義:狀態進到「交測」就算完成(RD 這端交出去了),見 DONE。
"""
import argparse, contextlib, io, json, os, re, sys, webbrowser
from datetime import date, timedelta
from urllib.parse import quote

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
WEEKLY_DIR = SKILL_DIR
PEOPLE_PATH = os.path.join(SKILL_DIR, "people.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
DEFAULT_OUT = os.path.join(SKILL_DIR, "out", "pmo-weekly.html")
PMO_BASE = "https://pmo.example.com"

if not os.path.isdir(WEEKLY_DIR):
    sys.exit(f"[錯誤] 找不到 {WEEKLY_DIR} —— 本 skill 靠 pmo-weekly 的抓取邏輯,請先確認它還在。")
sys.path.insert(0, WEEKLY_DIR)
import weekly as W  # noqa: E402
import taiga as TG  # 同目錄:讀 Taiga 卡的實際狀態

# 團隊定義:進到交測(含)之後 = RD 這端已完成。球在 QA 手上不算開發者沒做完。
DONE = {"UAT", "待上線", "Done"}
SHOW = {"UAT": "交測", "Doing": "進行中"}   # 用團隊講法顯示,組員才看得懂
SOON_DAYS = 3                    # 還沒交測、N 天內(含今天)到期 = 快到期
# 跟 taiga.py 的 TAIGA_RANK 同一把尺,兩邊才比得起來
STATUS_RANK = {"待進行": 0, "Doing": 1, "待RD修正": 2, "UAT": 3, "待上線": 4, "Done": 5}
WEEK_LABELS = ["上週", "本週", "下週", "下下週"]
# 下週/下下週的「未完成」只是「還沒開始」,不進 badge 計數,免得主管看到一片紅
COUNTED = {"上週", "本週"}


# ---------- 項目順序 ----------
ORDER_PATH = os.path.join(SKILL_DIR, "order.json")


def load_order():
    """頁面頂端「項目順序」的資料,由 serve.py 的 PUT /api/order 寫入。沒有檔案就是都沒填。
    每筆綁一週({"week": 週一, "items": [...]}),已過的週不嵌(頁面那邊也會再擋一次)。"""
    try:
        with open(ORDER_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    t = date.today()
    mon = (t - timedelta(days=t.weekday())).isoformat()
    return {k: v for k, v in d.items()
            if isinstance(v, dict) and isinstance(v.get("week"), str) and v["week"] >= mon}


# ---------- 名單 ----------
def load_people():
    with open(PEOPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_people(cfg):
    with open(PEOPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")



# ---------- 名字比對 ----------
def resolve(want, all_names):
    """(認得的, 不認得的)。

    weekly.py 的 resolve_people 只回傳「認得的」那一份,找不到的是印到 stderr,
    所以一次丟整批進去無法知道是誰沒對到 —— 這裡一個一個問,自己組出 bad 清單。
    順便吃掉它的提醒訊息,由本檔統一報。
    """
    ok, bad = [], []
    for raw in want:
        with contextlib.redirect_stderr(io.StringIO()):
            hit = W.resolve_people([raw], all_names)
        if hit:
            for n in hit:
                if n not in ok:
                    ok.append(n)
        else:
            bad.append(raw)
    return ok, bad


# ---------- 週次 ----------
def four_weeks(anchor):
    """anchor 所在那週當「本週」,回傳 [(標籤, 週一, 週日)] x4。"""
    monday = anchor - timedelta(days=anchor.weekday())
    out = []
    for i, label in enumerate(WEEK_LABELS):
        s = monday + timedelta(days=7 * (i - 1))
        out.append((label, s.isoformat(), (s + timedelta(days=6)).isoformat()))
    return out


# ---------- 連結 ----------
TAIGA_RE = re.compile(r"/project/([^/?#]+)/(us|epic|issue|task)/(\d+)")
KIND = {"us": "US", "epic": "EP", "issue": "IS", "task": "TK"}


def normalize_link(url, name):
    """Taiga 網址正規化。原始網址常帶 ?backlog-q=/?kanban-status= 這類看板篩選參數,
    那是當初複製網址時的畫面狀態,不是卡片的一部分,砍掉才穩定。"""
    m = TAIGA_RE.search(url)
    if m:
        slug, kind, num = m.group(1), m.group(2), m.group(3)
        base = url.split("/project/")[0]
        return {"tag": f"{KIND[kind]}{num}", "url": f"{base}/project/{slug}/{kind}/{num}",
                "kind": "taiga", "ref": int(num)}
    label = (name or "").strip()
    if not label:
        m2 = re.match(r"https?://([^/]+)", url)
        label = m2.group(1) if m2 else "連結"
    return {"tag": label, "url": url, "kind": "other"}


def links_of(task, byid):
    """連結多半掛在母任務(需求本身),分工子卡上沒有 —— 所以要往上找每一層祖先。
    從祖先拿到的標 inherited:母卡常掛著整個需求的所有 Taiga 卡(cms/ios/android 各一張),
    不見得跟這個人有關,build_data 會再用 Taiga 的 assigned/watchers 篩。"""
    out, seen, cur, guard = [], set(), task, set()
    while cur and cur["id"] not in guard:
        guard.add(cur["id"])
        for l in (cur.get("links") or []):
            if isinstance(l, dict) and l.get("url", "").startswith(("http://", "https://")):
                lk = normalize_link(l["url"], l.get("name"))
                if lk["url"] not in seen:
                    seen.add(lk["url"])
                    lk["inherited"] = cur is not task
                    out.append(lk)
        cur = byid.get(cur.get("parentTaskId"))
    return out


def involvement(tcard, ids):
    """這個人在這張 Taiga 卡上的角色:"assigned" / "watch" / None。"""
    if not tcard:
        return None
    if any(u["id"] in ids for u in tcard.get("assigned") or []):
        return "assigned"
    if any(u["id"] in ids for u in tcard.get("watchers") or []):
        return "watch"
    return None


def comparable(links, cards, ids):
    """從一列的 Taiga 連結挑出能拿來跟這人 PMO 狀態比的,回傳 (links, cards)。
    頁面警示、重抓對齊、template.html 的 remismatch() 三處共用這把尺,改一處要三處一起改。

    - Epic 一律不比:它是整個需求的容器,裡面有別人的工作,負責人只是需求 owner,
      他一個人推不動 Epic 的進度(2026-10-16 使用者指出:John被標交測對不上 Epic Pending)。
    - 只看這人被 ASSIGNED 的(例:Nina 粉絲團模組那張掛了別人的 [APP] 後端 task,不能拿來卡她)。
    - 他被指派的只有 Epic 時就一張都不比 —— 剩下的都是別人的卡。
      完全沒被指派(多半是 Taiga 對不到人)才退回比全部非 Epic 的。
    """
    pairs = [(l, c) for l, c in zip(links, cards) if c]
    assigned = [involvement(c, ids) == "assigned" for _, c in pairs]
    pick = [(l, c) for (l, c), a in zip(pairs, assigned) if a and c.get("kind") != "epic"]
    if not pick and not any(assigned):
        pick = [(l, c) for l, c in pairs if c.get("kind") != "epic"]
    return [l for l, _ in pick], [c for _, c in pick]


# ---------- 重抓時順手把 PMO 對齊 Taiga ----------
# Taiga 階級 -> PMO 狀態名稱。只列「意思明確」的:1 進行中、3 交測、4 待上線、5 完成。
# 0(New/Pending)不必往前推;2(Needs info)對 PMO 是「待RD修正」還是「進行中」說不準 —— 都不碰。
SYNC_TARGET = {1: "進行中", 3: "交測", 4: "待上線", 5: "完成"}
SYNC_GAP = 2   # 跟 TG.compare 標「對不上」的門檻一樣:只修頁面上會亮警示的那些


def sync_from_taiga(op, tasks, byid, people, win_s, win_e, tg, pids, apply, taiga_users=None):
    """把「Taiga 明顯跑在前面」的 PMO 分工卡狀態推上去。2026-10-10 使用者要求重抓資料時順便做。

    只在判斷得出來時才改,其餘一律不動:
      - 只看名單上的人、落在四週視窗內、**自己掛著 Taiga 連結**的分工卡。
        從母卡繼承來的連結不算 —— 母卡常掛整個需求的好幾張 Taiga 卡,分不出是哪張分工的。
      - 只改葉節點(底下沒子卡的),母卡/容器卡的狀態本來就沒人維護。
      - 同一張分工掛多張 Taiga 卡時,只看 comparable() 挑出來的那幾張,取最落後的那張(寧可少推);
        任一張撈不到、已取消、或狀態不在對照表裡 -> 不動。
      - 只往前推,不往回拉;PMO 是「暫停」的不動;PMO 跑在 Taiga 前面的情況也不動。
      - PMO 專案裡找不到同名狀態 -> 不動。
    apply=False 只回傳清單不寫入(--sync-dry-run)。改了會在 PMO 卡上留一則留言說明來源。
    回傳 [{task, name, assignee, frm, to, why, ok}]。
    """
    parents = {t.get("parentTaskId") for t in tasks if t.get("parentTaskId")}
    st_cache, out = {}, []
    for t in tasks:
        if t.get("assigneeName") not in people or t["id"] in parents:
            continue
        if not W.overlaps(t.get("startDate"), t.get("endDate"), win_s, win_e):
            continue
        own = [normalize_link(l["url"], l.get("name")) for l in (t.get("links") or [])
               if isinstance(l, dict) and l.get("url", "").startswith(("http://", "https://"))]
        # Epic 先照網址剔掉,不等 comparable():撈不到的 Epic 不該擋住下面「任一張撈不到就不動」
        own = [l for l in own if l["kind"] == "taiga"
               and (TG.parse_url(l["url"]) or (None, None, None))[1] != "epic"]
        if not own:
            continue
        cards = [tg.card(l["url"]) for l in own]
        if any(c is None for c in cards):
            continue
        ids = tg.user_ids(t["assigneeName"], (taiga_users or {}).get(t["assigneeName"]))
        own, cards = comparable(own, cards, ids)
        if not cards or any(c["cancelled"] or c["rank"] is None for c in cards):
            continue
        cur_team = W.team_status(t)
        cur = STATUS_RANK.get(cur_team)
        if cur is None or cur_team == "暫停":
            continue
        k = min(range(len(cards)), key=lambda i: cards[i]["rank"])
        target = cards[k]["rank"]
        if target - cur < SYNC_GAP or target not in SYNC_TARGET:
            continue
        pid = pids.get(t.get("_proj"))
        if not pid:
            continue
        if pid not in st_cache:
            st_cache[pid] = {x["name"]: x for x in W.api(op, f"/api/projects/{pid}/statuses").get("statuses", [])}
        want = st_cache[pid].get(SYNC_TARGET[target])
        if not want:
            continue
        why = f"Taiga {own[k]['tag']} 已到「{cards[k]['status']}」"
        row = {"task": t["id"], "name": t.get("name") or "", "assignee": t.get("assigneeName"),
               "frm": t.get("statusLabel") or "", "to": want["name"], "why": why, "ok": None}
        if apply:
            try:
                W.api(op, f"/api/projects/{pid}/tasks/{t['id']}", "PATCH", {"status": want["key"]})
                W.api(op, f"/api/projects/{pid}/tasks/{t['id']}/comments", "POST",
                      {"content": f"週報重抓資料時自動對齊 Taiga:{row['frm']} → {want['name']}({why})。"})
                # 就地改掉,後面 rollup 算出來的就是新狀態,頁面不會還亮「對不上」
                t["statusLabel"], t["statusCategory"] = want["name"], want.get("category")
                t["status"], t["statusId"] = want["key"], want["id"]
                row["ok"] = True
            except SystemExit as e:      # W.api 失敗是 die() -> SystemExit;一筆失敗不能讓整份報表產不出來
                row["ok"] = False; row["err"] = str(e)
        out.append(row)
    return out


# ---------- 組資料 ----------
def kind_of(done, overdue, status, end, today):
    """卡片顏色。優先序:逾期 > 完成 > 快到期 > 進行中 > 待進行。"""
    if overdue:
        return "late"
    if done:
        return "done"
    if end:
        d = (date.fromisoformat(end) - date.fromisoformat(today)).days
        if 0 <= d <= SOON_DAYS:
            return "soon"
    return "doing" if status == "Doing" else "wait"


def project_urls(op):
    """專案 key -> (甘特圖網址, projectId)。網址不含 ?task=,聚焦參數由 focus_url() 補。

    任務序列化沒吐 projectId,得自己拿 /api/projects 兜 key -> id。

    id 也要一起回傳:網頁上要改狀態/完成日時,前端得知道 PATCH 打到哪個專案。
    以前只吐網址,前端只能從 `r.pmo` 的網址裡把 id 正則挖出來 —— 那能動,
    但把「網址長什麼樣」變成了資料契約的一部分,改版面就會壞。所以明確吐一個 pid。
    """
    urls, ids = {}, {}
    for pr in W.api(op, "/api/projects"):
        if pr.get("key"):
            urls[pr["key"]] = f"{PMO_BASE}/projects/{pr['id']}/gantt"
            ids[pr["key"]] = pr["id"]
    return urls, ids


def focus_url(base, root_id):
    """專案甘特圖網址 + `?task=<母任務>` = 只看這個需求的視圖。

    PMO 2026-08-31 起支援 `?task=`(gantt/+page.svelte 讀 query 後跑 focusSubtree),
    所以連過去就只剩這個需求及其子任務,不必再到站上用搜尋框找。
    給**母任務(root)**而不是分工子卡:給子卡只會看到那一支,看不到整件事的分工。

    兩個已知邊界(要改文案前先知道):
    - 聚焦時 PMO 會刻意忽略對方存檔的篩選(否則他存的 dateFrom /「只看我的」會把
      整棵樹濾掉、變成空圖),所以連結不受收件人的篩選設定影響。
    - 收件人沒登入時會被丟到 /login **且原網址不保留**,要登入後再點一次。
      抽屜裡的手動步驟因此留著當備援。
    """
    if not base or not root_id:
        return base
    return f"{base}?task={quote(str(root_id), safe='')}"


def subtask_rows(root_id, kids, byid):
    """某個需求底下**所有人**的分工(不只本人)。卡片上只列本人的,drawer 要列全部——
    「這件事還卡在誰身上」是主管最想知道的,只看本人的就答不出來。"""
    out, stack = [], list(kids.get(root_id, []))
    while stack:
        tid = stack.pop(0)
        t = byid.get(tid)
        if not t:
            continue
        out.append({
            "name": t.get("name") or "",
            "assignee": t.get("assigneeName") or "",
            "status": t.get("statusLabel") or "",
            "start": t.get("startDate") or "", "end": t.get("endDate") or "",
        })
        stack.extend(kids.get(tid, []))
    return out


def search_term(root_name, subs, all_names, person):
    """算出「貼到 PMO 搜尋框能讓最多分工一起出現」的字串。

    PMO 的 applyGanttTaskTreeFilter 保留的是「子樹裡有命中的」,不是「命中的子樹」——
    所以用完整需求名搜,只會留下母任務孤零零一列、展不開(2026-08-31 拿正式碼實測)。
    要讓子任務跟著出現,搜尋字串必須命中「子任務」本身。

    候選有兩類:(1) 需求名的連續片段——去掉〔政策〕這種前綴後常常就是分工的共同字;
    (2) 這個人自己那幾筆分工的完整名稱——(1) 全軍覆沒時的保底(例如「Lend Zone」的分工
    叫「CMS 後端」「官網 前端」,跟需求名一個字都沒重疊)。
    除了命中幾條自家分工,也要看它在整個專案裡誤中多少無關任務。

    回傳 (字串, 命中幾條分工, 誤中幾列);都不堪用時回傳 (None, 0, 0)。
    """
    names = [t["name"] for t in subs]
    if not names:
        return root_name, 0, 0
    inside = set(names) | {root_name}
    NOISE_CAP = 20      # 超過就是「官網」那種撈出一整片的東西

    def score(c):
        hit = sum(1 for n in names if c in n)
        noise = sum(1 for n in all_names if c in n and n not in inside)
        return hit, noise

    cands = []
    for i in range(len(root_name)):
        for j in range(i + 4, len(root_name) + 1):
            c = root_name[i:j].strip(" 　")
            if c[:1] in "])】）_-：:,、" or c[-1:] in "([【（_-：:,、":
                continue
            if any(c.count(a) != c.count(b) for a, b in ("[]", "()", "【】", "（）")):
                continue
            if len(c) >= 4:
                cands.append(c)
    cands += [t["name"] for t in subs if t["assignee"] == person]   # 保底

    best, bh, bn = None, 0, 0
    for c in dict.fromkeys(cands):
        h, nz = score(c)
        if h < 1 or nz > NOISE_CAP:
            continue
        if h > bh or (h == bh and (nz, -len(c)) < (bn, -len(best or ""))):
            best, bh, bn = c, h, nz
    return best, bh, bn


DEAD_MAX = 10


def prune_dead_links(op, tasks, byid, people, win_s, win_e, tg, pids, apply):
    """把 Taiga 上已不存在(404)的連結從 PMO 卡拿掉。2026-10-14 使用者要求重抓時順便做。

    範圍跟頁面一樣:名單上的人、落在四週視窗內的卡,以及它們的祖先卡(連結多半掛在母卡)。
    只認 tg.dead(明確 404),連不上、逾時一律不動 —— 不然 Taiga 一掛,全部連結會被清光。
    一次超過 DEAD_MAX 條也全部不動,當成 Taiga 那邊出了異常,留給人看。
    apply=False 只回傳清單不寫入。回傳格式同 sync_from_taiga,頁面上的「已自動對齊」一起列。
    """
    owners = {}
    for t in tasks:
        if t.get("assigneeName") not in people:
            continue
        if not W.overlaps(t.get("startDate"), t.get("endDate"), win_s, win_e):
            continue
        cur = t
        while cur and cur["id"] not in owners:
            owners[cur["id"]] = cur
            cur = byid.get(cur.get("parentTaskId"))

    hits = []
    for o in owners.values():
        dead = []
        for l in o.get("links") or []:
            if not (isinstance(l, dict) and l.get("url", "").startswith(("http://", "https://"))):
                continue
            lk = normalize_link(l["url"], l.get("name"))
            if lk["kind"] == "taiga":
                tg.card(lk["url"])
                if lk["url"] in tg.dead:
                    dead.append((l, lk["tag"]))
        if dead:
            hits.append((o, dead))

    total = sum(len(d) for _, d in hits)
    if total > DEAD_MAX:
        print(f"[提醒] Taiga 回報 {total} 條連結不存在,超過 {DEAD_MAX} 條,疑似 Taiga 異常,這次不刪。",
              file=sys.stderr)
        return []

    out = []
    for o, dead in hits:
        tags = "、".join(tag for _, tag in dead)
        row = {"task": o["id"], "name": o.get("name") or "", "assignee": o.get("assigneeName") or "",
               "frm": tags, "to": "移除連結", "why": "Taiga 上已不存在", "ok": None}
        pid = pids.get(o.get("_proj"))
        if apply and pid:
            gone = [id(l) for l, _ in dead]
            keep = [l for l in o["links"] if id(l) not in gone]
            try:
                # links 是整批覆蓋:送回去的必須是剩下的全部,不是只送要刪的
                W.api(op, f"/api/projects/{pid}/tasks/{o['id']}", "PATCH", {"links": keep})
                W.api(op, f"/api/projects/{pid}/tasks/{o['id']}/comments", "POST",
                      {"content": f"週報重抓資料時自動移除 Taiga 上已不存在的連結:{tags}。"})
                o["links"] = keep
                row["ok"] = True
            except SystemExit as e:
                row["ok"] = False; row["err"] = str(e)
        elif apply:
            row["ok"] = False; row["err"] = "找不到 PMO 專案 id"
        out.append(row)
    return out


def build_data(op, people, weeks, today, tg=None, taiga_users=None, sync=None):
    """一件事(同一人、同一根任務)只算一組日期與狀態 —— 涵蓋整個四週視窗。

    早期版本是「每週各自 rollup」,同一件事在上週和本週會出現不同的 end 和狀態
    (因為每週只看落在那週的子任務),並排看非常困惑。現在各週只決定「出不出現」,
    日期與狀態一律用整體的,卡片左上的跨週條負責表達它橫跨哪幾週。
    """
    tasks, byid, _ = W.load_all_tasks(op)
    purl, pids = project_urls(op)
    synced = []
    if tg and sync:
        synced = sync_from_taiga(op, tasks, byid, people, weeks[0][1], weeks[-1][2],
                                 tg, pids, apply=(sync == "apply"), taiga_users=taiga_users)
        synced += prune_dead_links(op, tasks, byid, people, weeks[0][1], weeks[-1][2],
                                   tg, pids, apply=(sync == "apply"))
    kids = {}
    for t in tasks:
        kids.setdefault(t.get("parentTaskId") or "", []).append(t["id"])
    all_names = [t.get("name") or "" for t in tasks]
    win_s, win_e = weeks[0][1], weeks[-1][2]
    month_ago = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    out_people = []

    for p in people:
        tg_ids = tg.user_ids(p, (taiga_users or {}).get(p)) if tg else set()
        if tg and not tg_ids:
            print(f"[提醒] Taiga 上對不到「{p}」,他的母卡 Taiga 連結不做篩選。"
                  f"可在 people.json 的 taiga_users 指定帳號。", file=sys.stderr)
        # 母卡(底下有子卡的)負責人代表 owner,不是工作量:web 的需求母卡一律掛 leader,
        # 實際做的人在開發子卡上(2026-10-10 使用者說明)。所以有子卡的母卡不算進這人手上,
        # 沒子卡的母卡(需求本身就是一張工作卡)照算。
        mine = [t for t in tasks if t.get("assigneeName") == p and t["id"] not in kids
                and W.overlaps(t.get("startDate"), t.get("endDate"), win_s, win_e)]
        rows = []
        for g in W.rollup(mine, byid, today):
            mytasks = [t for t in mine if W.root_of(t, byid)["id"] == g["root_id"]]
            # 還沒到的週(下週、下下週)只看沒做完的卡:交測/完成了就是 RD 這端結束,
            # 結束日排得比實際晚(例:提早完成、或重抓時自動對齊成完成)也不該佔著未來的週。
            span = [any(W.overlaps(t.get("startDate"), t.get("endDate"), ws, we)
                        and (ws <= today or W.team_status(t) not in DONE)
                        for t in mytasks) for _, ws, we in weeks]
            done = g["status"] in DONE
            overdue = bool(g["end"] and g["end"] < today and not done)
            lks = {}
            for t in mytasks:
                for lk in links_of(t, byid):
                    old = lks.get(lk["url"])
                    # 同一張卡,某張子卡自己掛了、另一張是從母卡繼承的 -> 算自己掛的
                    if old is None or (old["inherited"] and not lk["inherited"]):
                        lks[lk["url"]] = lk
            lks = list(lks.values())
            rank = STATUS_RANK.get(g["status"])
            worst = None
            tg_hidden = 0
            hidden = []
            if tg:
                kept = []
                for lk in lks:
                    if lk.get("kind") == "taiga":
                        # tg.card 回的是快取共用物件,每人各自的資訊(mine)放在 lk 上,不要寫進去
                        lk["taiga"] = tg.card(lk["url"])
                        lk["mine"] = involvement(lk["taiga"], tg_ids)
                        # 母卡繼承來、而這人在 Taiga 上既非 assigned 也沒 watch -> 不是他的,拿掉。
                        # 撈不到卡或對不到人時保留(寧可多列,不要讓真的相關的卡消失)。
                        if (lk["inherited"] and lk["taiga"] and tg_ids and not lk["mine"]):
                            tg_hidden += 1
                            hidden.append(lk)
                            continue
                    kept.append(lk)
                lks = kept
                tgl = [lk for lk in lks if lk.get("kind") == "taiga"]
                judge, _ = comparable(tgl, [lk["taiga"] for lk in tgl], tg_ids)
                for lk in judge:
                    lvl, txt = TG.compare(rank, lk["taiga"])
                    # 差距記在「這條連結」上,不只記在整列:一個需求可能掛兩張 Taiga 卡
                    # 而且狀態不同(實測有這種列),只留一個 row 層級的 worst 會標錯張。
                    if lvl:
                        lk["mis"] = {"level": lvl, "text": txt}
                    if lvl and worst is None:
                        worst = {"level": lvl, "text": txt}
            subs = subtask_rows(g["root_id"], kids, byid)
            root_t = byid.get(g["root_id"]) or {}
            st_term, st_hits, _st_noise = search_term(g["name"], subs, all_names, p)
            # PMO 首次開啟該專案會把起日預設成一個月前;比這更早的分工會被濾掉
            earliest = min([t["start"] for t in subs if t["start"]] or [g["start"] or today])
            others = sorted({t["assignee"] for t in subs if t["assignee"] and t["assignee"] != p})
            rows.append({
                "id": g["root_id"],
                "name": g["name"],
                "subtasks": subs,
                # 母卡本身(需求那一張)的值:抽屜分工表第一列,頁面上也能改它的狀態
                "root": {"assignee": root_t.get("assigneeName") or "",
                         "status": root_t.get("statusLabel") or "",
                         "end": root_t.get("endDate") or ""},
                "search": st_term, "search_hits": st_hits,
                "howto": {
                    # 這個人自己的分工名不含搜尋字串時,留著 PMO 預設的「只看我的」
                    # 會兩個條件同時成立不了 -> 篩出來是空的。必須叫他清掉負責人。
                    "must_clear_assignee": not any(
                        st_term and st_term in t["name"]
                        for t in subs if t["assignee"] == p),
                    "root_unassigned": not (root_t.get("assigneeName") or ""),
                    "early": earliest < month_ago,
                    "earliest": earliest,
                    "others": others,
                },
                "mismatch": worst,
                "tg_hidden": tg_hidden,   # 母卡上有、但這人不在 assigned/watch 而被拿掉的 Taiga 卡數
                # 2026-10-20 使用者要求:這人一張 Taiga 都沒留下時,把母卡的列在下面參考。
                # 不放進 links —— 他不在那些卡上,拿來比對進度只會亮假警示。
                "tg_parent": [] if any(lk.get("kind") == "taiga" for lk in lks) else hidden,
                "status": SHOW.get(g["status"], g["status"]),
                "kind": kind_of(done, overdue, g["status"], g["end"], today),
                "done": done, "overdue": overdue,
                "start": g["start"], "end": g["end"],
                "parts": g["parts"], "links": lks, "proj": g["proj"],
                "pid": pids.get(g["proj"], ""),   # 前端改 PMO 時要打的專案 id
                "pmo": focus_url(purl.get(g["proj"], ""), g["root_id"]),
                "span": span,
            })

        per_week = {}
        for i, (label, _, _) in enumerate(weeks):
            per_week[label] = [r for r in rows if r["span"][i]]

        # 下週/下下週的「未完成」只是「還沒開始」,不進 badge 計數
        counted = [r for r in rows if any(r["span"][i] for i, (l, _, _) in enumerate(weeks)
                                          if l in COUNTED)]
        out_people.append({
            "name": p, "weeks": per_week,
            "open": len([r for r in per_week.get("本週", []) if not r["done"]]),
            "overdue": len([r for r in counted if r["overdue"]]),
            "soon": len([r for r in counted if r["kind"] == "soon"]),
        })
    return out_people, synced


# ---------- 產出 ----------
def render(cfg, people_data, weeks, generated, synced=None):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    payload = {
        "title": cfg.get("title") or "PMO 週報",
        "author": cfg.get("author") or "",     # 側欄署名;留空就不顯示那一行
        "leader": cfg.get("leader") or "",     # 組長:側欄單獨一區排最上面
        "nav_names": cfg.get("nav_names") or {},  # 側欄顯示名(只有側欄用)
        "order": load_order(),                 # 項目順序(serve.py 管寫入,這裡只嵌一份當初值)
        "notion_url": cfg.get("notion_url") or "https://www.notion.so/your-weekly-report-page",
        "generated": generated,
        "weeks": [{"label": l, "start": s, "end": e} for l, s, e in weeks],
        "people": people_data,
        # 這次重抓自動對齊 Taiga 的 PMO 卡(只放真的寫成功的),側欄顯示一行讓人知道發生過
        "synced": [x for x in (synced or []) if x.get("ok")],
        # 抽屜存檔後前端要照同一把尺重算卡片;常數從這裡送過去,兩邊才不會各改各的
        "rules": {"done": sorted(DONE), "show": SHOW, "rank": STATUS_RANK, "soon_days": SOON_DAYS,
                  "tg_rank": TG.TAIGA_RANK, "tg_cancelled": sorted(TG.CANCELLED)},
    }
    blob = json.dumps(payload, ensure_ascii=False)
    # 內嵌 JSON 不能出現 </script>,也順手擋掉 <!-- 這種會提早結束的序列
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (tpl.replace("__DATA__", blob)
               .replace("__TITLE__", payload["title"])
               .replace("__GENERATED__", generated)
               .replace("__AUTHOR__", payload["author"]))


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--anchor", help="基準日(YYYY-MM-DD),該日所在那週當「本週」")
    ap.add_argument("--people", help="這次只看這幾人,逗號分隔(不動名單)")
    ap.add_argument("--add", help="加人進 people.json,逗號分隔")
    ap.add_argument("--remove", help="從 people.json 減人,逗號分隔")
    ap.add_argument("--list", action="store_true", help="列出目前名單")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--serve", nargs="?", const="8099", help="產完起 http server(預設 8099)")
    ap.add_argument("--open", action="store_true", help="產完用瀏覽器打開")
    ap.add_argument("--no-taiga", action="store_true", help="不抓 Taiga 狀態(離線或趕時間時)")
    ap.add_argument("--no-sync", action="store_true", help="不要把 PMO 對齊 Taiga(預設會對齊判斷得出來的)")
    ap.add_argument("--sync-dry-run", action="store_true", help="只列出會對齊哪些 PMO 卡,不寫入")
    args = ap.parse_args()

    cfg = load_people()

    if args.list:
        print(f"名單({len(cfg['people'])} 人):" + "、".join(cfg["people"]))
        return

    if args.add or args.remove:
        op = W.connect()
        _, _, all_names = W.load_all_tasks(op)
        if args.add:
            want = [x.strip() for x in args.add.split(",") if x.strip()]
            ok, bad = resolve(want, all_names)
            if bad:
                sys.exit(f"[錯誤] PMO 上找不到:{'、'.join(bad)}(名單沒動)")
            for n in ok:
                if n in cfg["people"]:
                    print(f"  已在名單:{n}")
                else:
                    cfg["people"].append(n); print(f"  + {n}")
        if args.remove:
            for raw in [x.strip() for x in args.remove.split(",") if x.strip()]:
                hit = [n for n in cfg["people"] if n == raw or raw.lower() in n.lower()]
                if not hit:
                    print(f"  名單裡沒有:{raw}")
                for n in hit:
                    cfg["people"].remove(n); print(f"  - {n}")
        save_people(cfg)
        print(f"名單({len(cfg['people'])} 人):" + "、".join(cfg["people"]))
        return

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    weeks = four_weeks(anchor)
    today = date.today().isoformat()

    op = W.connect()
    if args.people:
        _, _, all_names = W.load_all_tasks(op)
        want = [x.strip() for x in args.people.split(",") if x.strip()]
        people, bad = resolve(want, all_names)
        if bad:
            sys.exit(f"[錯誤] PMO 上找不到:{'、'.join(bad)}")
    else:
        people = list(cfg["people"])
    if not people:
        sys.exit("[錯誤] 名單是空的。用 --add 加人。")

    generated = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
    tg = None
    if not args.no_taiga:
        tg = TG.Taiga()
        if not tg.connect():
            print(f"[提醒] {tg.reason}", file=sys.stderr)
            tg = None
    sync = None if args.no_sync else ("dry" if args.sync_dry_run else "apply")
    data, synced = build_data(op, people, weeks, today, tg, cfg.get("taiga_users"), sync)
    html = render(cfg, data, weeks, generated, synced)

    out = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    mism = [(p["name"], r) for p in data for rows in p["weeks"].values()
            for r in rows if r.get("mismatch")]
    seen_m, uniq_m = set(), []
    for who, r in mism:
        k = (who, r["name"])
        if k not in seen_m:
            seen_m.add(k); uniq_m.append((who, r))
    tot_open = sum(p["open"] for p in data)
    tot_late = sum(p["overdue"] for p in data)
    print(f"已產出 {out}")
    if synced:
        head = "會對齊(dry-run,未寫入)" if sync == "dry" else "已對齊 Taiga"
        print(f"  {head} {len(synced)} 筆:")
        for x in synced:
            mark = "" if x["ok"] is not False else f"  [失敗] {x.get('err', '')}"
            print(f"    {x['assignee']:<14}{x['name']}:{x['frm']} → {x['to']}({x['why']}){mark}")
    print(f"  {len(people)} 人 · 本週在手 {tot_open} 件 · 逾期 {tot_late} 件 · 基準週 {weeks[1][1]}~{weeks[1][2]}")
    for p in data:
        flag = f"  逾期 {p['overdue']}" if p["overdue"] else ""
        print(f"    {p['name']:<18}本週在手 {p['open']}{flag}")
    if uniq_m:
        print(f"\n  PMO 與 Taiga 對不上 {len(uniq_m)} 筆:")
        for who, r in uniq_m:
            print(f"    {who:<18}{r['name']}\n      -> {r['mismatch']['text']}")

    if args.open:
        webbrowser.open("file://" + out)
    if args.serve:
        import http.server, socketserver, functools
        port = int(args.serve)
        d, fn = os.path.dirname(out), os.path.basename(out)
        h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=d)
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", port), h) as httpd:
            url = f"http://127.0.0.1:{port}/{fn}"
            print(f"\n伺服器啟動:{url}   (Ctrl-C 停止)")
            if args.open:
                webbrowser.open(url)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")


if __name__ == "__main__":
    main()
