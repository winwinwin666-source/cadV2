"""CAD 任务分配管理工具 v4（云端版：Neon PostgreSQL）
部署：Streamlit Community Cloud；数据存放在 Neon，重启/休眠不丢失。

Secrets 二选一（在 Streamlit Cloud 后台 Settings → Secrets 里填写）：
  [connections.neon]
  url = "postgresql://用户名:密码@主机/库名?sslmode=require"
或顶层写一行：
  DATABASE_URL = "postgresql://用户名:密码@主机/库名?sslmode=require"

requirements.txt：streamlit pandas sqlalchemy psycopg2-binary
首次登录：账号 admin / 密码 admin123（登录后强制修改）
"""
import hashlib
import secrets
import time
import datetime as dt
import pandas as pd
from sqlalchemy import text
import streamlit as st

CONN_NAME = "neon"  # 对应 Secrets 里的 [connections.neon]
DIFF = {"简单": 0.8, "一般": 1.0, "复杂": 1.5}  # 难度系数
PRIO = ["紧急", "高", "普通", "低"]
SKILL_BONUS = 2.0  # 擅长该类型时，推荐分数减去的"等效工时"
ROLE_CN = {"admin": "超级管理员", "designer": "设计员"}
MAX_FAIL, LOCK_MIN = 5, 10  # 连续输错 5 次，锁定 10 分钟


# ---------- 数据库 ----------
def get_conn():
    try:
        url = st.secrets.get("DATABASE_URL")
    except Exception:
        url = None
    kw = dict(pool_pre_ping=True, pool_recycle=300)  # Neon 空闲会断开连接，自动检测并重连
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return st.connection(CONN_NAME, type="sql", url=url, **kw)
    return st.connection(CONN_NAME, type="sql", **kw)


def _py(v):
    return v.item() if hasattr(v, "item") else v  # numpy 数值转 Python 原生类型


def _named(sql, params):
    """把 ? 占位符转换成 SQLAlchemy 的 :p0, :p1 ..."""
    out, d, i = [], {}, 0
    for ch in sql:
        if ch == "?":
            out.append(f":p{i}")
            d[f"p{i}"] = _py(params[i])
            i += 1
        else:
            out.append(ch)
    assert i == len(params), "SQL 占位符数量与参数不一致"
    return "".join(out), d


def run_many(stmts):
    """同一个事务里执行多条语句：[(sql, params), ...]"""
    with get_conn().session as s:
        for sql, params in stmts:
            q, d = _named(sql, params)
            s.execute(text(q), d)
        s.commit()


def run(sql, params=()):
    run_many([(sql, params)])


def query(sql, params=()):
    q, d = _named(sql, params)
    return get_conn().query(q, params=d, ttl=0)


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def add_user(name, pw, role, skills="", must_change=1):
    salt, h = hash_pw(pw)
    run("INSERT INTO users(name,salt,pw_hash,role,skills,active,must_change) VALUES(?,?,?,?,?,1,?)",
        (name, salt, h, role, skills, must_change))


def set_password(name, pw, must_change=0):
    salt, h = hash_pw(pw)
    run("UPDATE users SET salt=?, pw_hash=?, must_change=? WHERE name=?", (salt, h, must_change, name))


@st.cache_resource
def init():
    """建表并写入默认数据；每个服务进程只执行一次"""
    run_many([
        ("""CREATE TABLE IF NOT EXISTS users(
            name TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT,
            skills TEXT DEFAULT '', active INTEGER DEFAULT 1, must_change INTEGER DEFAULT 1)""", ()),
        ("CREATE TABLE IF NOT EXISTS task_types(name TEXT PRIMARY KEY, std_hours DOUBLE PRECISION)", ()),
        ("""CREATE TABLE IF NOT EXISTS tasks(
            id SERIAL PRIMARY KEY,
            title TEXT, project TEXT, type TEXT, difficulty TEXT,
            priority TEXT, due TEXT, ref TEXT,
            est_hours DOUBLE PRECISION, actual_hours DOUBLE PRECISION,
            status TEXT DEFAULT '待分配', assignee TEXT,
            created TEXT, started TEXT, finished TEXT, note TEXT, creator TEXT, reviewer TEXT)""", ()),
    ])
    if query("SELECT COUNT(*) AS n FROM task_types").iloc[0, 0] == 0:
        run_many([("INSERT INTO task_types VALUES(?,?)", (n, h))
                  for n, h in [("新建图", 8.0), ("改图", 3.0), ("出图", 2.0), ("审图", 1.5)]])
    if query("SELECT COUNT(*) AS n FROM users").iloc[0, 0] == 0:
        add_user("admin", "admin123", "admin")


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- 业务逻辑 ----------
def designers():
    return query("SELECT name FROM users WHERE role='designer' AND active=1")["name"].tolist()


