#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讀 Taiga 卡的實際狀態,拿來跟 PMO 對照。

只讀不寫。認證與 taiga-card skill 共用同一組 Keychain 憑證(service=taiga-toolkit)。
Taiga 掛掉或某張卡撈不到時**一律安靜略過**,報表照樣要產得出來——
這是輔助資訊,不能讓它變成產報表的單點故障。
"""
import json, re, subprocess, sys, urllib.error, urllib.request

HOST = "https://taiga.example.com"
BASE = HOST + "/api/v1"
KEYCHAIN_SERVICE = "taiga-toolkit"
KEYCHAIN_ACCOUNT = "you@example.com"

# 卡片網址 -> (slug, 種類, ref)
URL_RE = re.compile(r"/project/([^/?#]+)/(us|epic|issue|task)/(\d+)")
ENDPOINT = {"us": "userstories", "epic": "epics", "issue": "issues", "task": "tasks"}

# Taiga 狀態 -> 與 PMO 同一把尺的階級(見 build.py 的 STATUS_RANK)。
# 這份對照是 2026-08-31 實際打 /userstory-statuses 等四個端點撈回來的,
# 兩個專案(scrum / bugji-you-hua)詞彙不完全一樣,兩邊的都收進來了。
TAIGA_RANK = {
    "new": 0, "pending": 0, "ready": 0, "sprint backlog": 0, "sprint planning": 0,
    "ready for refinement": 0, "postponed": 0,
    "in progress": 1, "breaking down": 1, "fixing": 1,
    "needs info": 2,
    "ready for test": 3, "review": 3,
    "validated": 4, "approved": 4, "ready to release": 4, "stage": 4,
    "done": 5, "production": 5, "closed": 5, "monitoring": 5,
}
# 這些是「不做了」,不是「做完了」,不參與進度比較
CANCELLED = {"cancelled", "cancelled1", "cancelled2", "rejected"}


def parse_url(url):
    m = URL_RE.search(url or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


class Taiga:
    def __init__(self):
        self.token = None
        self.slug_id = {}
        self.cache = {}
        self.dead = set()
        self.ok = False
        self.reason = ""
        self._users = None   # Taiga user id -> {"username", "name"}

    def _req(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method="POST" if body else "GET")
        r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        if self.token:
            r.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(r, timeout=20) as f:
            return json.loads(f.read().decode())

    def connect(self):
        try:
            pw = subprocess.check_output(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                 "-a", KEYCHAIN_ACCOUNT, "-w"], stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError:
            self.reason = (f"Keychain 沒有 {KEYCHAIN_SERVICE} 的密碼,略過 Taiga 狀態。\n"
                           f"       要啟用:security add-generic-password -s {KEYCHAIN_SERVICE} "
                           f"-a {KEYCHAIN_ACCOUNT} -w")
            return False
        try:
            self.token = self._req("/auth", {"type": "normal",
                                             "username": KEYCHAIN_ACCOUNT, "password": pw})["auth_token"]
            self.ok = True
            return True
        except Exception as e:
            self.reason = f"Taiga 登入失敗({e}),略過 Taiga 狀態。"
            return False

    def _pid(self, slug):
        if slug not in self.slug_id:
            try:
                self.slug_id[slug] = self._req(f"/projects/by_slug?slug={slug}")["id"]
            except Exception:
                self.slug_id[slug] = None
        return self.slug_id[slug]

    def users(self):
        """Taiga 全部使用者。卡片上的 assigned_users / watchers 只給 id,要靠這份換成名字。
        /users 預設分頁 30 筆,一定要帶 x-disable-pagination,不然後面的人(例:id 50 John)會查不到。"""
        if self._users is None:
            self._users = {}
            if self.ok:
                try:
                    for u in self._req("/users", headers={"x-disable-pagination": "True"}):
                        self._users[u["id"]] = {"username": u.get("username") or "",
                                                "name": u.get("full_name_display") or u.get("username") or ""}
                except Exception:
                    pass
        return self._users

    def user_ids(self, person, override=None):
        """PMO 名字 -> Taiga user id 集合。兩邊名字寫法不一定一樣
        (PMO「John Lee」/ Taiga「Acme-John Lee」),依序試:
        people.json 的 taiga_users 指定帳號 > username 或顯示名稱完全相同 > Taiga 顯示名稱包含 PMO 名字。
        username 只做完全比對 —— Taiga 有 Al、Bo、Ed 這種短帳號,用包含會誤配。"""
        us = self.users()
        if override:
            return {i for i, u in us.items() if u["username"].lower() == override.lower()}
        p = person.strip().lower()
        hit = {i for i, u in us.items() if p in (u["username"].lower(), u["name"].lower())}
        if not hit and len(p) >= 3:
            hit = {i for i, u in us.items() if p in u["name"].lower()}
        return hit

    def card(self, url):
        """回傳 {kind, status, rank, cancelled, closed, assignee, assigned, watchers, modified, subject},
        撈不到回 None。kind 是網址裡的 us / task / issue / epic。assigned / watchers 是 [{"id", "name"}]。
        確定不存在(404)的網址另外記進 self.dead;連不上、逾時這類暫時失敗不算。"""
        if not self.ok:
            return None
        if url in self.cache:
            return self.cache[url]
        got = None
        parsed = parse_url(url)
        if parsed:
            slug, kind, ref = parsed
            pid = self._pid(slug)
            if pid:
                try:
                    c = self._req(f"/{ENDPOINT[kind]}/by_ref?project={pid}&ref={ref}")
                    st = c.get("status_extra_info") or {}
                    name = st.get("name") or ""
                    key = name.strip().lower()
                    asg = c.get("assigned_to_extra_info") or {}
                    us = self.users()
                    def who(ids):
                        return [{"id": i, "name": (us.get(i) or {}).get("name") or f"#{i}"} for i in ids]
                    # US 用 assigned_users(可多人),task/issue 只有 assigned_to
                    a_ids = list(c.get("assigned_users") or [])
                    if c.get("assigned_to") and c["assigned_to"] not in a_ids:
                        a_ids.insert(0, c["assigned_to"])
                    got = {
                        "kind": kind,
                        "status": name,
                        "rank": TAIGA_RANK.get(key),
                        "cancelled": key in CANCELLED,
                        "closed": bool(st.get("is_closed")),
                        "assignee": asg.get("full_name_display") or "",
                        "assigned": who(a_ids),
                        "watchers": who(c.get("watchers") or []),
                        "modified": (c.get("modified_date") or "")[:10],
                        "subject": c.get("subject") or "",
                    }
                except urllib.error.HTTPError as e:
                    # 專案已經解得出 id(有權限看),卡片端點還回 404 -> 這張卡確實不存在
                    if e.code == 404:
                        self.dead.add(url)
                    got = None
                except Exception:
                    got = None
        self.cache[url] = got
        return got


def compare(pmo_rank, tg):
    """PMO 與 Taiga 的落差。回傳 (等級, 說明) —— 沒問題回 (None, "")。

    只在差兩階以上才標。差一階多半只是兩邊用詞顆粒度不同(例如 PMO「交測」對 Taiga
    「Review」),標出來只會製造雜訊。
    """
    if not tg:
        return None, ""
    if tg["cancelled"]:
        return "cancelled", f"Taiga 上已是「{tg['status']}」(不做了)"
    if tg["rank"] is None or pmo_rank is None:
        return None, ""
    d = pmo_rank - tg["rank"]
    if d >= 2:
        return "ahead", f"PMO 已到「{'交測' if pmo_rank >= 3 else '進行中'}」,Taiga 還在「{tg['status']}」"
    if d <= -2:
        return "behind", f"Taiga 已到「{tg['status']}」,PMO 還沒跟上"
    return None, ""


if __name__ == "__main__":
    t = Taiga()
    if not t.connect():
        sys.exit(t.reason)
    for u in sys.argv[1:]:
        print(u, "->", t.card(u))
