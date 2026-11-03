#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PMO 週報工具 — 從公司內網 PMO 排程系統抓成員任務,產出終端週報,或填 WebTeam 的 Notion 週報。

三種模式:
  # 1) 終端週報(預設)
  python3 weekly.py                       # 預設小組、本週
  python3 weekly.py --week last           # 上週
  python3 weekly.py --week 2026-08-10     # 含該日的那一週
  python3 weekly.py --start 2026-08-10 --end 2026-08-14   # 自訂區間(蓋過 --week)
  python3 weekly.py --people "John Lee,Kevin Huang"   # 只抓這幾人(取代名單)
  python3 weekly.py --add "lily,kevin"     # 名單再加人(臨時)
  python3 weekly.py --detail              # 展開每列底下實際的母/子任務(對帳用)
  python3 weekly.py --by-status           # 依狀態分組
  python3 weekly.py --overdue             # 只列逾期
  python3 weekly.py --json                # 原始 JSON

  # 2) 名單管理(加人/減人 —— 改的是 roster.json)
  python3 weekly.py roster                          # 列出目前對應
  python3 weekly.py roster --add "Amy=Amy Chang"   # 加(Notion中文名=PMO名)
  python3 weekly.py roster --add "lily"             # 加(Notion名預設同PMO名)
  python3 weekly.py roster --remove "Amy"           # 減(用 Notion 名或 PMO 名)

  # 3) 產生 WebTeam Notion 週報內容(給 Claude 用 MCP 寫進當週那頁)
  python3 weekly.py notion --start 2026-07-24 --end 2026-07-31
  #   stdout = Notion-flavored Markdown(整頁內容);stderr = 週次/各人筆數/逾期清單