def workload():
    m = query("SELECT name, skills FROM users WHERE role='designer' AND active=1")
    t = query("SELECT assignee, SUM(est_hours) AS h, COUNT(*) AS n FROM tasks "
              "WHERE status='进行中' GROUP BY assignee")
    m = m.merge(t, left_on="name", right_on="assignee", how="left").fillna({"h": 0, "n": 0})
    return m[["name", "skills", "n", "h"]]


def recommend(task_type):
    w = workload()
    if w.empty:
        return None
    w["score"] = w.apply(lambda r: r.h - (SKILL_BONUS if task_type in (r.skills or "").split(",") else 0), axis=1)
    return w.sort_values("score").iloc[0]["name"]


def set_status(tid, status, **extra):
    sets, vals = ["status=?"], [status]
    for k, v in extra.items():
        sets.append(f"{k}=?")
        vals.append(v)
    run(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*vals, tid))


def reviewers():
    """可选审核人：所有启用账号，管理员排前面"""
    return query("SELECT name FROM users WHERE active=1 ORDER BY (role='admin') DESC, name")["name"].tolist()


def first_admin():
    r = query("SELECT name FROM users WHERE role='admin' AND active=1 ORDER BY name")
    return r.iloc[0, 0] if not r.empty else None


def reviewer_conflict(reviewer, assignee):
    """审核人不能同时是负责人"""
    return bool(assignee) and reviewer == assignee


def can_review(user, t):
    if user["role"] == "admin":  # 管理员可审核任何任务
        return True
    return t["reviewer"] == user["name"] and t["assignee"] != user["name"]


def calc_est(types, ttype, diff):
    std = float(types.loc[types["name"] == ttype, "std_hours"].iloc[0])
    return round(std * DIFF[diff], 1), std


# ---------- 登录 ----------
@st.cache_resource
def fail_log():
    return {}


def is_locked(name):
    cutoff = time.time() - LOCK_MIN * 60
    fails = [x for x in fail_log().get(name, []) if x > cutoff]
    fail_log()[name] = fails
    return len(fails) >= MAX_FAIL


def current_user():
    name = st.session_state.get("user")
    if not name:
        return None
    r = query("SELECT name, role, must_change FROM users WHERE name=? AND active=1", (name,))
    if r.empty:  # 账号被停用，立即失效
        st.session_state.pop("user", None)
        return None
    return r.iloc[0]


def page_login():
    st.title("CAD 任务分配管理")
    adm = query("SELECT must_change FROM users WHERE name='admin'")
    if not adm.empty and adm.iloc[0, 0] == 1:
        st.info("首次使用：账号 admin，初始密码 admin123，登录后需修改密码。")
    with st.form("login"):
        u = st.text_input("账号")
        p = st.text_input("密码", type="password")
        ok = st.form_submit_button("登录")
    if ok:
        u = u.strip()
        if is_locked(u):
            st.error(f"输错次数过多，请 {LOCK_MIN} 分钟后再试")
            return
        r = query("SELECT * FROM users WHERE name=? AND active=1", (u,))
        if not r.empty:
            row = r.iloc[0]
            if secrets.compare_digest(hash_pw(p, row["salt"])[1], row["pw_hash"]):
                fail_log().pop(u, None)
                st.session_state["user"] = row["name"]
                st.rerun()
        fail_log().setdefault(u, []).append(time.time())
        st.error("账号或密码错误，或账号已停用")


def page_password(user):
    st.subheader("修改密码")
    if user["must_change"]:
        st.warning("为了安全，请先修改初始密码。")
    with st.form("pw"):
        old = st.text_input("原密码", type="password")
        new = st.text_input("新密码（至少 6 位）", type="password")
        new2 = st.text_input("确认新密码", type="password")
        ok = st.form_submit_button("修改")
    if ok:
        r = query("SELECT salt, pw_hash FROM users WHERE name=?", (user["name"],)).iloc[0]
        if not secrets.compare_digest(hash_pw(old, r["salt"])[1], r["pw_hash"]):
            st.error("原密码不正确")
        elif len(new) < 6:
            st.error("新密码至少 6 位")
        elif new != new2:
            st.error("两次输入不一致")
        elif new == old:
            st.error("新密码不能与原密码相同")
        else:
            set_password(user["name"], new, 0)
            st.success("密码已修改")
            st.rerun()


# ---------- 任务表单 / 详情 / 编辑（共用） ----------
def task_fields(types, t, k, default_rev=None):
    """任务字段输入；t 为 None 表示新建，否则为已有任务（编辑）。k 为 key 前缀。"""
    names = types["name"].tolist()
    g = (lambda f: t[f]) if t is not None else (lambda f: None)
    title = st.text_input("图号 / 任务名称*", value=g("title") or "", key=f"{k}_title")
    project = st.text_input("所属项目", value=g("project") or "", key=f"{k}_project")
    c1, c2, c3 = st.columns(3)
    ttype = c1.selectbox("类型", names, index=names.index(g("type")) if g("type") in names else 0, key=f"{k}_type")
    diff = c2.selectbox("难度", list(DIFF), index=list(DIFF).index(g("difficulty")) if g("difficulty") in DIFF else 1,
                        key=f"{k}_diff")
    prio = c3.selectbox("优先级", PRIO, index=PRIO.index(g("priority")) if g("priority") in PRIO else 2,
                        key=f"{k}_prio")
    due0 = dt.date.fromisoformat(g("due")) if g("due") else dt.date.today() + dt.timedelta(days=3)
    due = st.date_input("截止日期", due0, key=f"{k}_due")
    ref = st.text_area("参考资料 / 说明（路径、链接、要求）", value=g("ref") or "", key=f"{k}_ref")
    revs = reviewers()
    cur = g("reviewer") or default_rev or first_admin()
    reviewer = st.selectbox("审核人（任务完成后由谁验收）", revs,
                            index=revs.index(cur) if cur in revs else 0, key=f"{k}_rev")
    return title.strip(), project, ttype, diff, prio, due, ref, reviewer


def show_detail(t):
    st.write(f"项目：{t['project'] or '-'} ｜ 截止：{t['due']} ｜ 难度：{t['difficulty']} ｜ 创建人：{t['creator'] or '-'} ｜ 审核人：{t['reviewer'] or '管理员'}")
    if t["ref"]:
        st.write(f"说明：{t['ref']}")
    if t["note"]:
        st.warning(f"备注 / 修改记录：\n{t['note']}")