設計筆記見 SKILL.md。核心三原則:進度看狀態不看%、當週用時間軸交集、母子任務 roll-up 成一列。
"""
import argparse, json, os, subprocess, sys, urllib.request, urllib.error, http.cookiejar
from datetime import date, datetime, timedelta

BASE = "https://pmo.example.com"
KEYCHAIN_SERVICE = "pmo-toolkit"
PRIMARY_PROJECT_KEY = "SCRUM"  # PMO 主專案(key=SCRUM/name=PMO);此專案任務不標專案代號
ROSTER_PATH = os.path.join(os.path.dirname(__file__), "..", "roster.json")

ALIASES = {"bolin": "bolin", "alex": "Amy Chang", "amy": "Amy Chang"}

# 團隊用詞的「進度排序」。roll-up 取最小值(=最不進階者),寧可低報不高報。
STATUS_RANK = {"待進行": 0, "Doing": 1, "待RD修正": 2, "UAT": 3, "待上線": 4, "Done": 5}
TEAM_EMOJI = {"Done": "✅", "待上線": "🚀", "UAT": "📤", "Doing": "🔨",
              "待RD修正": "⛔", "暫停": "⏸", "待進行": "⬜"}


def die(msg, code=1):
    print(f"[錯誤] {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- config ----------
def load_roster():
    with open(ROSTER_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_roster(cfg):
    with open(ROSTER_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

def roster_pmo_names(cfg):
    """名單裡實際要抓的 PMO 名字(排除留白區塊)。"""
    return [e["pmo"] for e in cfg["roster"] if e.get("pmo") and not e.get("leave_blank")]


# ---------- credentials / http ----------
def get_credentials():
    try:
        pw = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL).decode().rstrip("\n")
    except subprocess.CalledProcessError:
        die(f"Keychain 找不到 service={KEYCHAIN_SERVICE} 的密碼。\n"
            f"       請先:security add-generic-password -U -s {KEYCHAIN_SERVICE} -a 你的Email -w '你的密碼'")
    user = None
    try:
        meta = subprocess.check_output(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
                                       stderr=subprocess.DEVNULL).decode()
        for line in meta.splitlines():
            if '"acct"' in line and "=" in line:
                user = line.split("=", 1)[1].strip().strip('"')
    except subprocess.CalledProcessError:
        pass
    user = os.environ.get("PMO_USER", user)
    if not user:
        die("找不到 PMO 帳號,請在 Keychain 的 account 欄位設定 Email,或用 PMO_USER 指定。")
    return user, pw


def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def api(opener, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"{method} {path} 失敗 (HTTP {e.code}): {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        die(f"連不到 {BASE} — {e.reason}。是否在公司內網/VPN?")


def connect():
    user, pw = get_credentials()
    op = make_opener()
    api(op, "/api/auth/login", "POST", {"username": user, "password": pw})
    return op


def load_all_tasks(op):
    """回傳(全部任務 list, id->task 對照, all_assignee_names set)。"""
    projects = api(op, "/api/projects")
    key_of = {p["id"]: p.get("key", "?") for p in projects}
    byid, tasks = {}, []
    for p in projects:
        for t in api(op, f"/api/projects/{p['id']}/tasks").get("tasks", []):
            t["_proj"] = key_of.get(p["id"], "?")
            byid[t["id"]] = t
            tasks.append(t)
    names = {t.get("assigneeName") for t in tasks if t.get("assigneeName")}
    return tasks, byid, names


# ---------- shared helpers ----------
def week_range(spec):
    if spec in (None, "current", "this", "本週", "當週"):
        anchor = date.today()
    elif spec in ("last", "上週", "上周"):
        anchor = date.today() - timedelta(days=7)
    else:
        try:
            anchor = datetime.strptime(spec, "%Y-%m-%d").date()
        except ValueError:
            die(f"--week 格式錯誤:{spec}(用 current / last / YYYY-MM-DD)")
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6)

def overlaps(s, e, a, b):
    if not s and not e:
        return False
    s = s or e
    e = e or s
    return s <= b and e >= a

def resolve_people(requested, all_names):
    lower = {n.lower(): n for n in all_names}
    resolved, unknown = [], []
    for raw in requested:
        name = (raw or "").strip()
        if not name:
            continue
        name = ALIASES.get(name.lower(), name)
        if name in all_names:
            resolved.append(name); continue
        if name.lower() in lower:
            resolved.append(lower[name.lower()]); continue
        hits = [o for lo, o in lower.items() if name.lower() in lo or lo in name.lower()]
        if len(hits) == 1:
            resolved.append(hits[0])
        elif hits:
            print(f"[提醒]「{raw}」對到多人:{hits},全納入", file=sys.stderr); resolved.extend(hits)
        else:
            unknown.append(raw)
    if unknown:
        print(f"[提醒] 找不到、已略過:{unknown}", file=sys.stderr)
    seen, out = set(), []
    for n in resolved:
        if n not in seen:
            seen.add(n); out.append(n)
    return out


def team_status(t):
    """PMO 狀態 -> WebTeam 用詞。"""
    cat = t.get("statusCategory"); lab = (t.get("statusLabel") or "")
    if lab in ("完成", "已完成", "上線結案") or cat == "done":
        return "Done"
    if lab == "待上線":
        return "待上線"
    if lab == "暫停":
        return "暫停"
    if lab in ("交測", "測試中", "測試完成", "測試", "內測", "待測試", "待交測", "UAT") or cat == "review":
        return "UAT"
    if lab in ("待RD修正",) or cat == "blocked":
        return "待RD修正"
    if lab in ("進行中", "開發中", "Doing", "In Progress") or cat == "in_progress":
        return "Doing"
    return "待進行"


# ---------- roll-up:母子任務併成一列 ----------
def root_of(t, byid):
    """往上找到根任務(母任務沒被撈到、或有環時,就地停住)。"""
    cur, seen = t, {t["id"]}
    while cur.get("parentTaskId"):
        p = byid.get(cur["parentTaskId"])
        if not p or p["id"] in seen:
            break
        seen.add(p["id"]); cur = p
    return cur


def group_status(items):
    """一組(同一根任務、同一人)任務 -> 一個代表狀態。

    1) 母任務的狀態是「容器狀態」,PMO 上沒人回頭維護(實測:子任務已交測,母任務還掛進行中)。
       只要這組裡有它的子任務,就丟掉母任務那筆的狀態。
    2) 暫停不參與比較,除非整組都暫停/完成。
    3) 已完成不參與比較,除非整組都完成 —— 一件事只有全部做完才算 Done。
    4) 其餘取「最不進階」的那個(瓶頸),寧可低報不高報。
    """
    parents = {t.get("parentTaskId") for t in items if t.get("parentTaskId")}
    leaves = [t for t in items if t["id"] not in parents]
    pool = leaves or items
    sts = [team_status(t) for t in pool]
    live = [s for s in sts if s not in ("暫停", "Done")]
    if live:
        return min(live, key=lambda s: STATUS_RANK.get(s, 0))
    return "暫停" if "暫停" in sts else "Done"


def rollup(person_tasks, byid, today):
    """把同一人、同一根任務底下的多筆任務併成一列。

    名稱取根任務名(母任務通常是需求本身,子任務只是 CMS後端/測試 這種分工詞)。
    日期取這個人自己那些任務的區間,不用母任務的 —— 母任務常涵蓋整個大專案。
    """
    groups = {}
    for t in person_tasks:
        r = root_of(t, byid)
        groups.setdefault(r["id"], {"root": r, "items": []})["items"].append(t)

    out = []
    for g in groups.values():
        items, root = g["items"], g["root"]
        starts = [d for d in (t.get("startDate") or t.get("endDate") for t in items) if d]
        ends = [d for d in (t.get("endDate") or t.get("startDate") for t in items) if d]
        st = group_status(items)
        end = max(ends) if ends else None
        out.append({
            "root_id": root["id"],
            "name": root.get("name") or "",
            "status": st,
            "start": min(starts) if starts else None,
            "end": end,
            "proj": items[0].get("_proj"),
            "overdue": bool(end and end < today and st != "Done"),
            "parts": [f"{t.get('name')}〔{t.get('statusLabel')}〕" for t in items
                      if t["id"] != root["id"]],
            "raw_status": " / ".join(t.get("statusLabel") or "?" for t in items),
            "n": len(items),
        })
    out.sort(key=lambda r: (r["start"] or "9999", r["name"]))
    return out


def md_date(d):
    return f"{int(d[5:7])}/{int(d[8:10])}" if d else ""

NMD_ESCAPE = ['\\', '*', '~', '`', '$', '[', ']', '<', '>', '{', '}', '|', '^']
def esc(s):
    s = s or ""
    for ch in NMD_ESCAPE:
        s = s.replace(ch, '\\' + ch)
    return s


# ---------- mode: roster ----------
def cmd_roster(argv):
    ap = argparse.ArgumentParser(prog="weekly.py roster")
    ap.add_argument("--add", help='"Notion名=PMO名" 或 "PMO名"')
    ap.add_argument("--remove", help="用 Notion 名或 PMO 名移除")
    a = ap.parse_args(argv)
    cfg = load_roster()

    if a.add:
        if "=" in a.add:
            notion, pmo = [x.strip() for x in a.add.split("=", 1)]
        else:
            notion = pmo = a.add.strip()
        # 驗證 PMO 名字存在(避免打錯字白填)
        op = connect()
        _, _, names = load_all_tasks(op)
        matched = resolve_people([pmo], names)
        if not matched:
            die(f"PMO 裡找不到成員「{pmo}」,沒有加入。用 weekly.py 看得到的 assignee 名稱為準。")
        pmo = matched[0]
        for e in cfg["roster"]:
            if e.get("pmo") == pmo or e.get("notion") == notion:
                die(f"「{notion}={pmo}」已存在(或同名衝突),未重複加入。")
        cfg["roster"].append({"notion": notion, "pmo": pmo})
        save_roster(cfg)
        print(f"已加入:{notion} = {pmo}(共 {len(cfg['roster'])} 個區塊)")
        return

    if a.remove:
        key = a.remove.strip()
        before = len(cfg["roster"])
        cfg["roster"] = [e for e in cfg["roster"]
                         if e.get("notion") != key and e.get("pmo") != key]
        if len(cfg["roster"]) == before:
            die(f"名單裡找不到「{key}」(Notion 名或 PMO 名都比對過)。")
        save_roster(cfg)
        print(f"已移除:{key}(剩 {len(cfg['roster'])} 個區塊)")
        return

    # list
    print(f"# {cfg.get('team_label','')} 週報名單({ROSTER_PATH})")
    for i, e in enumerate(cfg["roster"]):
        tag = "  (留白/本人)" if e.get("leave_blank") else ""
        print(f"  {i+1:>2}. {e['notion']:<16} = {e.get('pmo') or '—'}{tag}")


# ---------- mode: notion ----------
def build_notion_content(cfg, people_rows):
    """people_rows: list of (entry, rows)。回傳整頁 Notion-flavored Markdown。"""
    def table(entry, rows):
        four = entry.get("cols", 4) != 3
        head = ["項目名稱", "目前進度"] + (["預計 UAT 日期"] if four else []) + ["備註"]
        out = ['<table header-row="true">', "<tr>"] + [f"<td>{h}</td>" for h in head] + ["</tr>"]
        if not rows:  # 留白區塊:一列空白
            out += ["<tr>"] + ["<td></td>" for _ in head] + ["</tr>", "</table>"]
            return "\n".join(out)
        for r in rows:
            cells = [esc(r["name"]), esc(r["status"])] + ([esc(r["uat"])] if four else []) + [""]
            out += ["<tr>"] + [f"<td>{c}</td>" for c in cells] + ["</tr>"]
        out.append("</table>")
        return "\n".join(out)

    parts = []
    for i, (entry, rows) in enumerate(people_rows):
        if i >= 3:                      # 忠實原版:前三區塊不加分隔線,其後每區塊前加 ---
            parts.append("---")
        parts.append(f"### {entry['notion']}\n" + table(entry, rows))
    return "\n".join(parts)


def cmd_notion(argv):
    ap = argparse.ArgumentParser(prog="weekly.py notion")
    ap.add_argument("--start", help="YYYY-MM-DD 週次起(通常取當週那頁標題)")
    ap.add_argument("--end", help="YYYY-MM-DD 週次迄")
    a = ap.parse_args(argv)
    if a.start and a.end:
        ws, we = a.start, a.end
    else:
        m, s = week_range("current")
        ws, we = m.isoformat(), s.isoformat()

    cfg = load_roster()
    op = connect()
    tasks, byid, names = load_all_tasks(op)
    today = date.today().isoformat()

    people_rows, summary, overdue = [], [], []
    for entry in cfg["roster"]:
        if entry.get("leave_blank") or not entry.get("pmo"):
            people_rows.append((entry, []))  # 留白
            continue
        an = resolve_people([entry["pmo"]], names)
        an = an[0] if an else entry["pmo"]
        mine = [t for t in tasks
                if t.get("assigneeName") == an
                and overlaps(t.get("startDate"), t.get("endDate"), ws, we)]
        groups = rollup(mine, byid, today)
        rows = [{"name": g["name"], "status": g["status"],
                 "uat": md_date(g["end"]) if g["status"] != "Done" else ""} for g in groups]
        rows.sort(key=lambda r: r["name"])
        for g in groups:
            if g["overdue"]:
                overdue.append(f"{entry['notion']}({an}): {g['name']} — 原訂 {md_date(g['end'])}")
        people_rows.append((entry, rows))
        summary.append(f"{entry['notion']}({an}) {len(rows)} 筆")

    content = build_notion_content(cfg, people_rows)
    # stdout：純內容(給 Claude 貼進 MCP);stderr：摘要 + 逾期
    print(content)
    print(f"\n=== 週次 {ws} ~ {we} ===", file=sys.stderr)
    print("各區塊:" + " / ".join(summary), file=sys.stderr)
    if overdue:
        print(f"\n⚠️ 逾期(未寫進報告,{len(overdue)} 筆):", file=sys.stderr)
        for o in overdue:
            print("  - " + o, file=sys.stderr)


# ---------- mode: report (default) ----------
def cmd_report(argv):
    ap = argparse.ArgumentParser(prog="weekly.py")
    ap.add_argument("--week", default="current")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD,與 --end 併用可蓋過 --week")
    ap.add_argument("--end", default=None)
    ap.add_argument("--people", default=None)
    ap.add_argument("--add", default=None)
    ap.add_argument("--detail", action="store_true", help="展開每列底下實際的母/子任務")
    ap.add_argument("--by-status", action="store_true")
    ap.add_argument("--overdue", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_roster()
    op = connect()
    tasks, byid, names = load_all_tasks(op)
    if args.start and args.end:
        ws, we = args.start, args.end
    else:
        monday, sunday = week_range(args.week)
        ws, we = monday.isoformat(), sunday.isoformat()
    today = date.today().isoformat()

    roster = [n.strip() for n in args.people.split(",")] if args.people else roster_pmo_names(cfg)
    if args.add:
        roster += [n.strip() for n in args.add.split(",")]
    people = resolve_people(roster, names)
    if not people:
        die("沒有可對應的成員名單。")

    by_person = {}
    for person in people:
        mine = [t for t in tasks
                if t.get("assigneeName") == person
                and overlaps(t.get("startDate"), t.get("endDate"), ws, we)]
        gs = rollup(mine, byid, today)
        if args.overdue:
            gs = [g for g in gs if g["overdue"]]
        by_person[person] = gs
    total = sum(len(v) for v in by_person.values())

    if args.json:
        out = [dict(g, assignee=p) for p in people for g in by_person[p]]
        print(json.dumps({"week": [ws, we], "count": len(out), "items": out},
                         ensure_ascii=False, indent=2))
        return

    lines = [f"# 週報彙整　{ws} ~ {we}（{'逾期未完成' if args.overdue else '本週手上'} {total} 項）",
             "> 一項 = 一個母任務(子任務已併入)。進度看「狀態」,此系統 progress% 幾乎沒人填。"
             "〔非 PMO 專案會標專案代號〕\n"]

    def fmt(g, indent=""):
        d = f"{(g['start'] or '')[5:]}→{(g['end'] or '')[5:]}"
        tag = f"〔{g['proj']}〕" if g["proj"] != PRIMARY_PROJECT_KEY else ""
        od = "　⚠️逾期" if g["overdue"] else ""
        head = (f"{indent}- {TEAM_EMOJI.get(g['status'],'•')} **{g['status']}**　{d}　"
                f"{tag}{g['name']}{od}")
        if args.detail and g["parts"]:
            head += "".join(f"\n{indent}    ↳ {p}" for p in g["parts"])
        return head

    if args.by_status:
        from collections import defaultdict
        groups = defaultdict(list)
        for p in people:
            for g in by_person[p]:
                groups[g["status"]].append((p, g))
        for st in sorted(groups, key=lambda s: STATUS_RANK.get(s, -1)):
            g = sorted(groups[st], key=lambda pg: (pg[0], pg[1]["start"] or "9999"))
            lines.append(f"\n## {st}　—　{len(g)} 項")
            for p, item in g:
                lines.append(f"  ({p}) " + fmt(item, indent="  ")[4:])
    else:
        for person in people:
            gs = by_person[person]
            lines.append(f"\n## {person}　—　{len(gs)} 項")
            if not gs:
                lines.append("　（無逾期）" if args.overdue else "　（本週無任務）"); continue
            for g in gs:
                lines.append(fmt(g))
    print("\n".join(lines))


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "roster":
        cmd_roster(argv[1:])
    elif argv and argv[0] == "notion":
        cmd_notion(argv[1:])
    else:
        cmd_report(argv)


if __name__ == "__main__":
    main()