def render_edit(t, user):
    """权限：管理员可改/删任何任务；设计员只能改自己创建的（待分配/进行中），删自己创建且未指派的"""
    tid = int(t["id"])
    admin = user["role"] == "admin"
    own = t["creator"] == user["name"]
    can_edit = admin or (own and t["status"] in ("待分配", "进行中"))
    can_del = admin or (own and t["status"] == "待分配")
    if not (can_edit or can_del):
        return
    if not st.checkbox("✏️ 编辑 / 删除此任务", key=f"tg{tid}"):
        return
    types = query("SELECT * FROM task_types")
    if can_edit:
        with st.form(f"ef{tid}"):
            title, project, ttype, diff, prio, due, ref, reviewer = task_fields(types, t, f"e{tid}")
            save = st.form_submit_button("保存修改")
        if save:
            if not title:
                st.error("请填写图号/任务名称")
            elif reviewer_conflict(reviewer, t["assignee"]):
                st.error("审核人不能是任务负责人，请换一个审核人")
            else:
                est, _ = calc_est(types, ttype, diff)
                note = (t["note"] or "") + f"[{now()}] {user['name']} 修改了任务信息\n"
                run("UPDATE tasks SET title=?,project=?,type=?,difficulty=?,priority=?,due=?,ref=?,"
                    "reviewer=?,est_hours=?,note=? WHERE id=?",
                    (title, project, ttype, diff, prio, due.isoformat(), ref, reviewer, est, note, tid))
                st.rerun()
    if can_del:
        sure = st.checkbox("确认删除（不可恢复）", key=f"dc{tid}")
        if st.button("🗑️ 删除任务", key=f"dd{tid}", disabled=not sure):
            run("DELETE FROM tasks WHERE id=?", (tid,))
            st.rerun()


def render_review(t, user):
    """审核：指定审核人或管理员可操作；审核人不能是负责人本人"""
    tid = int(t["id"])
    if not can_review(user, t):
        st.caption(f"等待 {t['reviewer'] or '管理员'} 审核")
        return
    default = t["actual_hours"] if pd.notna(t["actual_hours"]) else t["est_hours"]
    hrs = st.number_input("实际工时（设计员填报，可调整）", 0.0, 200.0, float(default), 0.5, key=f"h{tid}")
    c1, c2 = st.columns(2)
    if c1.button("✅ 通过", key=f"p{tid}"):
        note = (t["note"] or "") + f"[{now()}] {user['name']} 审核通过\n"
        set_status(tid, "完成", actual_hours=hrs, finished=now(), note=note)
        st.rerun()
    reason = c2.text_input("退回原因", key=f"r{tid}")
    if c2.button("↩️ 退回修改", key=f"b{tid}"):
        if not reason.strip():
            st.error("请填写退回原因")
        else:
            note = (t["note"] or "") + f"[{now()}] {user['name']} 退回：{reason}\n"
            set_status(tid, "进行中", note=note)
            st.rerun()


# ---------- 公共页面 ----------
def page_board():
    st.subheader("负载看板")
    w = workload()
    if w.empty:
        st.info("还没有设计员账号，请管理员在“账号管理”中添加。")
    else:
        cols = st.columns(len(w))
        for col, (_, r) in zip(cols, w.iterrows()):
            col.metric(r["name"], f"{r['h']:.1f} 小时", f"{int(r['n'])} 个进行中", delta_color="off")
    pend = query("SELECT COUNT(*) n FROM tasks WHERE status='待分配'").iloc[0, 0]
    review = query("SELECT COUNT(*) n FROM tasks WHERE status='待审核'").iloc[0, 0]
    st.caption(f"待分配 {pend} 个 ｜ 待审核 {review} 个")

    df = query("SELECT id, title, project, type, priority, due, est_hours, status, assignee, creator, reviewer "
               "FROM tasks WHERE status!='完成'")
    if df.empty:
        st.info("暂无未完成任务")
        return
    today = dt.date.today().isoformat()
    df["逾期"] = df["due"].apply(lambda d: "⚠️" if d and d < today else "")
    df["_p"] = df["priority"].map({p: i for i, p in enumerate(PRIO)})
    df = df.sort_values(["_p", "due"]).drop(columns="_p")
    st.dataframe(df, hide_index=True, use_container_width=True)


def page_new(user):
    st.subheader("新建任务")
    admin = user["role"] == "admin"
    types = query("SELECT * FROM task_types")
    with st.form("new_task", clear_on_submit=True):
        title, project, ttype, diff, prio, due, ref, reviewer = task_fields(
            types, None, "new", user["name"] if admin else None)
        if admin:
            opts = ["暂不指派（先放待分配）"] + designers()
            pick = st.selectbox("直接下单给", opts)
            assignee = None if pick == opts[0] else pick
        else:
            mine = st.checkbox("这个任务由我自己来做（直接开始）")
            assignee = user["name"] if mine else None
        ok = st.form_submit_button("提交")
    if ok:
        if not title:
            st.error("请填写图号/任务名称")
            return
        if reviewer_conflict(reviewer, assignee):
            st.error("审核人不能是任务负责人，请换一个审核人或负责人")
            return
        est, std = calc_est(types, ttype, diff)
        status, started = ("进行中", now()) if assignee else ("待分配", None)
        run("INSERT INTO tasks(title,project,type,difficulty,priority,due,ref,est_hours,status,"
            "assignee,created,started,creator,reviewer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, project, ttype, diff, prio, due.isoformat(), ref, est, status,
             assignee, now(), started, user["name"], reviewer))
        msg = f"已提交，预估工时 {est} 小时（标准 {std} × 难度系数 {DIFF[diff]}）"
        st.success(msg + (f"，已指派给 {assignee}" if assignee else "，等待管理员指派"))


# ---------- 设计员页面 ----------
def page_mine(user):
    st.subheader("我的任务（我负责的 + 我创建的）")
    me = user["name"]
    n = query("SELECT COUNT(*) FROM tasks WHERE status='待审核' AND reviewer=? AND COALESCE(assignee,'')!=?",
              (me, me)).iloc[0, 0]
    if n:
        st.info(f"你有 {n} 个任务等你审核，请到“待我审核”页处理。")
    df = query("SELECT * FROM tasks WHERE (assignee=? OR creator=?) AND status!='完成' ORDER BY id DESC",
               (me, me))
    if df.empty:
        st.info("当前没有任务")
    for _, t in df.iterrows():
        tid = int(t["id"])
        head = (f"#{tid} [{t['priority']}] {t['title']} · {t['type']} · 预估 {t['est_hours']}h · "
                f"{t['status']} · 负责人：{t['assignee'] or '未指派'}")
        with st.expander(head, expanded=(t["assignee"] == me and t["status"] == "进行中")):
            show_detail(t)
            if t["assignee"] == me and t["status"] == "进行中":
                hrs = st.number_input("实际用时（小时）", 0.0, 200.0, float(t["est_hours"]), 0.5, key=f"h{tid}")
                if st.button("提交审核", key=f"s{tid}"):
                    set_status(tid, "待审核", actual_hours=hrs)
                    st.rerun()
            elif t["status"] == "待审核":
                st.caption(f"已提交审核，等待 {t['reviewer'] or '管理员'} 审核")
            elif not t["assignee"]:
                st.caption("等待管理员指派")
            render_edit(t, user)
    done = query("SELECT COUNT(*) n, COALESCE(SUM(actual_hours),0) h FROM tasks "
                 "WHERE assignee=? AND status='完成'", (me,)).iloc[0]
    st.caption(f"历史已完成 {int(done['n'])} 个，累计 {done['h']:.1f} 小时")


def page_review(user):
    st.subheader("待我审核")
    if user["role"] == "admin":  # 管理员可看到并审核所有待审核任务
        df = query("SELECT * FROM tasks WHERE status='待审核' ORDER BY id DESC")
    else:
        df = query("SELECT * FROM tasks WHERE status='待审核' AND reviewer=? AND COALESCE(assignee,'')!=? "
                   "ORDER BY id DESC", (user["name"], user["name"]))
    if df.empty:
        st.info("没有待审核的任务")
        return
    for _, t in df.iterrows():
        head = (f"#{int(t['id'])} [{t['priority']}] {t['title']} · {t['type']} · "
                f"负责人：{t['assignee']} · 填报 {t['actual_hours']}h")
        with st.expander(head, expanded=True):
            show_detail(t)
            render_review(t, user)


# ---------- 管理员页面 ----------
def page_tasks(user):
    st.subheader("任务处理")
    members = designers()
    df = query("SELECT * FROM tasks WHERE status!='完成' ORDER BY id DESC")
    if df.empty:
        st.info("没有待处理任务")
        return
    if not members:
        st.warning("还没有可用的设计员，请先在“账号管理”添加。")
    for _, t in df.iterrows():
        tid = int(t["id"])
        head = f"#{tid} [{t['priority']}] {t['title']} · {t['type']} · {t['est_hours']}h · {t['status']}"
        if t["assignee"]:
            head += f" · {t['assignee']}"
        with st.expander(head):
            show_detail(t)
            if t["status"] == "待分配" and members:
                rec = recommend(t["type"])
                st.caption(f"系统推荐：{rec}（负载低且擅长优先）")
                who = st.selectbox("指派给", members, index=members.index(rec), key=f"w{tid}")
                if st.button("指派并开始", key=f"a{tid}"):
                    if reviewer_conflict(t["reviewer"], who):
                        st.error("该设计员同时是审核人，请先在下方“编辑”里改审核人，或换一个负责人")
                    else:
                        set_status(tid, "进行中", assignee=who, started=now())
                        st.rerun()

            elif t["status"] == "进行中" and members:
                idx = members.index(t["assignee"]) if t["assignee"] in members else 0
                who = st.selectbox("改派给", members, index=idx, key=f"rw{tid}")
                if st.button("确认改派", key=f"ra{tid}") and who != t["assignee"]:
                    if reviewer_conflict(t["reviewer"], who):
                        st.error("该设计员同时是审核人，请先在下方“编辑”里改审核人，或换一个负责人")
                    else:
                        note = (t["note"] or "") + f"[{now()}] 改派：{t['assignee']} → {who}\n"
                        set_status(tid, "进行中", assignee=who, note=note)
                        st.rerun()

            elif t["status"] == "待审核":
                render_review(t, user)
            render_edit(t, user)


def page_all(user):
    st.subheader("全部任务（含已完成）")
    df = query("SELECT id, title, project, type, priority, due, est_hours, actual_hours, status, "
               "assignee, creator, created, finished FROM tasks ORDER BY id DESC")
    if df.empty:
        st.info("暂无任务")
        return
    st.dataframe(df, hide_index=True, use_container_width=True)
    tid = st.selectbox("选择任务编号进行编辑/删除", df["id"].tolist())
    t = query("SELECT * FROM tasks WHERE id=?", (int(tid),)).iloc[0]
    show_detail(t)
    render_edit(t, user)


def page_stats():
    st.subheader("数据复盘")
    df = query("SELECT * FROM tasks WHERE status='完成'")
    if df.empty:
        st.info("暂无已完成任务")
        return
    for title, key in [("按人员", "assignee"), ("按任务类型（比值长期偏离 1，说明标准工时需要调整）", "type")]:
        st.write(f"**{title}**")
        a = df.groupby(key).agg(完成数=("id", "count"), 预估工时=("est_hours", "sum"),
                                实际工时=("actual_hours", "sum")).reset_index()
        a["实际/预估"] = (a["实际工时"] / a["预估工时"]).round(2)
        st.dataframe(a, hide_index=True, use_container_width=True)
    st.download_button("导出全部任务 CSV", query("SELECT * FROM tasks").to_csv(index=False).encode("utf-8-sig"),
                       "tasks.csv", "text/csv")


def page_accounts(user):
    st.subheader("账号管理")
    types = query("SELECT name FROM task_types")["name"].tolist()
    u = query('SELECT name AS "账号", role AS "角色", skills AS "擅长类型", active AS "启用", '
              'must_change AS "待改密" FROM users')
    u["角色"] = u["角色"].map(ROLE_CN)
    u["启用"] = u["启用"].map({1: "是", 0: "否"})
    u["待改密"] = u["待改密"].map({1: "是", 0: "否"})
    st.dataframe(u, hide_index=True, use_container_width=True)

    st.write("**新增账号**")
    with st.form("add_user", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("账号（也是显示名）")
        pw = c2.text_input("初始密码（至少 6 位）", type="password")
        role = c3.selectbox("角色", list(ROLE_CN), format_func=ROLE_CN.get, index=1)
        skills = st.multiselect("擅长类型", types)
        ok = st.form_submit_button("创建")
    if ok:
        name = name.strip()
        if not name or len(pw) < 6:
            st.error("账号不能为空，密码至少 6 位")
        elif not query("SELECT 1 FROM users WHERE name=?", (name,)).empty:
            st.error("账号已存在")
        else:
            add_user(name, pw, role, ",".join(skills))
            st.success(f"已创建 {name}，首次登录将要求修改密码")
            st.rerun()

    st.write("**修改 / 停用 / 重置密码**")
    target = st.selectbox("选择账号", query("SELECT name FROM users")["name"].tolist())
    row = query("SELECT * FROM users WHERE name=?", (target,)).iloc[0]
    c1, c2, c3 = st.columns(3)
    new_role = c1.selectbox("角色", list(ROLE_CN), format_func=ROLE_CN.get,
                            index=list(ROLE_CN).index(row["role"]), key=f"role_{target}")
    active = c2.checkbox("启用", value=bool(row["active"]), key=f"act_{target}")
    cur_sk = [s for s in (row["skills"] or "").split(",") if s in types]
    new_sk = c3.multiselect("擅长类型", types, default=cur_sk, key=f"sk_{target}")
    if st.button("保存修改"):
        admins = query("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").iloc[0, 0]
        losing_admin = row["role"] == "admin" and row["active"] == 1 and (new_role != "admin" or not active)
        if losing_admin and admins <= 1:
            st.error("至少要保留一个启用的超级管理员")
        elif target == user["name"] and not active:
            st.error("不能停用自己当前登录的账号")
        else:
            run("UPDATE users SET role=?, active=?, skills=? WHERE name=?",
                (new_role, int(active), ",".join(new_sk), target))
            st.success("已保存")
            st.rerun()
    with st.form(f"reset_{target}", clear_on_submit=True):
        rp = st.text_input("为该账号设置新密码（至少 6 位）", type="password")
        if st.form_submit_button("重置密码"):
            if len(rp) < 6:
                st.error("密码至少 6 位")
            else:
                set_password(target, rp, must_change=1)
                st.success("已重置，该用户下次登录需修改密码")
    st.caption("历史任务会保留账号名，所以账号只能停用，不能删除。")


def page_settings():
    st.subheader("任务类型与标准工时")
    t = st.data_editor(query("SELECT * FROM task_types"), num_rows="dynamic", hide_index=True, key="te")
    st.caption(f"难度系数：{DIFF}；擅长加分：{SKILL_BONUS} 小时。可在文件顶部修改。")
    if st.button("保存"):
        rows = t.dropna(subset=["name", "std_hours"])
        stmts = [("DELETE FROM task_types", ())]
        stmts += [("INSERT INTO task_types VALUES(?,?) "
                   "ON CONFLICT (name) DO UPDATE SET std_hours=EXCLUDED.std_hours",
                   (r["name"], float(r["std_hours"]))) for _, r in rows.iterrows()]
        run_many(stmts)
        st.success("已保存")


# ---------- 入口 ----------
def main():
    st.set_page_config(page_title="CAD任务管理", layout="wide")
    init()
    user = current_user()
    if user is None:
        page_login()
        return

    st.sidebar.write(f"👤 {user['name']}（{ROLE_CN[user['role']]}）")
    if st.sidebar.button("退出登录"):
        st.session_state.pop("user", None)
        st.rerun()

    if user["must_change"]:
        page_password(user)
        return

    if user["role"] == "admin":
        pages = {"负载看板": page_board, "新建任务": lambda: page_new(user),
                 "任务处理": lambda: page_tasks(user), "待我审核": lambda: page_review(user),
                 "全部任务": lambda: page_all(user),
                 "数据复盘": page_stats, "账号管理": lambda: page_accounts(user),
                 "任务类型设置": page_settings, "修改密码": lambda: page_password(user)}
    else:
        pages = {"我的任务": lambda: page_mine(user), "新建任务": lambda: page_new(user),
                 "待我审核": lambda: page_review(user), "负载看板": page_board,
                 "修改密码": lambda: page_password(user)}
    choice = st.sidebar.radio("导航", list(pages))
    st.title("CAD 任务分配管理")
    pages[choice]()


main()
