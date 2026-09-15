#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
罐底沉降监测数据管理与复算 API
================================

仅依赖标准库（http.server / json / sqlite3），管理：
  罐体尺寸、标志方位、历次高程、装液工况、水准环线、校准（原点稳定性）资料。

处理流程
--------
1. 每轮观测先归算到稳定高程系：
     h_r(m) = H_r(m) - H_r(原点) + Δ原点(date) + α·h_ref·T + c·L
   其中 Δ原点 取校准资料中不晚于观测日期的最新改正；α 为罐壁线膨胀系数，
   c 为液位-下沉响应系数，分别消除温差与装液影响。
2. 两轮间下沉量  s = h_a - h_b（正为下沉）。
3. 最小二乘分离：整体升降 a0、刚性倾斜（cosθ/sinθ）、环向局部残差，
   残差再做 2..K 阶谐波分析；并计算邻点差、底板排水坡度。
4. 质检（不满足则该弧段不参与计算，并列补测位置）：
     LOOP_CLOSURE   环线闭合差超限        （整轮作废，整圈补测）
     ORIGIN_UNSTABLE 联系水准判定原点失稳  （不计算，补测原点/联系点）
     UNTIED         原点无校准且非假定稳定点
     DUPLICATE_AZIMUTH 方位重号           （相关弧段剔除）
     ORDER_INVERSION 观测次序倒置         （相关邻边剔除）
     MISSING        标志漏测              （邻边剔除，逐点补测）
     GAP_TOO_LONG   空缺弧过长            （弧中补测）
5. 任一指标超允许值：issue 回指原始 reading_id / marker_id / round_id。
6. 改选原点或剔除读数必须给出 reason，生成新版本；锁定后才能导出
   补测表(CSV)、环向沉降 SVG、复算 JSON。
7. 分级加载（水压试验）：方案按级设定目标液位、最短保压时长、允许沉降速率
   与残余沉降限值；修订按顺序绑定空罐、充水、保压、卸载、复测轮次。引擎以
   液位×密度换算荷载，复用逐轮归算与质检（qc_* 系列函数），逐标志计算各级
   增量、保压速率、卸载回弹率、残余沉降与加载—卸载滞回。阶段倒序、同一轮次
   重复绑定、荷载方向不符、观测间隔不足或来源轮次含致命问题时保持待判
   （pending）；速率或残余越限回指阶段与读数。改绑阶段或改取阈值须记 reason
   生成新试验修订；定稿(final)后导出逐级 JSON 与荷载—沉降 SVG。
"""

import argparse
import csv
import io
import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------
# 数据库结构
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tank (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  radius_m REAL,
  height_m REAL,
  wall_alpha REAL DEFAULT 1.2e-5,      -- 罐壁线膨胀系数 1/℃
  ref_height_m REAL DEFAULT 0,         -- 壳底标志至检尺(温度)基准面高度 m
  bottom_slope_spec REAL DEFAULT 0.0083, -- 设计排水坡度(约 1/120，向心为正)
  center_marker_id INTEGER,
  note TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS marker (
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tank(id),
  code TEXT NOT NULL,                  -- 标志编号
  azimuth_deg REAL NOT NULL,           -- 环向方位角（北=0，顺时针）
  ring_order INTEGER,                  -- 观测次序编号
  is_center INTEGER DEFAULT 0,
  note TEXT,
  UNIQUE(tank_id, code)
);
CREATE TABLE IF NOT EXISTS benchmark (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  assumed_stable INTEGER DEFAULT 0,    -- 假定稳定原点（初始高程系）
  description TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS calibration (
  id INTEGER PRIMARY KEY,
  benchmark_id INTEGER NOT NULL REFERENCES benchmark(id),
  date TEXT NOT NULL,                  -- ISO 日期
  delta_m REAL DEFAULT 0,              -- 相对稳定高程系的累计高程改正
  delta_std_m REAL,
  stable INTEGER,                      -- 1 稳定 / 0 失稳 / NULL 待核
  note TEXT
);
CREATE TABLE IF NOT EXISTS round (
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tank(id),
  seq INTEGER NOT NULL,                -- 观测轮次
  measured_at TEXT NOT NULL,          -- ISO 日期时间
  origin_benchmark_id INTEGER REFERENCES benchmark(id),
  origin_reading_m REAL,               -- 原点在本轮的观测高程 H_r(原点)
  liquid_level_m REAL DEFAULT 0,       -- 液位
  liquid_density REAL,
  wall_temp_c REAL,                    -- 罐壁温度 ℃
  load_coeff_m_per_m REAL DEFAULT 0,   -- 每米液位壳底下沉量（实测/设计给定）
  loop_misclosure_m REAL,              -- 水准环线闭合差
  loop_length_km REAL DEFAULT 1,       -- 环线长度
  note TEXT,
  UNIQUE(tank_id, seq)
);
CREATE TABLE IF NOT EXISTS reading (
  id INTEGER PRIMARY KEY,
  round_id INTEGER NOT NULL REFERENCES round(id),
  marker_id INTEGER NOT NULL REFERENCES marker(id),
  elevation_m REAL NOT NULL,
  order_idx INTEGER,
  note TEXT,
  UNIQUE(round_id, marker_id)
);
CREATE TABLE IF NOT EXISTS round_tie (
  id INTEGER PRIMARY KEY,
  round_id INTEGER NOT NULL REFERENCES round(id),
  benchmark_id INTEGER NOT NULL REFERENCES benchmark(id),
  tie_elevation_m REAL,                -- 该基准点本轮联测高程
  delta_stable_m REAL,                 -- 已知稳定高程改正（可空）
  UNIQUE(round_id, benchmark_id)
);
CREATE TABLE IF NOT EXISTS version (
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tank(id),
  seq INTEGER NOT NULL,                -- 版本号（同罐递增）
  round_a_id INTEGER NOT NULL REFERENCES round(id),
  round_b_id INTEGER NOT NULL REFERENCES round(id),
  origin_benchmark_id INTEGER REFERENCES benchmark(id), -- 改选原点（可空=沿用轮次原点）
  excluded_readings TEXT,              -- JSON {"<reading_id>": "剔除理由"}
  params TEXT,                         -- JSON 容差等参数
  reason TEXT NOT NULL,                -- 建版理由（改原点/剔除读数必须说明）
  status TEXT DEFAULT 'draft',         -- draft / locked / rejected
  created_at TEXT,
  locked_at TEXT,
  result TEXT                          -- JSON 复算结果
);
CREATE TABLE IF NOT EXISTS hydro_test (
  id INTEGER PRIMARY KEY,
  tank_id INTEGER NOT NULL REFERENCES tank(id),
  code TEXT NOT NULL,                  -- 试验编号
  density_kg_m3 REAL DEFAULT 1000,     -- 试验介质密度（默认水）
  note TEXT,
  created_at TEXT,
  UNIQUE(tank_id, code)
);
CREATE TABLE IF NOT EXISTS hydro_stage (
  id INTEGER PRIMARY KEY,
  test_id INTEGER NOT NULL REFERENCES hydro_test(id),
  seq INTEGER NOT NULL,                -- 级序（1..N，加载顺序）
  target_level_m REAL NOT NULL,        -- 目标液位 m
  min_hold_hours REAL NOT NULL,        -- 最短保压时长 h
  rate_limit_m_per_h REAL NOT NULL,    -- 允许沉降速率 m/h
  residual_limit_m REAL NOT NULL,      -- 残余沉降限值 m
  UNIQUE(test_id, seq)
);
CREATE TABLE IF NOT EXISTS hydro_revision (
  id INTEGER PRIMARY KEY,
  test_id INTEGER NOT NULL REFERENCES hydro_test(id),
  seq INTEGER NOT NULL,                -- 修订号（同试验递增）
  bindings TEXT NOT NULL,              -- JSON 槽位绑定 {"empty":rid,
                                       --   "stages":[{"fill":rid,"hold":rid}...],
                                       --   "unload":rid,"recheck":rid}
  thresholds TEXT,                     -- JSON 改取阈值 {"stages":{"<级>":{...}},
                                       --   "params":{...}}
  reason TEXT NOT NULL,                -- 修订依据（改绑/改阈值必须说明）
  status TEXT DEFAULT 'pending',       -- pending(待判) / draft / final(定稿)
  created_at TEXT,
  finalized_at TEXT,
  result TEXT                          -- JSON 分级分析结果（定稿后冻结）
);
"""

DEFAULT_PARAMS = {
    "loop_m_per_sqrt_km": 0.004,   # 环线闭合差允许值系数：±k√L km，单位 m
    "origin_drift_m": 0.003,       # 原点漂移允许值
    "tie_spread_m": 0.003,         # 联系点相对变化互差
    "total_settlement_m": 0.040,   # 单点累计沉降允许值
    "adjacent_diff_m": 0.008,      # 邻点沉降差允许值
    "diameter_diff_m": 0.018,      # 罐顶/罐底直径方向差异沉降允许值
    "harmonic_amp_m": 0.010,       # 单阶谐波振幅允许值
    "bottom_slope_min": 0.0083,    # 底板最小排水坡度（约1/120）
    "max_gap_deg": 90.0,           # 允许最长空缺弧
    "max_missing_fraction": 0.25,  # 允许漏测比例
    "harmonic_max": 6,             # 最高谐波阶数
}

write_lock = threading.RLock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# ----------------------------------------------------------------------------
# 基础数值工具
# ----------------------------------------------------------------------------

def _gauss(A, b):
    """高斯-若当消元，解线性方程组。"""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("奇异矩阵，无法求解")
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        for c in range(col, n + 1):
            M[col][c] /= d
        for r in range(n):
            if r == col:
                continue
            f = M[r][col]
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    return [M[i][n] for i in range(n)]


def lls(X, y):
    """最小二乘 y ≈ X·β（正规方程）。"""
    n, m = len(X), len(X[0])
    XtX = [[0.0] * m for _ in range(m)]
    Xty = [0.0] * m
    for i in range(n):
        for k in range(m):
            xik = X[i][k]
            Xty[k] += xik * y[i]
            for j in range(m):
                XtX[k][j] += xik * X[i][j]
    return _gauss(XtX, Xty)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else 0.5 * (xs[mid - 1] + xs[mid])


def fwd_angle(a, b):
    """方位角 a -> b 的顺时针夹角（0..360）。"""
    return (b - a) % 360.0


# ----------------------------------------------------------------------------
# 数据访问辅助
# ----------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def require(obj, fields):
    missing = [f for f in fields if obj.get(f) is None]
    if missing:
        raise ApiError(400, "MISSING_FIELD", "缺少必填字段: %s" % ", ".join(missing))


def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def allrows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def cal_delta(conn, benchmark_id, date):
    """取不晚于 date 的最新校准改正；无记录返回 (0.0, False)。"""
    r = conn.execute(
        "SELECT * FROM calibration WHERE benchmark_id=? AND date<=? "
        "ORDER BY date DESC, id DESC LIMIT 1", (benchmark_id, date)).fetchone()
    return (float(r["delta_m"]), True) if r else (0.0, False)


def cal_latest(conn, benchmark_id, date):
    """不晚于 date 的最新校准整行记录（含 stable 判定）。"""
    return conn.execute(
        "SELECT * FROM calibration WHERE benchmark_id=? AND date<=? "
        "ORDER BY date DESC, id DESC LIMIT 1", (benchmark_id, date)).fetchone()


# ----------------------------------------------------------------------------
# 逐轮归算与质检（analyze_version 与分级加载试验引擎共用）
# ----------------------------------------------------------------------------

def qc_resolve_origin(conn, rnd, origin, ties_of_round, issue, fatal_codes):
    """逐轮确定实际原点读数、稳定系改正与来源；记录致命/提示问题。

    返回 {"reading_m","delta_m","source","has_cal"}；致命问题代码追加到
    fatal_codes。origin 为版本/试验实际采用的水准原点；改选原点时必须有该轮
    round_tie 联测记录，否则记 UNTIED 致命。
    """
    bid = origin["id"]
    calrow = cal_latest(conn, bid, rnd["measured_at"])
    has_cal = calrow is not None
    cal_delta_m = float(calrow["delta_m"]) if has_cal else 0.0

    if bid == rnd["origin_benchmark_id"]:
        # 沿用本轮原观测原点
        if rnd["origin_reading_m"] is None:
            raise ApiError(400, "BAD_ROUND",
                           "轮次 %d 缺原点观测高程" % rnd["seq"])
        origin_h, source = float(rnd["origin_reading_m"]), "round.origin_reading_m"
    else:
        # 改选原点：必须以该轮 round_tie.tie_elevation_m 重新归算
        tie = ties_of_round.get(bid)
        if not tie or tie["tie_elevation_m"] is None:
            issue("UNTIED", "fatal",
                  "轮次 %d 未对改选原点 %s 作联系联测，无法重新归算"
                  % (rnd["seq"], origin["code"]),
                  round_ids=[rnd["id"]], benchmark_ids=[bid],
                  resurvey={"type": "改选原点联测补测",
                            "round_seq": rnd["seq"], "benchmark": origin["code"]})
            fatal_codes.append("UNTIED")
            return {"reading_m": None, "delta_m": 0.0,
                    "source": "missing_tie", "has_cal": has_cal}
        origin_h = float(tie["tie_elevation_m"])
        source = "round_tie.tie_elevation_m"
        # 联测自带稳定系改正时，优先采用（无独立校准资料）
        if tie["delta_stable_m"] is not None and not has_cal:
            cal_delta_m = float(tie["delta_stable_m"])
            has_cal = True

    # 校准资料 stable=0：原点失稳，该轮弧段一律不计算
    if calrow is not None and calrow["stable"] == 0:
        note = calrow["note"] or ""
        issue("ORIGIN_UNSTABLE", "fatal",
              "校准资料判定原点 %s 在轮次 %d（%s）失稳%s，弧段不计算"
              % (origin["code"], rnd["seq"], calrow["date"],
                 ("：" + note) if note else ""),
              round_ids=[rnd["id"]], benchmark_ids=[bid],
              metric="calibration.stable", value=0, limit=1,
              resurvey={"type": "原点重新检定/另选稳定原点",
                        "round_seq": rnd["seq"], "benchmark": origin["code"]})
        fatal_codes.append("ORIGIN_UNSTABLE")

    if not has_cal:
        if origin["assumed_stable"]:
            issue("CAL_MISSING", "info",
                  "原点 %s 在轮次 %d 观测日前无校准记录，按假定稳定点处理"
                  % (origin["code"], rnd["seq"]), round_ids=[rnd["id"]])
        else:
            issue("UNTIED", "fatal",
                  "轮次 %d 的水准原点 %s 无校准资料且非假定稳定点，无法归算"
                  % (rnd["seq"], origin["code"]),
                  round_ids=[rnd["id"]], benchmark_ids=[bid],
                  resurvey={"type": "原点校准补测", "benchmark": origin["code"]})
            fatal_codes.append("UNTIED")

    return {"reading_m": origin_h, "delta_m": cal_delta_m,
            "source": source, "has_cal": has_cal}


def qc_check_loop(rnd, params, issue, fatal_codes):
    """环线闭合差质检：超过 k·√L 记 LOOP_CLOSURE 致命并整圈重测。"""
    f, L = rnd["loop_misclosure_m"], rnd["loop_length_km"]
    if f is None:
        issue("LOOP_DATA_MISSING", "warning", "轮次 %d 缺环线闭合差记录" % rnd["seq"],
              round_ids=[rnd["id"]])
        return
    tol = params["loop_m_per_sqrt_km"] * math.sqrt(max(L or 0, 1e-9))
    if abs(float(f)) > tol:
        issue("LOOP_CLOSURE", "fatal",
              "轮次 %d 环线闭合差 %.1f mm 超过允许 ±%.1f mm"
              % (rnd["seq"], float(f) * 1000, tol * 1000),
              round_ids=[rnd["id"]], metric="loop_misclosure_m",
              value=f, limit=tol,
              resurvey={"type": "整圈水准环线重测", "round_seq": rnd["seq"],
                        "where": "全环"})
        fatal_codes.append("LOOP_CLOSURE")


def qc_order_inversions(rnd, rdict, ring, skip_marker_ids, by_id, issue):
    """按逐轮 reading.order_idx 检查环向观测次序，返回倒置邻边集合。

    只对两个端点均有有效读数且确为环向相邻（含闭合边）的边判倒置；
    观测从最大序点闭合回最小序点的那一条物理闭合边属正常收测，不判倒置；
    连续倒置段归并成一条 ORDER_INVERSION。
    """
    idx_of = {}
    for m in ring:
        row = rdict.get(m["id"])
        if row is None or row["order_idx"] is None or m["id"] in skip_marker_ids:
            continue
        idx_of[m["id"]] = int(row["order_idx"])

    n = len(ring)
    oriented = [(ring[i]["id"], ring[(i + 1) % n]["id"]) for i in range(n)]

    # 闭合边：序号最大点与最小序点若环向相邻，该边为正常收测边
    closure = None
    if len(idx_of) >= 2:
        mk_max = max(idx_of, key=lambda k: (idx_of[k], k))
        mk_min = min(idx_of, key=lambda k: (idx_of[k], -k))
        for a, b in oriented:
            if (a, b) == (mk_max, mk_min) or (a, b) == (mk_min, mk_max):
                closure = (a, b)
                break

    inversions = set()
    for a, b in oriented:
        if (a, b) == closure:
            continue
        if a in idx_of and b in idx_of and idx_of[b] <= idx_of[a]:
            inversions.add((a, b))

    if inversions:
        inv_idx = {i for i, e in enumerate(oriented) if e in inversions}
        ordered = sorted(inv_idx)
        runs, cur = [], [ordered[0]]
        for x in ordered[1:]:
            if x == cur[-1] + 1:
                cur.append(x)
            else:
                runs.append(cur)
                cur = [x]
        runs.append(cur)
        for run in runs:
            e_ids = []
            segs = []
            for s in run:
                a, b = oriented[s]
                e_ids.extend([a, b])
                segs.append("%s(序%d)→%s(序%d)" % (
                    by_id[a]["code"], idx_of[a],
                    by_id[b]["code"], idx_of[b]))
            e_ids = sorted(set(e_ids))
            issue("ORDER_INVERSION", "warning",
                  "轮次 %d 观测次序倒置（%d 条有效读数弧段）：%s，受影响弧段不计算"
                  % (rnd["seq"], len(run), "、".join(segs)),
                  round_ids=[rnd["id"]], marker_ids=e_ids,
                  reading_ids=[rdict[m]["id"] for m in e_ids if m in rdict],
                  resurvey={"type": "观测次序核查", "round_seq": rnd["seq"],
                            "markers": [by_id[m]["code"] for m in e_ids]})
    return inversions


def reduce_reading(rnd, elevation_m, marker, origin_ctx, alpha, href):
    """单读数归算到稳定高程系：h_r = H − H_原点 + Δ原点 + α·h_ref·T + c·L。"""
    org_h = origin_ctx["reading_m"]
    if org_h is None:
        return None, None
    H = float(elevation_m)
    rel = H - org_h
    d_o = origin_ctx["delta_m"]
    therm = 0.0 if marker["is_center"] else alpha * href * float(rnd["wall_temp_c"] or 0)
    load = float(rnd["load_coeff_m_per_m"] or 0) * float(rnd["liquid_level_m"] or 0)
    return rel + d_o + therm + load, {"origin_reading_m": org_h,
                                      "origin_source": origin_ctx["source"],
                                      "origin_delta_m": d_o,
                                      "thermal_m": therm, "load_m": load}


# ----------------------------------------------------------------------------
# 核心：两轮观测归算与沉降分离
# ----------------------------------------------------------------------------

def analyze_version(conn, version):
    tank = one(conn, "SELECT * FROM tank WHERE id=?", (version["tank_id"],))
    ra = one(conn, "SELECT * FROM round WHERE id=?", (version["round_a_id"],))
    rb = one(conn, "SELECT * FROM round WHERE id=?", (version["round_b_id"],))
    if not (tank and ra and rb):
        raise ApiError(400, "BAD_REFS", "罐体或观测轮次不存在")
    if ra["tank_id"] != tank["id"] or rb["tank_id"] != tank["id"]:
        raise ApiError(400, "BAD_REFS", "两轮观测必须属于同一罐体")

    params = dict(DEFAULT_PARAMS)
    if version["params"]:
        params.update(json.loads(version["params"]))
    excluded = json.loads(version["excluded_readings"] or "{}")
    excluded = {int(k): v for k, v in excluded.items()}

    issues = []

    def issue(code, severity, message, **refs):
        issues.append({
            "id": "I%02d" % (len(issues) + 1),
            "code": code,
            "severity": severity,           # fatal / warning / info
            "message": message,
            "round_ids": refs.get("round_ids", []),
            "marker_ids": refs.get("marker_ids", []),
            "reading_ids": refs.get("reading_ids", []),
            "benchmark_ids": refs.get("benchmark_ids", []),
            "edge": refs.get("edge"),
            "metric": refs.get("metric"),
            "value": refs.get("value"),
            "limit": refs.get("limit"),
            "resurvey": refs.get("resurvey"),
        })

    markers = allrows(conn, "SELECT * FROM marker WHERE tank_id=? ORDER BY azimuth_deg",
                      (tank["id"],))
    ring = [m for m in markers if not m["is_center"]]
    center = next((m for m in markers if m["is_center"]), None)
    by_id = {m["id"]: m for m in markers}
    radius = float(tank["radius_m"])

    def readings_of(rnd):
        out = {}
        for r in conn.execute("SELECT * FROM reading WHERE round_id=?", (rnd["id"],)):
            out[r["marker_id"]] = dict(r)
        return out

    rda, rdb = readings_of(ra), readings_of(rb)

    # -- 联系水准（各轮联测点，原点改选与跨轮稳定性都要用） -------------------
    ties = {}
    for rnd in (ra, rb):
        ties[rnd["id"]] = {t["benchmark_id"]: dict(t) for t in conn.execute(
            "SELECT * FROM round_tie WHERE round_id=?", (rnd["id"],))}
    ties_a, ties_b = ties[ra["id"]], ties[rb["id"]]

    # -- 原点与校准（逐轮确定，允许版本改选原点） -----------------------------
    origin = one(conn, "SELECT * FROM benchmark WHERE id=?",
                 ((version["origin_benchmark_id"] or ra["origin_benchmark_id"]),))
    switched_rounds = []
    for rnd in (ra, rb):
        if origin["id"] != rnd["origin_benchmark_id"]:
            switched_rounds.append(rnd)
    if switched_rounds:
        issue("ORIGIN_SWITCHED", "info",
              "本版本原点改选为 %s（理由：%s）；改选轮次 %s 按该轮联系水准重新归算"
              % (origin["code"], version["reason"],
                 "、".join(str(r["seq"]) for r in switched_rounds)),
              round_ids=[r["id"] for r in switched_rounds],
              benchmark_ids=[origin["id"]])

    round_fatal = {ra["id"]: [], rb["id"]: []}   # 每轮的致命问题代码
    origin_ctx = {}   # round_id -> 该轮实际原点高程/改正/来源

    def resolve_origin(rnd):
        """逐轮确定实际原点读数、稳定系改正与来源（见 qc_resolve_origin）。"""
        origin_ctx[rnd["id"]] = qc_resolve_origin(
            conn, rnd, origin, ties[rnd["id"]], issue, round_fatal[rnd["id"]])

    resolve_origin(ra)
    resolve_origin(rb)

    d_o_a = origin_ctx[ra["id"]]["delta_m"]
    d_o_b = origin_ctx[rb["id"]]["delta_m"]
    cal_a = origin_ctx[ra["id"]]["has_cal"]
    cal_b = origin_ctx[rb["id"]]["has_cal"]

    # -- 环线闭合差 -----------------------------------------------------------
    def check_loop(rnd):
        qc_check_loop(rnd, params, issue, round_fatal[rnd["id"]])

    check_loop(ra)
    check_loop(rb)

    # -- 联系水准：原点稳定性（跨轮，按版本实际原点逐轮归算） -----------------
    rel_changes = []
    for bid in sorted(set(ties_a) & set(ties_b)):
        if bid == origin["id"]:
            continue   # 原点自身不作校核点
        bm = one(conn, "SELECT * FROM benchmark WHERE id=?", (bid,))
        dja, has_a = cal_delta(conn, bid, ra["measured_at"])
        djb, has_b = cal_delta(conn, bid, rb["measured_at"])
        if not bm["assumed_stable"]:
            dja = ties_a[bid]["delta_stable_m"] if ties_a[bid]["delta_stable_m"] is not None else dja
            djb = ties_b[bid]["delta_stable_m"] if ties_b[bid]["delta_stable_m"] is not None else djb
        org_a = origin_ctx[ra["id"]]["reading_m"]
        org_b = origin_ctx[rb["id"]]["reading_m"]
        if org_a is None or org_b is None:
            continue
        ch = (ties_b[bid]["tie_elevation_m"] - org_b) \
             - (ties_a[bid]["tie_elevation_m"] - org_a) \
             - (djb - dja)
        rel_changes.append((bid, bm["code"], ch))
    if rel_changes:
        vals = [c for _, _, c in rel_changes]
        drift = median(vals)
        spread = max(vals) - min(vals)
        if abs(drift) > params["origin_drift_m"]:
            issue("ORIGIN_UNSTABLE", "fatal",
                  "联系水准显示原点两轮间漂移 %.1f mm（允许 %.1f mm），弧段不计算"
                  % (drift * 1000, params["origin_drift_m"] * 1000),
                  round_ids=[ra["id"], rb["id"]],
                  benchmark_ids=[origin["id"]] + [b for b, _, _ in rel_changes],
                  metric="origin_drift_m", value=drift,
                  limit=params["origin_drift_m"],
                  resurvey={"type": "原点及联系点重新联测",
                            "benchmark": origin["code"],
                            "ties": [c for _, c, _ in rel_changes]})
            round_fatal[ra["id"]].append("ORIGIN_UNSTABLE")
            round_fatal[rb["id"]].append("ORIGIN_UNSTABLE")
        if spread > params["tie_spread_m"]:
            issue("TIE_SPREAD", "warning",
                  "联系点相对变化互差 %.1f mm 超限（%.1f mm）"
                  % (spread * 1000, params["tie_spread_m"] * 1000),
                  round_ids=[ra["id"], rb["id"]],
                  benchmark_ids=[b for b, _, _ in rel_changes],
                  metric="tie_spread_m", value=spread, limit=params["tie_spread_m"])
    else:
        issue("TIE_MISSING", "info",
              "两轮无公共联系点，原点稳定性无法跨轮校核（仅按校准资料归算）",
              round_ids=[ra["id"], rb["id"]])

    # -- 方位重号（主数据） ---------------------------------------------------
    dup_marker_ids = set()
    seen_az = {}
    for m in ring:
        az = float(m["azimuth_deg"]) % 360.0
        seen_az.setdefault(az, []).append(m["id"])
    for az, ids in seen_az.items():
        if len(ids) > 1:
            dup_marker_ids.update(ids)
            issue("DUPLICATE_AZIMUTH", "warning",
                  "方位 %.1f° 存在重号标志 %s，相关弧段不计算"
                  % (az, "、".join(by_id[i]["code"] for i in ids)),
                  marker_ids=ids, round_ids=[ra["id"], rb["id"]],
                  metric="azimuth_deg", value=az,
                  resurvey={"type": "方位核查/重新编号", "azimuth_deg": az,
                            "markers": [by_id[i]["code"] for i in ids]})

    # -- 每轮：漏测 + 观测次序倒置（次序校验只认逐轮 reading.order_idx，
    #    且剔除读数不参与次序判定） ------------------------------------------
    observed = {ra["id"]: set(rda), rb["id"]: set(rdb)}

    def excluded_in_round(rnd):
        rows = rda if rnd["id"] == ra["id"] else rdb
        return {m for m, row in rows.items() if row["id"] in excluded}

    def order_check(rnd, rdict):
        """按逐轮 reading.order_idx 检查环向观测次序（见 qc_order_inversions）。"""
        return qc_order_inversions(rnd, rdict, ring, excluded_in_round(rnd),
                                   by_id, issue)

    inv_edges = order_check(ra, rda) | order_check(rb, rdb)

    for m in ring:
        for rnd, rdict in ((ra, rda), (rb, rdb)):
            if m["id"] not in rdict:
                issue("MISSING", "warning",
                      "标志 %s（%.1f°）在轮次 %d 漏测，相邻弧段不计算"
                      % (m["code"], m["azimuth_deg"], rnd["seq"]),
                      round_ids=[rnd["id"]], marker_ids=[m["id"]],
                      resurvey={"type": "漏测补测", "round_seq": rnd["seq"],
                                "marker": m["code"], "azimuth_deg": m["azimuth_deg"]})

    for rid, reason in excluded.items():
        rr = one(conn, "SELECT * FROM reading WHERE id=?", (rid,))
        if rr:
            mk = by_id.get(rr["marker_id"])
        issue("READING_EXCLUDED", "info",
              "读数 #%s%s 经版本剔除：%s"
              % (rid, "（标志 %s）" % mk["code"] if mk else "", reason),
              round_ids=[rr["round_id"]] if rr else [],
              marker_ids=[rr["marker_id"]] if rr else [], reading_ids=[rid],
              resurvey=({"type": "剔除读数复测", "marker": mk["code"],
                         "azimuth_deg": mk["azimuth_deg"]} if mk else None))

    # -- 归算到稳定高程系（温度/液位/原点改正） -------------------------------
    alpha = float(tank["wall_alpha"])
    href = float(tank["ref_height_m"] or 0)

    def reduce(rnd, reading_row, m):
        return reduce_reading(rnd, reading_row["elevation_m"], m,
                              origin_ctx[rnd["id"]], alpha, href)

    corr_a, corr_b = {}, {}
    corr_terms = {}
    for rnd, rdict, store in ((ra, rda, corr_a), (rb, rdb, corr_b)):
        terms = {}
        if origin_ctx[rnd["id"]]["reading_m"] is None:
            corr_terms[rnd["id"]] = terms
            continue
        for m in ring:
            if m["id"] in rdict:
                store[m["id"]], terms[m["id"]] = reduce(rnd, rdict[m["id"]], m)
        if center and center["id"] in rdict:
            store[center["id"]], terms[center["id"]] = reduce(rnd, rdict[center["id"]], center)
        corr_terms[rnd["id"]] = terms

    # -- 逐点沉降量 -----------------------------------------------------------
    points = []
    for m in ring:
        ra_rd = rda.get(m["id"])
        rb_rd = rdb.get(m["id"])
        is_excl = bool({ra_rd["id"] if ra_rd else None,
                        rb_rd["id"] if rb_rd else None} & set(excluded))
        spatial_ok = (m["id"] not in dup_marker_ids and not is_excl)
        s = None
        if m["id"] in corr_a and m["id"] in corr_b and not is_excl:
            s = corr_a[m["id"]] - corr_b[m["id"]]   # 正为下沉
        points.append({
            "marker_id": m["id"], "code": m["code"],
            "azimuth_deg": m["azimuth_deg"], "ring_order": m["ring_order"],
            "reading_a_id": ra_rd["id"] if ra_rd else None,
            "reading_b_id": rb_rd["id"] if rb_rd else None,
            "elevation_a_m": ra_rd["elevation_m"] if ra_rd else None,
            "elevation_b_m": rb_rd["elevation_m"] if rb_rd else None,
            "excluded": is_excl,
            "spatial_ok": spatial_ok and s is not None,
            "corrected_a_m": corr_a.get(m["id"]),
            "corrected_b_m": corr_b.get(m["id"]),
            "settlement_m": s,
        })

    center_point = None
    if center and center["id"] in corr_a and center["id"] in corr_b:
        center_point = {
            "marker_id": center["id"], "code": center["code"],
            "settlement_m": corr_a[center["id"]] - corr_b[center["id"]],
            "corrected_a_m": corr_a[center["id"]],
            "corrected_b_m": corr_b[center["id"]],
        }

    # -- 整体升降 + 倾斜（一阶刚体）最小二乘 ----------------------------------
    valid = [p for p in points if p["spatial_ok"]]
    decomposition = None
    tilt = {"amp_m": None, "azimuth_deg": None, "diameter_diff_m": None,
            "slope": None}
    for p in points:
        p["model_m"] = None
        p["residual_m"] = None
        p["harmonics"] = {}

    if len(valid) >= 4 and not (round_fatal[ra["id"]] or round_fatal[rb["id"]]):
        thetas = [math.radians(p["azimuth_deg"]) for p in valid]
        X = [[1.0, math.cos(t), math.sin(t)] for t in thetas]
        y = [p["settlement_m"] for p in valid]
        beta = lls(X, y)
        a0, ax, ay = beta
        amp = math.hypot(ax, ay)
        az_tilt = math.degrees(math.atan2(ay, ax)) % 360.0
        decomposition = {"body_m": a0, "tilt_cos_m": ax, "tilt_sin_m": ay}
        tilt = {"amp_m": amp, "azimuth_deg": az_tilt,
                "diameter_diff_m": 2 * amp,
                "slope": amp / radius if radius else None}

        if abs(a0) > params["total_settlement_m"]:
            issue("TOTAL_SETTLEMENT", "warning",
                  "整体升降 %.1f mm 超过允许 %.1f mm"
                  % (a0 * 1000, params["total_settlement_m"] * 1000),
                  round_ids=[ra["id"], rb["id"]], metric="body_m",
                  value=a0, limit=params["total_settlement_m"])
        if tilt["diameter_diff_m"] > params["diameter_diff_m"]:
            issue("DIAMETER_DIFF", "warning",
                  "直径方向差异沉降 %.1f mm（倾斜方位 %.1f°）超过允许 %.1f mm"
                  % (tilt["diameter_diff_m"] * 1000, az_tilt,
                     params["diameter_diff_m"] * 1000),
                  round_ids=[ra["id"], rb["id"]], metric="diameter_diff_m",
                  value=tilt["diameter_diff_m"], limit=params["diameter_diff_m"])

        for p, t in zip(valid, thetas):
            model = a0 + ax * math.cos(t) + ay * math.sin(t)
            p["model_m"] = model
            p["residual_m"] = p["settlement_m"] - model

        # -- 残差谐波（2..K 阶） --
        K = min(int(params["harmonic_max"]),
                max(1, (len(valid) - 3) // 2))
        harmonics_out = {}
        if K >= 2:
            cols, names = [], []
            for k in range(2, K + 1):
                cols.append([math.cos(k * t) for t in thetas])
                cols.append([math.sin(k * t) for t in thetas])
                names.append(k)
            Xh = [list(row) for row in zip(*cols)]
            bh = lls(Xh, [p["residual_m"] for p in valid])
            for idx, k in enumerate(names):
                ck, sk = bh[2 * idx], bh[2 * idx + 1]
                ampk = math.hypot(ck, sk)
                phase = math.degrees(math.atan2(sk, ck) / k) % (360.0 / k)
                harmonics_out[str(k)] = {"amp_m": ampk, "cos_m": ck,
                                         "sin_m": sk, "phase_deg": phase}
                if ampk > params["harmonic_amp_m"]:
                    issue("HARMONIC_AMPLITUDE", "warning",
                          "%d 阶谐波振幅 %.1f mm 超过允许 %.1f mm（相位 %.1f°）"
                          % (k, ampk * 1000, params["harmonic_amp_m"] * 1000, phase),
                          round_ids=[ra["id"], rb["id"]],
                          metric="harmonic_%d_amp_m" % k, value=ampk,
                          limit=params["harmonic_amp_m"])
            for p, t in zip(valid, thetas):
                for idx, k in enumerate(names):
                    ck, sk = bh[2 * idx], bh[2 * idx + 1]
                    p["harmonics"][str(k)] = ck * math.cos(k * t) + sk * math.sin(k * t)
        else:
            harmonics_out = {}
            if len(valid) < 7:
                issue("RESOLUTION_REDUCED", "info",
                      "有效点仅 %d 个，谐波阶数受限" % len(valid),
                      round_ids=[ra["id"], rb["id"]])

        for p in valid:
            if abs(p["settlement_m"]) > params["total_settlement_m"]:
                issue("POINT_SETTLEMENT", "warning",
                      "标志 %s 沉降 %.1f mm 超过允许 %.1f mm"
                      % (p["code"], p["settlement_m"] * 1000,
                         params["total_settlement_m"] * 1000),
                      round_ids=[ra["id"], rb["id"]], marker_ids=[p["marker_id"]],
                      reading_ids=[p["reading_a_id"], p["reading_b_id"]],
                      metric="settlement_m", value=p["settlement_m"],
                      limit=params["total_settlement_m"])
    else:
        harmonics_out = {}
        if len(valid) < 4 and not (round_fatal[ra["id"]] or round_fatal[rb["id"]]):
            issue("INSUFFICIENT_POINTS", "fatal",
                  "有效空间点仅 %d 个（<4），无法分离倾斜与局部沉降" % len(valid),
                  round_ids=[ra["id"], rb["id"]])

    # -- 弧段（环向邻边） -----------------------------------------------------
    n = len(ring)
    edges = []
    for i in range(n):
        m1, m2 = ring[i], ring[(i + 1) % n]
        span = fwd_angle(float(m1["azimuth_deg"]), float(m2["azimuth_deg"]))
        chord = 2 * radius * math.sin(math.radians(span) / 2) if radius else None
        reasons = []
        for rnd in (ra, rb):
            if round_fatal[rnd["id"]]:
                reasons.append("轮次%d:%s" % (rnd["seq"],
                                             "/".join(round_fatal[rnd["id"]])))
            if m1["id"] not in observed[rnd["id"]]:
                reasons.append("轮次%d漏测:%s" % (rnd["seq"], m1["code"]))
            if m2["id"] not in observed[rnd["id"]]:
                reasons.append("轮次%d漏测:%s" % (rnd["seq"], m2["code"]))
        if m1["id"] in dup_marker_ids or m2["id"] in dup_marker_ids:
            reasons.append("方位重号")
        if (m1["id"], m2["id"]) in inv_edges:
            reasons.append("观测次序倒置")
        for m in (m1, m2):
            for rdict in (rda, rdb):
                row = rdict.get(m["id"])
                if row and row["id"] in excluded:
                    reasons.append("读数#%d已剔除" % row["id"])
        blocked = bool(reasons)
        p1 = next(p for p in points if p["marker_id"] == m1["id"])
        p2 = next(p for p in points if p["marker_id"] == m2["id"])
        diff = None
        if not blocked and p1["settlement_m"] is not None and \
                p2["settlement_m"] is not None:
            diff = p2["settlement_m"] - p1["settlement_m"]
            if abs(diff) > params["adjacent_diff_m"]:
                issue("ADJACENT_DIFF", "warning",
                      "弧段 %s→%s 邻点沉降差 %.1f mm 超过允许 %.1f mm"
                      % (m1["code"], m2["code"], diff * 1000,
                         params["adjacent_diff_m"] * 1000),
                      round_ids=[ra["id"], rb["id"]],
                      marker_ids=[m1["id"], m2["id"]],
                      reading_ids=[p1["reading_a_id"], p1["reading_b_id"],
                                   p2["reading_a_id"], p2["reading_b_id"]],
                      edge=[m1["id"], m2["id"]], metric="adjacent_diff_m",
                      value=diff, limit=params["adjacent_diff_m"])
        tang = None
        if not blocked and chord and p1["corrected_b_m"] is not None \
                and p2["corrected_b_m"] is not None:
            tang = (p2["corrected_b_m"] - p1["corrected_b_m"]) / chord
        edges.append({"from_marker_id": m1["id"], "to_marker_id": m2["id"],
                      "from_code": m1["code"], "to_code": m2["code"],
                      "span_deg": span, "chord_m": chord,
                      "blocked": blocked, "block_reasons": reasons,
                      "adjacent_diff_m": diff, "tangent_slope": tang})

    # -- 空缺弧统计 -----------------------------------------------------------
    longest = 0
    longest_run = None
    i = 0
    gap_runs = []
    any_real_edge = any(not e["blocked"] for e in edges)
    while i < n:
        if edges[i]["blocked"]:
            j = i
            while edges[j % n]["blocked"]:
                j += 1
                if j - i >= n:
                    break
            run = list(range(i, j))
            if len(run) < n:
                gap_runs.append(run)
                arc = fwd_angle(float(ring[run[0]]["azimuth_deg"]),
                                float(ring[run[(len(run) - 1)] % n]["azimuth_deg"]))
                if len(run) == 1:
                    arc = edges[run[0]]["span_deg"]
                if arc > longest:
                    longest, longest_run = arc, run
            i = j
        else:
            i += 1

    missing_count = sum(1 for m in ring
                        if m["id"] not in observed[ra["id"]]
                        or m["id"] not in observed[rb["id"]])
    miss_frac = missing_count / max(n, 1)

    if any_real_edge and longest_run and longest > params["max_gap_deg"]:
        first_m = ring[longest_run[0]]
        last_m = ring[longest_run[(len(longest_run) - 1)] % n]
        mid_az = (float(first_m["azimuth_deg"]) + longest / 2) % 360
        issue("GAP_TOO_LONG", "fatal",
              "%.1f°–%.1f° 间空缺弧 %.1f° 超过允许 %.1f°，弧中需补测"
              % (first_m["azimuth_deg"], last_m["azimuth_deg"],
                 longest, params["max_gap_deg"]),
              round_ids=[ra["id"], rb["id"]],
              edge=[first_m["id"], last_m["id"]], metric="gap_arc_deg",
              value=longest, limit=params["max_gap_deg"],
              resurvey={"type": "空缺弧中部补测", "azimuth_deg": mid_az,
                        "arc_deg": longest})
    if miss_frac > params["max_missing_fraction"]:
        issue("MISSING_FRACTION", "warning",
              "漏测标志比例 %.0f%% 超过允许 %.0f%%"
              % (miss_frac * 100, params["max_missing_fraction"] * 100),
              round_ids=[ra["id"], rb["id"]], metric="missing_fraction",
              value=miss_frac, limit=params["max_missing_fraction"])

    # -- 底板排水坡度 ---------------------------------------------------------
    slopes = {"radial": None, "tangential_max_abs": None}
    if center_point and radius and not (round_fatal[ra["id"]] or round_fatal[rb["id"]]):
        radial_vals = []
        for p in valid:
            if p["corrected_b_m"] is not None:
                v = (p["corrected_b_m"] - center_point["corrected_b_m"]) / radius
                chg = (center_point["settlement_m"] - p["settlement_m"]) / radius
                radial_vals.append({"marker_id": p["marker_id"], "code": p["code"],
                                    "slope": v, "change": chg})
        slopes["radial"] = radial_vals
        if radial_vals:
            mn = min(x["slope"] for x in radial_vals)
            slopes["radial_min"] = mn
            if mn < float(tank["bottom_slope_spec"]):
                bad = min(radial_vals, key=lambda x: x["slope"])
                issue("BOTTOM_SLOPE_LOW", "warning",
                      "标志 %s 处底板向心坡度 1:%.0f 小于设计 1:%.0f"
                      % (bad["code"],
                         (1 / bad["slope"]) if bad["slope"] > 0 else float("inf"),
                         1 / float(tank["bottom_slope_spec"])),
                      round_ids=[ra["id"], rb["id"]], marker_ids=[bad["marker_id"]],
                      metric="radial_slope", value=mn,
                      limit=float(tank["bottom_slope_spec"]))
            if mn < 0:
                issue("BOTTOM_SLOPE_REVERSED", "warning",
                      "罐底出现反坡（倒泛水），最小坡度 %.4f" % mn,
                      round_ids=[ra["id"], rb["id"]], metric="radial_slope", value=mn,
                      limit=0)
    tang_vals = [abs(e["tangent_slope"]) for e in edges
                 if e["tangent_slope"] is not None]
    if tang_vals:
        slopes["tangential_max_abs"] = max(tang_vals)

    # -- 补测位置汇总 ---------------------------------------------------------
    resurvey = []
    for iss in issues:
        rs = iss.get("resurvey")
        if rs:
            resurvey.append({"issue_id": iss["id"], "code": iss["code"], **rs})

    fatal = [i for i in issues if i["severity"] == "fatal"]
    result = {
        "version_id": version["id"],
        "tank": {"id": tank["id"], "name": tank["name"], "radius_m": tank["radius_m"]},
        "rounds": {
            "a": _round_brief(ra, origin, origin_ctx[ra["id"]],
                              corr_terms.get(ra["id"], {}), ring),
            "b": _round_brief(rb, origin, origin_ctx[rb["id"]],
                              corr_terms.get(rb["id"], {}), ring),
        },
        "params": params,
        "points": points,
        "center_point": center_point,
        "edges": edges,
        "decomposition": decomposition,
        "tilt": tilt,
        "harmonics": harmonics_out,
        "slopes": slopes,
        "gap": {"longest_blocked_arc_deg": longest,
                "missing_fraction": miss_frac},
        "issues": issues,
        "resurvey": resurvey,
        "generated_at": now_iso(),
    }
    result["status"] = "rejected" if fatal else "draft"
    return result


def _round_brief(rnd, origin, ctx, terms, ring):
    used = [terms.get(m["id"], {}) for m in ring]
    return {
        "id": rnd["id"], "seq": rnd["seq"], "measured_at": rnd["measured_at"],
        "origin_benchmark": origin["code"],
        "origin_reading_m": ctx["reading_m"],
        "origin_source": ctx["source"],
        "origin_record_benchmark_id": rnd["origin_benchmark_id"],
        "origin_switched": origin["id"] != rnd["origin_benchmark_id"],
        "origin_delta_m": ctx["delta_m"],
        "wall_temp_c": rnd["wall_temp_c"],
        "liquid_level_m": rnd["liquid_level_m"],
        "load_coeff_m_per_m": rnd["load_coeff_m_per_m"],
        "thermal_m_typical": used[0].get("thermal_m") if used else None,
        "load_m_typical": used[0].get("load_m") if used else None,
        "loop_misclosure_m": rnd["loop_misclosure_m"],
        "loop_length_km": rnd["loop_length_km"],
    }


# ----------------------------------------------------------------------------
# 导出：复算 JSON / 补测 CSV / 环向沉降 SVG
# ----------------------------------------------------------------------------

def build_resurvey_csv(result):
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["序号", "罐体", "问题代码", "补测类型", "轮次", "标志/基准点",
                "方位角(°)", "位置说明"])
    tank_name = result["tank"]["name"]
    for i, r in enumerate(result["resurvey"], 1):
        seq = r.get("round_seq", "")
        target = r.get("marker") or r.get("benchmark") or \
            "、".join(r.get("markers") or r.get("ties") or []) or ""
        w.writerow([i, tank_name, r["code"], r.get("type", ""), seq, target,
                    r.get("azimuth_deg", ""),
                    r.get("where", "") or r.get("type", "")])
    return buf.getvalue().encode("utf-8")


def build_svg(result):
    pts = [p for p in result["points"]]
    valid = [p for p in pts if p["settlement_m"] is not None and not p["excluded"]]
    W, H = 860, 620
    cx, cy, R = 300, 310, 185
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" font-family="sans-serif">' % (W, H, W, H)]
    parts.append('<rect width="100%" height="100%" fill="white"/>')
    parts.append('<text x="20" y="34" font-size="20" font-weight="bold">%s 罐底环向沉降图（轮次 %s → %s）</text>'
                 % (result["tank"]["name"], result["rounds"]["a"]["seq"],
                    result["rounds"]["b"]["seq"]))

    if not valid:
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#b00">该版本存在致命问题，弧段未计算</text>'
                     % (cx, cy))
        parts.append("</svg>")
        return "\n".join(parts)

    smax = max(abs(p["settlement_m"]) for p in valid) or 1.0
    k = 110.0 / smax
    base_ring = []
    for deg in range(0, 360, 2):
        t = math.radians(deg)
        base_ring.append((cx + R * math.sin(t), cy - R * math.cos(t)))
    parts.append('<polygon points="%s" fill="none" stroke="#bbb" stroke-dasharray="4 4"/>'
                 % " ".join("%.1f,%.1f" % q for q in base_ring))
    for rr, lab in ((R - 55, "-%.0fmm" % (55 / k * 1000)),
                    (R + 55, "+%.0fmm" % (55 / k * 1000))):
        if rr > 20:
            ring = []
            for deg in range(0, 360, 2):
                t = math.radians(deg)
                ring.append((cx + rr * math.sin(t), cy - rr * math.cos(t)))
            parts.append('<polygon points="%s" fill="none" stroke="#e5e5e5"/>'
                         % " ".join("%.1f,%.1f" % q for q in ring))
            parts.append('<text x="%d" y="%d" font-size="10" fill="#999">%s</text>'
                         % (cx + 3, cy - rr + 3, lab))

    edges = result["edges"]
    blocked_pairs = {(e["from_marker_id"], e["to_marker_id"]): e["blocked"]
                     for e in edges}

    def xy(p):
        t = math.radians(p["azimuth_deg"])
        rr = R + k * p["settlement_m"]
        return cx + rr * math.sin(t), cy - rr * math.cos(t)

    n = len(pts)
    for i in range(n):
        p1, p2 = pts[i], pts[(i + 1) % n]
        if p1["settlement_m"] is None or p2["settlement_m"] is None:
            continue
        blocked = blocked_pairs.get((p1["marker_id"], p2["marker_id"]), True)
        x1, y1 = xy(p1)
        x2, y2 = xy(p2)
        if blocked:
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                         'stroke="#ccc" stroke-dasharray="6 5"/>' % (x1, y1, x2, y2))
        else:
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                         'stroke="#1f5fbf" stroke-width="2"/>' % (x1, y1, x2, y2))

    adj_lim = result["params"]["adjacent_diff_m"]
    bad_adj = {i["marker_ids"] for i in result["issues"]
               if i["code"] == "ADJACENT_DIFF"}
    bad_markers = {m for pair in bad_adj for m in (pair or [])}
    for p in pts:
        t = math.radians(p["azimuth_deg"])
        lx, ly = cx + (R + 135) * math.sin(t), cy - (R + 135) * math.cos(t)
        if p["settlement_m"] is None:
            x, y = cx + R * math.sin(t), cy - R * math.cos(t)
            parts.append('<rect x="%.1f" y="%.1f" width="9" height="9" fill="white" '
                         'stroke="#c00" stroke-width="2" transform="rotate(45 %.1f %.1f)"/>'
                         % (x - 4.5, y - 4.5, x, y))
            parts.append('<text x="%.1f" y="%.1f" font-size="9" fill="#c00" '
                         'text-anchor="middle">缺</text>' % (lx, ly))
            continue
        x, y = xy(p)
        col = "#d33" if p["marker_id"] in bad_markers else \
              ("#e80" if abs(p["settlement_m"]) > result["params"]["total_settlement_m"]
               else "#0a7")
        parts.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s"/>' % (x, y, col))
        if n <= 24 or p["ring_order"] in (1,):
            anchor = "middle"
            parts.append('<text x="%.1f" y="%.1f" font-size="10" fill="#333" '
                         'text-anchor="%s">%s</text>' % (lx, ly, anchor, p["code"]))

    tilt = result.get("tilt") or {}
    if tilt.get("azimuth_deg") is not None:
        t = math.radians(tilt["azimuth_deg"])
        parts.append('<line x1="%d" y1="%d" x2="%.1f" y2="%.1f" stroke="#c60" '
                     'stroke-width="2" marker-end="url(#arrow)"/>'
                     % (cx, cy, cx + (R - 70) * math.sin(t),
                        cy - (R - 70) * math.cos(t)))
        parts.append('<text x="%.1f" y="%.1f" font-size="11" fill="#c60">倾斜方向 %.0f°</text>'
                     % (cx + (R - 58) * math.sin(t),
                        cy - (R - 58) * math.cos(t) - 6, tilt["azimuth_deg"]))
    parts.append('<defs><marker id="arrow" markerWidth="10" markerHeight="10" '
                 'refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" '
                 'fill="#c60"/></marker></defs>')

    lx0, ly0 = 620, 110
    legend = [
        ("#1f5fbf", "有效弧段沉降线"),
        ("#ccc", "阻断/漏测弧段"),
        ("#0a7", "标志点"),
        ("#e80", "单点沉降超限"),
        ("#d33", "邻点差超限"),
        ("#c60", "整体倾斜方向"),
        ("#bbb", "平均高程基准圆"),
    ]
    parts.append('<text x="%d" y="%d" font-size="14" font-weight="bold">图例</text>'
                 % (lx0, ly0 - 24))
    for i, (c, lab) in enumerate(legend):
        y = ly0 + i * 26
        parts.append('<rect x="%d" y="%d" width="16" height="6" fill="%s"/>'
                     % (lx0, y - 5, c))
        parts.append('<text x="%d" y="%d" font-size="12">%s</text>'
                     % (lx0 + 24, y, lab))

    dec = result.get("decomposition")
    if dec:
        y = ly0 + len(legend) * 26 + 20
        lines = [
            "整体升降: %+.1f mm" % (dec["body_m"] * 1000),
            "直径差异沉降: %.1f mm" % (tilt["diameter_diff_m"] * 1000),
            "倾斜坡度: 1:%.0f" % (1 / tilt["slope"]) if tilt["slope"] else "-",
            "比例尺: 圆环 1 mm ≡ %.1f px" % k,
        ]
        for s in lines:
            parts.append('<text x="%d" y="%d" font-size="12">%s</text>'
                         % (lx0, y, s))
            y += 22

    warns = [i for i in result["issues"] if i["severity"] in ("fatal", "warning")]
    y = H - 30 - max(0, len(warns) - 1) * 0
    parts.append('<text x="20" y="%d" font-size="12" fill="#a00">问题 %d 项（致命 %d，详见复算 JSON）</text>'
                 % (H - 24, len(result["issues"]),
                    len([i for i in result["issues"] if i["severity"] == "fatal"])))
    parts.append("</svg>")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# 分级加载（水压试验）：方案—修订—引擎—成果
# ----------------------------------------------------------------------------

HYDRO_PARAMS = {
    "gravity_m_s2": 9.80665,     # 重力加速度
    "level_tolerance_m": 0.05,   # 荷载方向判定的液位容差
    "target_tolerance_m": 0.10,  # 充水液位与目标液位的允许偏差
}

# 方案各级必填字段 / 修订允许改取的阈值字段
HYDRO_STAGE_FIELDS = ("target_level_m", "min_hold_hours",
                      "rate_limit_m_per_h", "residual_limit_m")
HYDRO_THRESHOLD_FIELDS = ("min_hold_hours", "rate_limit_m_per_h",
                          "residual_limit_m")

HYDRO_ROLE_CN = {"empty": "空罐", "fill": "充水", "hold": "保压",
                 "unload": "卸载", "recheck": "复测"}


def parse_ts(s):
    """ISO 日期时间 → datetime（naive 视为 UTC）。"""
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_between(t1, t2):
    """两个 ISO 时间的小时差。"""
    return (parse_ts(t2) - parse_ts(t1)).total_seconds() / 3600.0


def _role_label(role, stage_seq):
    lab = HYDRO_ROLE_CN.get(role, role)
    return "%s（第%d级）" % (lab, stage_seq) if role in ("fill", "hold") else lab


def hydro_slots(bindings, n_stages):
    """绑定 JSON 展开为有序槽位 [(role, stage_seq, round_id)]；缺槽为 None。"""
    rstages = bindings.get("stages") or []
    slots = [("empty", 0, bindings.get("empty"))]
    for i in range(n_stages):
        st = rstages[i] if i < len(rstages) and isinstance(rstages[i], dict) else {}
        slots.append(("fill", i + 1, st.get("fill")))
        slots.append(("hold", i + 1, st.get("hold")))
    slots.append(("unload", 0, bindings.get("unload")))
    slots.append(("recheck", 0, bindings.get("recheck")))
    return slots


def analyze_hydro_revision(conn, test, stages, revision):
    """分级加载试验引擎。

    复用逐轮归算（原点/校准/温度/液位改正）与质检（环线闭合差、原点稳定、
    方位重号、观测次序、漏测）；按槽位顺序以液位×密度换算荷载，逐标志计算
    各级增量、保压速率、卸载回弹率、残余沉降与加载—卸载滞回。
    致命问题 → 状态 pending（待判）；速率/残余越限 → warning 回指阶段与读数。
    """
    tank = one(conn, "SELECT * FROM tank WHERE id=?", (test["tank_id"],))
    if not tank:
        raise ApiError(400, "BAD_REFS", "罐体不存在")
    bindings = json.loads(revision["bindings"])
    thresholds = json.loads(revision["thresholds"] or "{}")
    params = dict(HYDRO_PARAMS)
    params.update(thresholds.get("params") or {})
    qc_params = dict(DEFAULT_PARAMS)

    issues = []

    def issue(code, severity, message, **refs):
        issues.append({
            "id": "I%02d" % (len(issues) + 1),
            "code": code,
            "severity": severity,           # fatal(待判) / warning / info
            "message": message,
            "stage_seq": refs.get("stage_seq"),
            "round_ids": refs.get("round_ids", []),
            "marker_ids": refs.get("marker_ids", []),
            "reading_ids": refs.get("reading_ids", []),
            "benchmark_ids": refs.get("benchmark_ids", []),
            "metric": refs.get("metric"),
            "value": refs.get("value"),
            "limit": refs.get("limit"),
            "resurvey": refs.get("resurvey"),
        })

    n_stages = len(stages)
    # 有效阈值 = 方案值被本修订 thresholds 覆盖（改取阈值留痕于 result）
    eff = []
    for st in stages:
        e = {f: float(st[f]) for f in HYDRO_STAGE_FIELDS}
        ov = (thresholds.get("stages") or {}).get(str(st["seq"])) or {}
        for f in HYDRO_THRESHOLD_FIELDS:
            if ov.get(f) is not None:
                e[f] = float(ov[f])
        eff.append(e)

    slots = hydro_slots(bindings, n_stages)
    if len(bindings.get("stages") or []) != n_stages:
        issue("BINDING_INCOMPLETE", "fatal",
              "绑定级数 %d 与方案级数 %d 不符"
              % (len(bindings.get("stages") or []), n_stages))

    # -- 槽位 → 轮次 ---------------------------------------------------------
    slot_rounds = {}
    for role, sseq, rid in slots:
        if rid is None:
            issue("BINDING_INCOMPLETE", "fatal",
                  "槽位 %s 未绑定观测轮次" % _role_label(role, sseq),
                  stage_seq=sseq or None)
            continue
        rnd = one(conn, "SELECT * FROM round WHERE id=?", (rid,))
        if not rnd or rnd["tank_id"] != tank["id"]:
            raise ApiError(400, "BAD_ROUND", "轮次 %s 不存在或不属于该罐" % rid)
        slot_rounds[(role, sseq)] = rnd

    # 同一轮次重复绑定
    seen = {}
    for role, sseq, rid in slots:
        if rid is not None:
            seen.setdefault(rid, []).append(_role_label(role, sseq))
    for rid, labels in seen.items():
        if len(labels) > 1:
            rnd = one(conn, "SELECT seq FROM round WHERE id=?", (rid,))
            issue("DUPLICATE_BINDING", "fatal",
                  "轮次 %d 被重复绑定到 %s"
                  % (rnd["seq"] if rnd else rid, "、".join(labels)),
                  round_ids=[rid])

    # 阶段倒序：槽位观测时间必须严格递增
    prev = None
    for role, sseq, rid in slots:
        rnd = slot_rounds.get((role, sseq))
        if rnd is None:
            continue
        if prev is not None and parse_ts(rnd["measured_at"]) <= \
                parse_ts(prev[1]["measured_at"]):
            issue("STAGE_ORDER", "fatal",
                  "槽位 %s 轮次 %d（%s）不晚于 %s 轮次 %d（%s），阶段顺序倒置"
                  % (_role_label(role, sseq), rnd["seq"], rnd["measured_at"],
                     _role_label(*prev[0]), prev[1]["seq"],
                     prev[1]["measured_at"]),
                  stage_seq=sseq or None, round_ids=[prev[1]["id"], rnd["id"]])
        prev = ((role, sseq), rnd)

    # -- 荷载方向（液位单调性） ----------------------------------------------
    tol = params["level_tolerance_m"]

    def lvl(r):
        return float(r["liquid_level_m"] or 0)

    empty_r = slot_rounds.get(("empty", 0))
    if empty_r is not None and lvl(empty_r) > tol:
        issue("LOAD_DIRECTION", "fatal",
              "空罐轮次 %d 液位 %.2f m 不为零，与空罐工况不符"
              % (empty_r["seq"], lvl(empty_r)),
              round_ids=[empty_r["id"]], metric="liquid_level_m",
              value=lvl(empty_r), limit=tol)
    for i in range(1, n_stages + 1):
        fr = slot_rounds.get(("fill", i))
        hr = slot_rounds.get(("hold", i))
        prev_r = slot_rounds.get(("hold", i - 1)) if i > 1 else empty_r
        if fr is not None and prev_r is not None and lvl(fr) <= lvl(prev_r) + tol:
            issue("LOAD_DIRECTION", "fatal",
                  "第 %d 级充水轮次 %d 液位 %.2f m 未高于上一级 %.2f m，荷载方向不符"
                  % (i, fr["seq"], lvl(fr), lvl(prev_r)),
                  stage_seq=i, round_ids=[prev_r["id"], fr["id"]],
                  metric="liquid_level_m", value=lvl(fr), limit=lvl(prev_r))
        if fr is not None and hr is not None and abs(lvl(hr) - lvl(fr)) > tol:
            issue("LOAD_DIRECTION", "fatal",
                  "第 %d 级保压轮次 %d 液位 %.2f m 与充水液位 %.2f m 不一致，"
                  "荷载方向不符" % (i, hr["seq"], lvl(hr), lvl(fr)),
                  stage_seq=i, round_ids=[fr["id"], hr["id"]],
                  metric="liquid_level_m", value=lvl(hr), limit=lvl(fr))
        if fr is not None:
            tgt = eff[i - 1]["target_level_m"]
            if abs(lvl(fr) - tgt) > params["target_tolerance_m"]:
                issue("TARGET_DEVIATION", "warning",
                      "第 %d 级充水液位 %.2f m 与目标 %.2f m 偏差超过 %.2f m"
                      % (i, lvl(fr), tgt, params["target_tolerance_m"]),
                      stage_seq=i, round_ids=[fr["id"]],
                      metric="liquid_level_m", value=lvl(fr), limit=tgt)
    un_r = slot_rounds.get(("unload", 0))
    re_r = slot_rounds.get(("recheck", 0))
    last_hold = slot_rounds.get(("hold", n_stages))
    if un_r is not None and last_hold is not None and \
            lvl(un_r) >= lvl(last_hold) - tol:
        issue("LOAD_DIRECTION", "fatal",
              "卸载轮次 %d 液位 %.2f m 未低于末级保压液位 %.2f m，荷载方向不符"
              % (un_r["seq"], lvl(un_r), lvl(last_hold)),
              round_ids=[last_hold["id"], un_r["id"]],
              metric="liquid_level_m", value=lvl(un_r), limit=lvl(last_hold))
    if re_r is not None and un_r is not None and lvl(re_r) > lvl(un_r) + tol:
        issue("LOAD_DIRECTION", "fatal",
              "复测轮次 %d 液位 %.2f m 高于卸载轮液位 %.2f m，荷载方向不符"
              % (re_r["seq"], lvl(re_r), lvl(un_r)),
              round_ids=[un_r["id"], re_r["id"]],
              metric="liquid_level_m", value=lvl(re_r), limit=lvl(un_r))

    # -- 观测间隔（保压时长） -------------------------------------------------
    hold_hours = {}
    for i in range(1, n_stages + 1):
        fr = slot_rounds.get(("fill", i))
        hr = slot_rounds.get(("hold", i))
        if fr is None or hr is None:
            continue
        dt = hours_between(fr["measured_at"], hr["measured_at"])
        hold_hours[i] = dt
        need = eff[i - 1]["min_hold_hours"]
        if dt < need:
            issue("HOLD_TOO_SHORT", "fatal",
                  "第 %d 级保压时长 %.1f h 不足最短 %.1f h，观测间隔不足"
                  % (i, dt, need),
                  stage_seq=i, round_ids=[fr["id"], hr["id"]],
                  metric="hold_hours", value=dt, limit=need,
                  resurvey={"type": "延长保压后补测", "stage_seq": i})

    # -- 逐轮归算与质检（复用两轮分析的同一套机制） ---------------------------
    markers = allrows(conn, "SELECT * FROM marker WHERE tank_id=? ORDER BY azimuth_deg",
                      (tank["id"],))
    ring = [m for m in markers if not m["is_center"]]
    by_id = {m["id"]: m for m in markers}
    alpha = float(tank["wall_alpha"])
    href = float(tank["ref_height_m"] or 0)

    dup_marker_ids = set()
    seen_az = {}
    for m in ring:
        seen_az.setdefault(float(m["azimuth_deg"]) % 360.0, []).append(m["id"])
    for az, ids in seen_az.items():
        if len(ids) > 1:
            dup_marker_ids.update(ids)
            issue("DUPLICATE_AZIMUTH", "warning",
                  "方位 %.1f° 存在重号标志 %s，相关标志不参与分级分析"
                  % (az, "、".join(by_id[i]["code"] for i in ids)),
                  marker_ids=ids, metric="azimuth_deg", value=az,
                  resurvey={"type": "方位核查/重新编号", "azimuth_deg": az,
                            "markers": [by_id[i]["code"] for i in ids]})

    round_qc = {}
    for role, sseq, rid in slots:
        if (role, sseq) not in slot_rounds or rid in round_qc:
            continue
        rnd = slot_rounds[(role, sseq)]
        fl = []
        rdict = {r["marker_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM reading WHERE round_id=?", (rid,))}
        if rnd["origin_benchmark_id"] is None:
            issue("UNTIED", "fatal", "轮次 %d 未记录水准原点，无法归算" % rnd["seq"],
                  round_ids=[rid],
                  resurvey={"type": "原点联测补测", "round_seq": rnd["seq"]})
            fl.append("UNTIED")
            ctx = {"reading_m": None, "delta_m": 0.0,
                   "source": "no_origin", "has_cal": False}
        else:
            origin_row = one(conn, "SELECT * FROM benchmark WHERE id=?",
                             (rnd["origin_benchmark_id"],))
            ties_r = {t["benchmark_id"]: dict(t) for t in conn.execute(
                "SELECT * FROM round_tie WHERE round_id=?", (rid,))}
            ctx = qc_resolve_origin(conn, rnd, origin_row, ties_r, issue, fl)
        qc_check_loop(rnd, qc_params, issue, fl)
        qc_order_inversions(rnd, rdict, ring, set(), by_id, issue)
        for m in ring:
            if m["id"] not in rdict:
                issue("MISSING", "warning",
                      "标志 %s（%.1f°）在轮次 %d 漏测，该标志对应槽位数据缺失"
                      % (m["code"], m["azimuth_deg"], rnd["seq"]),
                      round_ids=[rid], marker_ids=[m["id"]],
                      resurvey={"type": "漏测补测", "round_seq": rnd["seq"],
                                "marker": m["code"],
                                "azimuth_deg": m["azimuth_deg"]})
        corr, terms = {}, {}
        if ctx["reading_m"] is not None:
            for m in ring:
                row = rdict.get(m["id"])
                if row:
                    corr[m["id"]], terms[m["id"]] = reduce_reading(
                        rnd, row["elevation_m"], m, ctx, alpha, href)
        round_qc[rid] = {"fatal": fl, "ctx": ctx, "corr": corr, "terms": terms,
                         "readings": rdict, "round": rnd}
        if fl:
            issue("ROUND_FATAL", "fatal",
                  "轮次 %d 含致命质检问题（%s），分级分析保持待判"
                  % (rnd["seq"], "/".join(fl)),
                  round_ids=[rid],
                  resurvey={"type": "致命问题轮次整改后重测",
                            "round_seq": rnd["seq"]})

    # -- 逐标志沉降序列（以空罐轮为基准，正为下沉） ---------------------------
    empty_rid = bindings.get("empty")
    base_qc = round_qc.get(empty_rid)
    base_corr = base_qc["corr"] if base_qc else {}
    series = {}
    for m in ring:
        mid = m["id"]
        if mid in dup_marker_ids:
            continue
        if mid not in base_corr:
            if base_qc is not None and base_qc["ctx"]["reading_m"] is not None:
                issue("NO_BASELINE", "info",
                      "标志 %s 空罐轮无有效读数，不参与分级分析" % m["code"],
                      marker_ids=[mid])
            continue
        s_map = {}
        for rid, qc in round_qc.items():
            if mid in qc["corr"]:
                s_map[rid] = base_corr[mid] - qc["corr"][mid]
        series[mid] = s_map

    # -- 荷载换算：q = ρ·g·h --------------------------------------------------
    g = params["gravity_m_s2"]
    rho_default = float(test["density_kg_m3"] or 1000)

    def load_kpa(rnd):
        rho = float(rnd["liquid_density"] or rho_default)
        return rho * g * float(rnd["liquid_level_m"] or 0) / 1000.0

    def s_of(mid, rid):
        return series.get(mid, {}).get(rid)

    def reading_id(rid, mid):
        qc = round_qc.get(rid)
        row = qc["readings"].get(mid) if qc else None
        return row["id"] if row else None

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    # -- 逐级：充水/保压增量、保压速率 ----------------------------------------
    stage_out = []
    for i in range(1, n_stages + 1):
        e = eff[i - 1]
        fr = slot_rounds.get(("fill", i))
        hr = slot_rounds.get(("hold", i))
        prev_r = slot_rounds.get(("hold", i - 1)) if i > 1 else empty_r
        dt_h = hold_hours.get(i)
        mk_rows = []
        rates = []
        for m in ring:
            mid = m["id"]
            if mid not in series:
                continue
            s_prev = s_of(mid, prev_r["id"]) if prev_r else None
            s_f = s_of(mid, fr["id"]) if fr else None
            s_h = s_of(mid, hr["id"]) if hr else None
            d_load = s_f - s_prev if (s_f is not None and s_prev is not None) else None
            d_hold = s_h - s_f if (s_h is not None and s_f is not None) else None
            rate = d_hold / dt_h if (d_hold is not None and dt_h and dt_h > 0) else None
            mk_rows.append({
                "marker_id": mid, "code": m["code"],
                "azimuth_deg": m["azimuth_deg"],
                "prev_settlement_m": s_prev,
                "fill_settlement_m": s_f,
                "hold_settlement_m": s_h,
                "load_increment_m": d_load,
                "hold_increment_m": d_hold,
                "hold_rate_m_per_h": rate,
                "cumulative_m": s_h,
                "fill_reading_id": reading_id(fr["id"], mid) if fr else None,
                "hold_reading_id": reading_id(hr["id"], mid) if hr else None,
            })
            if rate is not None:
                rates.append((rate, mid))
        over = [(r, mid) for r, mid in rates if r > e["rate_limit_m_per_h"]]
        if over:
            worst = max(over)[0]
            rids = []
            for _, mid in over:
                for rid in ((fr["id"] if fr else None), (hr["id"] if hr else None)):
                    x = reading_id(rid, mid) if rid else None
                    if x:
                        rids.append(x)
            issue("SETTLEMENT_RATE", "warning",
                  "第 %d 级保压沉降速率最大 %.3f mm/h 超过允许 %.3f mm/h"
                  "（%d 个标志越限）"
                  % (i, worst * 1000, e["rate_limit_m_per_h"] * 1000, len(over)),
                  stage_seq=i, marker_ids=[mid for _, mid in over],
                  reading_ids=rids, metric="hold_rate_m_per_h",
                  value=worst, limit=e["rate_limit_m_per_h"],
                  resurvey={"type": "延长保压并加密观测", "stage_seq": i})
        stage_out.append({
            "seq": i,
            "target_level_m": e["target_level_m"],
            "min_hold_hours": e["min_hold_hours"],
            "rate_limit_m_per_h": e["rate_limit_m_per_h"],
            "residual_limit_m": e["residual_limit_m"],
            "plan": {f: float(stages[i - 1][f]) for f in HYDRO_STAGE_FIELDS},
            "fill_round_id": fr["id"] if fr else None,
            "hold_round_id": hr["id"] if hr else None,
            "fill_round_seq": fr["seq"] if fr else None,
            "hold_round_seq": hr["seq"] if hr else None,
            "hold_hours": dt_h,
            "fill_load_kpa": load_kpa(fr) if fr else None,
            "hold_load_kpa": load_kpa(hr) if hr else None,
            "target_load_kpa": rho_default * g * e["target_level_m"] / 1000.0,
            "mean": {
                "load_increment_m": _mean([r["load_increment_m"] for r in mk_rows]),
                "hold_increment_m": _mean([r["hold_increment_m"] for r in mk_rows]),
                "hold_rate_m_per_h": _mean([r["hold_rate_m_per_h"] for r in mk_rows]),
                "cumulative_m": _mean([r["cumulative_m"] for r in mk_rows]),
            },
            "markers": mk_rows,
        })

    # -- 卸载回弹、残余沉降、加载—卸载滞回 ------------------------------------
    last_hold_rid = last_hold["id"] if last_hold else None
    un_rid = un_r["id"] if un_r else None
    re_rid = re_r["id"] if re_r else None
    ordered_slots = [(role, sseq, slot_rounds[(role, sseq)])
                     for role, sseq, _ in slots if (role, sseq) in slot_rounds]

    def hysteresis_area(pts):
        """(q kPa, s mm) 折线闭合面积（鞋带公式），即加卸载滞回环面积。"""
        if len(pts) < 3:
            return None
        a = 0.0
        for j in range(len(pts)):
            x1, y1 = pts[j]
            x2, y2 = pts[(j + 1) % len(pts)]
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0

    markers_out = []
    residuals = []
    for m in ring:
        mid = m["id"]
        if mid not in series:
            continue
        s_max = s_of(mid, last_hold_rid) if last_hold_rid else None
        s_un = s_of(mid, un_rid) if un_rid else None
        s_re = s_of(mid, re_rid) if re_rid else None
        rebound_m = s_max - s_un if (s_max is not None and s_un is not None) else None
        ratio = rebound_m / s_max \
            if (rebound_m is not None and s_max and s_max > 0) else None
        pts = []
        serie_pts = []
        for role, sseq, rnd in ordered_slots:
            sv = s_of(mid, rnd["id"])
            serie_pts.append({"role": role, "stage_seq": sseq or None,
                              "round_id": rnd["id"], "round_seq": rnd["seq"],
                              "load_kpa": load_kpa(rnd), "settlement_m": sv})
            if sv is not None:
                pts.append((load_kpa(rnd), sv * 1000.0))
        markers_out.append({
            "marker_id": mid, "code": m["code"], "azimuth_deg": m["azimuth_deg"],
            "series": serie_pts,
            "max_settlement_m": s_max,
            "unload_settlement_m": s_un,
            "rebound_m": rebound_m,
            "rebound_ratio": ratio,
            "residual_m": s_re,
            "hysteresis_area_kpa_mm": hysteresis_area(pts),
            "unload_reading_id": reading_id(un_rid, mid) if un_rid else None,
            "recheck_reading_id": reading_id(re_rid, mid) if re_rid else None,
        })
        if s_re is not None:
            residuals.append((s_re, mid))

    # 残余沉降：以末级（最高荷载级）残余限值判定
    lim_res = eff[-1]["residual_limit_m"] if eff else None
    if lim_res is not None:
        over = [(v, mid) for v, mid in residuals if v > lim_res]
        if over:
            worst = max(over)[0]
            issue("RESIDUAL_SETTLEMENT", "warning",
                  "复测残余沉降最大 %.1f mm 超过第 %d 级允许 %.1f mm"
                  "（%d 个标志越限）"
                  % (worst * 1000, n_stages, lim_res * 1000, len(over)),
                  stage_seq=n_stages, marker_ids=[mid for _, mid in over],
                  reading_ids=[reading_id(re_rid, mid) for _, mid in over
                               if reading_id(re_rid, mid)],
                  metric="residual_m", value=worst, limit=lim_res,
                  resurvey={"type": "残余沉降复测", "stage_seq": n_stages})

    # -- 均值曲线与汇总 --------------------------------------------------------
    mean_series = []
    for role, sseq, rnd in ordered_slots:
        vals = [s_of(mid, rnd["id"]) for mid in series]
        vals = [v for v in vals if v is not None]
        mean_series.append({
            "role": role, "stage_seq": sseq or None,
            "round_id": rnd["id"], "round_seq": rnd["seq"],
            "measured_at": rnd["measured_at"],
            "liquid_level_m": rnd["liquid_level_m"],
            "load_kpa": load_kpa(rnd),
            "settlement_m": (sum(vals) / len(vals)) if vals else None,
        })
    rebound_vals = [mo["rebound_ratio"] for mo in markers_out
                    if mo["rebound_ratio"] is not None]
    hyst_vals = [mo["hysteresis_area_kpa_mm"] for mo in markers_out
                 if mo["hysteresis_area_kpa_mm"] is not None]

    rounds_brief = {}
    for rid, qc in round_qc.items():
        rnd, ctx = qc["round"], qc["ctx"]
        rounds_brief[str(rid)] = {
            "id": rid, "seq": rnd["seq"], "measured_at": rnd["measured_at"],
            "origin_benchmark_id": rnd["origin_benchmark_id"],
            "origin_reading_m": ctx["reading_m"],
            "origin_source": ctx["source"],
            "origin_delta_m": ctx["delta_m"],
            "liquid_level_m": rnd["liquid_level_m"],
            "liquid_density": rnd["liquid_density"],
            "load_kpa": load_kpa(rnd),
            "loop_misclosure_m": rnd["loop_misclosure_m"],
            "fatal": qc["fatal"],
        }

    resurvey = []
    for iss in issues:
        rs = iss.get("resurvey")
        if rs:
            resurvey.append({"issue_id": iss["id"], "code": iss["code"], **rs})

    fatal = [i for i in issues if i["severity"] == "fatal"]
    result = {
        "revision_id": revision["id"],
        "revision_seq": revision["seq"],
        "test": {"id": test["id"], "code": test["code"],
                 "density_kg_m3": rho_default},
        "tank": {"id": tank["id"], "name": tank["name"],
                 "radius_m": tank["radius_m"]},
        "reason": revision["reason"],
        "params": params,
        "thresholds": thresholds,
        "rounds_used": [{"role": role, "stage_seq": sseq or None,
                         "round_id": rnd["id"], "round_seq": rnd["seq"]}
                        for role, sseq, rnd in ordered_slots],
        "rounds": rounds_brief,
        "stages": stage_out,
        "unload": {"round_id": un_rid,
                   "round_seq": un_r["seq"] if un_r else None,
                   "load_kpa": load_kpa(un_r) if un_r else None},
        "recheck": {"round_id": re_rid,
                    "round_seq": re_r["seq"] if re_r else None,
                    "load_kpa": load_kpa(re_r) if re_r else None,
                    "residual_limit_m": lim_res,
                    "residual_check_stage_seq": n_stages},
        "markers": markers_out,
        "mean_series": mean_series,
        "summary": {
            "max_settlement_m": max((mo["max_settlement_m"] for mo in markers_out
                                     if mo["max_settlement_m"] is not None),
                                    default=None),
            "mean_rebound_ratio": _mean(rebound_vals),
            "mean_residual_m": _mean([v for v, _ in residuals]),
            "mean_hysteresis_area_kpa_mm": _mean(hyst_vals),
        },
        "issues": issues,
        "resurvey": resurvey,
        "generated_at": now_iso(),
    }
    result["status"] = "pending" if fatal else "draft"
    return result


def build_hydro_svg(result):
    """荷载—沉降曲线 SVG。

    细灰线：逐标志；蓝线：加载段均值；橙线：卸载段均值；橙色半透明区：
    加载—卸载滞回环；竖虚线：各级目标荷载；红虚线：残余沉降。
    """
    W, H = 900, 640
    x0, y0, pw, ph = 100, 90, 540, 420
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" font-family="sans-serif">' % (W, H, W, H)]
    parts.append('<rect width="100%" height="100%" fill="white"/>')
    parts.append('<text x="20" y="36" font-size="20" font-weight="bold">'
                 '%s 水压试验 %s 荷载—沉降曲线（修订 R%d）</text>'
                 % (result["tank"]["name"], result["test"]["code"],
                    result["revision_seq"]))
    if result.get("status") == "pending":
        parts.append('<text x="%d" y="36" font-size="13" fill="#b00" '
                     'text-anchor="end">存在致命问题，保持待判</text>' % (W - 20))

    series = [p for p in result.get("mean_series", [])
              if p.get("settlement_m") is not None]
    if not series:
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#b00">'
                     '无有效沉降序列（致命问题或数据缺失）</text>'
                     % (x0 + pw // 2, y0 + ph // 2))
        parts.append("</svg>")
        return "\n".join(parts)

    qmax = max(p["load_kpa"] for p in series) * 1.08 or 1.0
    smax = max(max(p["settlement_m"] for p in series) * 1000.0 * 1.15, 1e-3)

    def X(q):
        return x0 + pw * q / qmax

    def Y(s_mm):
        return y0 + ph * s_mm / smax

    for k in range(6):
        q = qmax * k / 5
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#eee"/>'
                     % (X(q), y0, X(q), y0 + ph))
        parts.append('<text x="%.1f" y="%d" font-size="10" fill="#888" '
                     'text-anchor="middle">%.0f</text>' % (X(q), y0 + ph + 16, q))
    for k in range(6):
        s = smax * k / 5
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eee"/>'
                     % (x0, Y(s), x0 + pw, Y(s)))
        parts.append('<text x="%d" y="%.1f" font-size="10" fill="#888" '
                     'text-anchor="end">%.1f</text>' % (x0 - 6, Y(s) + 3, s))
    parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" '
                 'stroke="#999"/>' % (x0, y0, pw, ph))
    parts.append('<text x="%.1f" y="%d" font-size="12" text-anchor="middle">'
                 '荷载 q (kPa)</text>' % (x0 + pw / 2.0, y0 + ph + 40))
    parts.append('<text x="34" y="%.1f" font-size="12" text-anchor="middle" '
                 'transform="rotate(-90 34 %.1f)">沉降 s (mm)</text>'
                 % (y0 + ph / 2.0, y0 + ph / 2.0))

    for st in result.get("stages", []):
        qt = st.get("target_load_kpa")
        if qt is None:
            continue
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#bbb" '
                     'stroke-dasharray="5 4"/>' % (X(qt), y0, X(qt), y0 + ph))
        parts.append('<text x="%.1f" y="%d" font-size="10" fill="#999" '
                     'text-anchor="middle">%d级 %.1fm</text>'
                     % (X(qt), y0 - 6, st["seq"], st["target_level_m"]))

    for mo in result.get("markers", []):
        pts = [(p["load_kpa"], p["settlement_m"] * 1000.0)
               for p in mo.get("series", []) if p.get("settlement_m") is not None]
        if len(pts) >= 2:
            parts.append('<polyline points="%s" fill="none" stroke="#ddd" '
                         'stroke-width="1"/>'
                         % " ".join("%.1f,%.1f" % (X(q), Y(s)) for q, s in pts))

    un_i = next((i for i, p in enumerate(series) if p["role"] == "unload"),
                len(series))
    loading = series[:un_i] if un_i < len(series) else series
    unloading = ([series[un_i - 1]] + series[un_i:]) \
        if 0 < un_i < len(series) else []

    def path(pts):
        return " ".join("%.1f,%.1f" % (X(p["load_kpa"]),
                                       Y(p["settlement_m"] * 1000.0))
                        for p in pts)

    if unloading:
        loop = list(loading) + list(unloading[1:])
        parts.append('<polygon points="%s" fill="#e88000" fill-opacity="0.12" '
                     'stroke="none"/>' % path(loop))
    if len(loading) >= 2:
        parts.append('<polyline points="%s" fill="none" stroke="#1f5fbf" '
                     'stroke-width="2.5"/>' % path(loading))
    if len(unloading) >= 2:
        parts.append('<polyline points="%s" fill="none" stroke="#e80" '
                     'stroke-width="2.5"/>' % path(unloading))
    for p in series:
        col = "#1f5fbf" if p["role"] in ("empty", "fill", "hold") else "#e80"
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s"/>'
                     % (X(p["load_kpa"]), Y(p["settlement_m"] * 1000.0), col))
        if p["role"] in ("hold", "unload", "recheck"):
            parts.append('<text x="%.1f" y="%.1f" font-size="9" fill="#666">%s</text>'
                         % (X(p["load_kpa"]) + 5,
                            Y(p["settlement_m"] * 1000.0) - 5,
                            _role_label(p["role"], p["stage_seq"] or 0)))

    re_pt = next((p for p in reversed(series) if p["role"] == "recheck"), None)
    if re_pt:
        sx = X(re_pt["load_kpa"])
        sy = Y(re_pt["settlement_m"] * 1000.0)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#c00" '
                     'stroke-dasharray="3 3"/>' % (sx, Y(0), sx, sy))
        parts.append('<text x="%.1f" y="%.1f" font-size="10" fill="#c00">'
                     '残余 %.1f mm</text>'
                     % (sx + 6, (Y(0) + sy) / 2, re_pt["settlement_m"] * 1000.0))

    lx0, ly0 = 690, 120
    legend = [
        ("#1f5fbf", "加载段（均值）"),
        ("#e80", "卸载段（均值）"),
        ("#ddd", "逐标志曲线"),
        ("#bbb", "分级目标荷载"),
    ]
    parts.append('<text x="%d" y="%d" font-size="14" font-weight="bold">图例</text>'
                 % (lx0, ly0 - 24))
    for i, (c, lab) in enumerate(legend):
        y = ly0 + i * 24
        parts.append('<rect x="%d" y="%d" width="16" height="6" fill="%s"/>'
                     % (lx0, y - 5, c))
        parts.append('<text x="%d" y="%d" font-size="12">%s</text>'
                     % (lx0 + 24, y, lab))
    y = ly0 + len(legend) * 24 + 6
    parts.append('<rect x="%d" y="%d" width="16" height="10" fill="#e88000" '
                 'fill-opacity="0.25"/>' % (lx0, y - 9))
    parts.append('<text x="%d" y="%d" font-size="12">加载—卸载滞回环</text>'
                 % (lx0 + 24, y))

    summ = result.get("summary", {})
    y += 30
    lines = []
    if summ.get("max_settlement_m") is not None:
        lines.append("最大沉降: %.1f mm" % (summ["max_settlement_m"] * 1000))
    if summ.get("mean_rebound_ratio") is not None:
        lines.append("平均卸载回弹率: %.1f%%" % (summ["mean_rebound_ratio"] * 100))
    if summ.get("mean_residual_m") is not None:
        lines.append("平均残余沉降: %.1f mm" % (summ["mean_residual_m"] * 1000))
    if summ.get("mean_hysteresis_area_kpa_mm") is not None:
        lines.append("平均滞回环面积: %.1f kPa·mm"
                     % summ["mean_hysteresis_area_kpa_mm"])
    for s in lines:
        parts.append('<text x="%d" y="%d" font-size="12">%s</text>' % (lx0, y, s))
        y += 22

    n_fatal = len([i for i in result.get("issues", [])
                   if i["severity"] == "fatal"])
    parts.append('<text x="20" y="%d" font-size="12" fill="#a00">'
                 '问题 %d 项（致命 %d，详见逐级 JSON）</text>'
                 % (H - 20, len(result.get("issues", [])), n_fatal))
    parts.append("</svg>")
    return "\n".join(parts)


def normalize_hydro_bindings(conn, test, stages, raw):
    """校验并归一化槽位绑定：轮次引用 seq 或 {"id"} → 轮次 id。"""
    if not isinstance(raw, dict):
        raise ApiError(400, "BAD_BINDINGS", "bindings 应为对象")
    n = len(stages)

    def resolve(x, label):
        if isinstance(x, dict) and x.get("id") is not None:
            rid = int(x["id"])
        elif isinstance(x, int):
            r = one(conn, "SELECT id FROM round WHERE tank_id=? AND seq=?",
                    (test["tank_id"], x))
            if not r:
                raise ApiError(400, "BAD_ROUND", "%s 轮次 %s 不存在" % (label, x))
            rid = r["id"]
        else:
            raise ApiError(400, "BAD_ROUND",
                           "%s 轮次引用应为 seq 或 {\"id\"}" % label)
        if not one(conn, "SELECT 1 FROM round WHERE id=? AND tank_id=?",
                   (rid, test["tank_id"])):
            raise ApiError(400, "BAD_ROUND", "%s 轮次 %d 不属于该罐" % (label, rid))
        return rid

    out = {}
    for key, label in (("empty", "空罐"), ("unload", "卸载"), ("recheck", "复测")):
        if raw.get(key) is None:
            raise ApiError(400, "MISSING_FIELD", "绑定缺少 %s 轮次" % label)
        out[key] = resolve(raw[key], label)
    rstages = raw.get("stages")
    if not isinstance(rstages, list) or len(rstages) != n:
        raise ApiError(400, "BAD_BINDINGS",
                       "绑定级数 %s 与方案级数 %d 不符"
                       % (len(rstages) if isinstance(rstages, list) else rstages, n))
    out["stages"] = []
    for i, st in enumerate(rstages, 1):
        if not isinstance(st, dict) or st.get("fill") is None \
                or st.get("hold") is None:
            raise ApiError(400, "MISSING_FIELD", "第 %d 级绑定缺少 fill/hold 轮次" % i)
        out["stages"].append({"fill": resolve(st["fill"], "第%d级充水" % i),
                              "hold": resolve(st["hold"], "第%d级保压" % i)})
    return out


def validate_hydro_thresholds(raw, n_stages):
    """改取阈值结构校验：stages.<级> 仅允许阈值字段，params 仅允许已知参数。"""
    if not isinstance(raw, dict):
        raise ApiError(400, "BAD_THRESHOLDS", "thresholds 应为对象")
    for key in raw:
        if key not in ("stages", "params"):
            raise ApiError(400, "BAD_THRESHOLDS", "未知阈值分组: %s" % key)
    for seq_k, ov in (raw.get("stages") or {}).items():
        if not str(seq_k).isdigit() or not 1 <= int(seq_k) <= n_stages:
            raise ApiError(400, "BAD_THRESHOLDS", "阈值指向不存在的级: %s" % seq_k)
        if not isinstance(ov, dict):
            raise ApiError(400, "BAD_THRESHOLDS", "第 %s 级阈值应为对象" % seq_k)
        for f in ov:
            if f not in HYDRO_THRESHOLD_FIELDS:
                raise ApiError(400, "BAD_THRESHOLDS", "未知阈值字段: %s" % f)
    for f in (raw.get("params") or {}):
        if f not in HYDRO_PARAMS:
            raise ApiError(400, "BAD_THRESHOLDS", "未知参数: %s" % f)
    return raw


def create_hydro_revision(conn, test, stages, bindings, reason, thresholds=None):
    """新建试验修订并立即运行分级引擎（改绑/改阈值必须给出 reason）。"""
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM hydro_revision "
                       "WHERE test_id=?", (test["id"],)).fetchone()[0]
    rid = conn.execute(
        "INSERT INTO hydro_revision(test_id,seq,bindings,thresholds,reason,status,"
        "created_at) VALUES(?,?,?,?,?,'pending',?)",
        (test["id"], seq, json.dumps(bindings, ensure_ascii=False),
         json.dumps(thresholds or {}, ensure_ascii=False), reason, now_iso())).lastrowid
    conn.commit()
    rev = one(conn, "SELECT * FROM hydro_revision WHERE id=?", (rid,))
    result = analyze_hydro_revision(conn, test, stages, rev)
    conn.execute("UPDATE hydro_revision SET status=?, result=? WHERE id=?",
                 (result["status"], json.dumps(result, ensure_ascii=False), rid))
    conn.commit()
    return one(conn, "SELECT * FROM hydro_revision WHERE id=?", (rid,)), result


def finalize_hydro_revision(conn, rev):
    """定稿：仅 draft 可定稿；pending（待判）须改绑阶段或改取阈值后新建修订。"""
    if rev["status"] == "pending":
        raise ApiError(409, "HYDRO_PENDING",
                       "修订含致命问题，保持待判，不能定稿；"
                       "请改绑阶段或调整阈值后新建修订")
    if rev["status"] == "final":
        raise ApiError(409, "HYDRO_FINAL", "修订已定稿")
    with write_lock:
        conn.execute("UPDATE hydro_revision SET status='final',finalized_at=? "
                     "WHERE id=?", (now_iso(), rev["id"]))
        conn.commit()
    return one(conn, "SELECT * FROM hydro_revision WHERE id=?", (rev["id"],))


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TankSettlement/1.0"
    db_path = "tank_settlement.db"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8",
              headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        try:
            with connect(self.db_path) as conn:
                self._route(conn, method, parts, q)
        except ApiError as e:
            self._send(e.status, {"error": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": "INTERNAL", "message": str(e)})

    def _route(self, conn, method, parts, q):
        if not parts:
            self._send(200, {"service": "tank-settlement", "status": "ok",
                             "endpoints": [
                                 "POST /tanks /markers /benchmarks /calibrations",
                                 "POST /rounds (含 readings/ties)",
                                 "GET  /rounds /readings /ties /versions",
                                 "POST /analyze",
                                 "POST /versions/<id>/lock",
                                 "GET  /versions/<id>/resurvey.csv",
                                 "GET  /versions/<id>/settlement.svg",
                                 "GET  /versions/<id>/recompute.json",
                                 "POST /hydro-tests (含 stages 方案)",
                                 "GET  /hydro-tests[/<id>]",
                                 "POST /hydro-tests/<id>/revisions (改绑/改阈值)",
                                 "GET  /hydro-tests/<id>/revisions[/<seq>]",
                                 "POST /hydro-tests/<id>/revisions/<seq>/finalize",
                                 "GET  /hydro-tests/<id>/revisions/<seq>/stages.json",
                                 "GET  /hydro-tests/<id>/revisions/<seq>/load-settlement.svg"]})
            return

        root = parts[0]
        routes = {
            "tanks": self._tanks, "markers": self._markers,
            "benchmarks": self._benchmarks, "calibrations": self._calibrations,
            "rounds": self._rounds, "readings": self._readings,
            "ties": self._ties, "versions": self._versions,
            "analyze": self._analyze, "hydro-tests": self._hydro_tests,
        }
        if root not in routes:
            raise ApiError(404, "NOT_FOUND", "未知路径: %s" % root)
        routes[root](conn, parts[1:], q)

    # -- 罐体 ----------------------------------------------------------------
    def _tanks(self, conn, parts, q):
        if self._method() == "POST" and not parts:
            data = self._json_body()
            if isinstance(data, list):
                ids = [self._create_tank(conn, d) for d in data]
                self._send(201, {"ids": ids})
            else:
                tid = self._create_tank(conn, data)
                self._send(201, {"id": tid})
        elif self._method() == "GET":
            if parts:
                r = one(conn, "SELECT * FROM tank WHERE id=?", (int(parts[0]),))
                if not r:
                    raise ApiError(404, "NOT_FOUND", "罐体不存在")
                self._send(200, dict(r))
            else:
                self._send(200, allrows(conn, "SELECT * FROM tank ORDER BY id"))
        elif self._method() == "POST" and len(parts) == 2 and parts[1] == "center":
            data = self._json_body()
            require(data, ["marker_id"])
            m = one(conn, "SELECT * FROM marker WHERE id=? AND tank_id=?",
                    (data["marker_id"], int(parts[0])))
            if not m:
                raise ApiError(404, "NOT_FOUND", "中心标志不存在或不属于该罐")
            with write_lock:
                conn.execute("UPDATE tank SET center_marker_id=? WHERE id=?",
                             (m["id"], int(parts[0])))
                conn.commit()
            self._send(200, {"ok": True})
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    def _create_tank(self, conn, d):
        require(d, ["name"])
        with write_lock:
            cur = conn.execute(
                "INSERT INTO tank(name,radius_m,height_m,wall_alpha,ref_height_m,"
                "bottom_slope_spec,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (d["name"], d.get("radius_m"), d.get("height_m"),
                 d.get("wall_alpha", 1.2e-5), d.get("ref_height_m", 0),
                 d.get("bottom_slope_spec", 0.0083), d.get("note"), now_iso()))
            conn.commit()
            return cur.lastrowid

    # -- 标志 ----------------------------------------------------------------
    def _markers(self, conn, parts, q):
        if self._method() == "POST" and not parts:
            data = self._json_body()
            items = data if isinstance(data, list) else [data]
            ids = []
            with write_lock:
                for d in items:
                    require(d, ["tank_id", "code", "azimuth_deg"])
                    if not one(conn, "SELECT 1 FROM tank WHERE id=?", (d["tank_id"],)):
                        raise ApiError(400, "BAD_TANK", "tank_id 不存在")
                    cur = conn.execute(
                        "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order,"
                        "is_center,note) VALUES(?,?,?,?,?,?)",
                        (d["tank_id"], d["code"], d["azimuth_deg"],
                         d.get("ring_order"), 1 if d.get("is_center") else 0,
                         d.get("note")))
                    ids.append(cur.lastrowid)
                conn.commit()
            self._send(201, {"ids": ids})
        elif self._method() == "GET":
            sql = "SELECT * FROM marker"
            args = ()
            if q.get("tank_id"):
                sql += " WHERE tank_id=?"
                args = (int(q["tank_id"][0]),)
            sql += " ORDER BY azimuth_deg"
            self._send(200, allrows(conn, sql, args))
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    # -- 水准原点 -------------------------------------------------------------
    def _benchmarks(self, conn, parts, q):
        if self._method() == "POST" and not parts:
            data = self._json_body()
            items = data if isinstance(data, list) else [data]
            ids = []
            with write_lock:
                for d in items:
                    require(d, ["code"])
                    try:
                        cur = conn.execute(
                            "INSERT INTO benchmark(code,assumed_stable,description,"
                            "created_at) VALUES(?,?,?,?)",
                            (d["code"], 1 if d.get("assumed_stable") else 0,
                             d.get("description"), now_iso()))
                        ids.append(cur.lastrowid)
                    except sqlite3.IntegrityError:
                        raise ApiError(409, "DUP_BENCHMARK",
                                       "基准点编号已存在: %s" % d["code"])
                conn.commit()
            self._send(201, {"ids": ids})
        elif self._method() == "GET":
            self._send(200, allrows(conn, "SELECT * FROM benchmark ORDER BY id"))
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    # -- 校准资料 -------------------------------------------------------------
    def _calibrations(self, conn, parts, q):
        if self._method() == "POST" and not parts:
            data = self._json_body()
            items = data if isinstance(data, list) else [data]
            ids = []
            with write_lock:
                for d in items:
                    require(d, ["benchmark_id", "date"])
                    if not one(conn, "SELECT 1 FROM benchmark WHERE id=?",
                               (d["benchmark_id"],)):
                        raise ApiError(400, "BAD_BENCHMARK", "benchmark_id 不存在")
                    cur = conn.execute(
                        "INSERT INTO calibration(benchmark_id,date,delta_m,"
                        "delta_std_m,stable,note) VALUES(?,?,?,?,?,?)",
                        (d["benchmark_id"], d["date"], d.get("delta_m", 0),
                         d.get("delta_std_m"), d.get("stable"), d.get("note")))
                    ids.append(cur.lastrowid)
                conn.commit()
            self._send(201, {"ids": ids})
        elif self._method() == "GET":
            sql = "SELECT * FROM calibration"
            args = ()
            if q.get("benchmark_id"):
                sql += " WHERE benchmark_id=?"
                args = (int(q["benchmark_id"][0]),)
            sql += " ORDER BY date"
            self._send(200, allrows(conn, sql, args))
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    # -- 观测轮次（可带 readings / ties） ------------------------------------
    def _rounds(self, conn, parts, q):
        if self._method() == "POST" and not parts:
            d = self._json_body()
            require(d, ["tank_id", "seq", "measured_at"])
            with write_lock:
                if not one(conn, "SELECT 1 FROM tank WHERE id=?", (d["tank_id"],)):
                    raise ApiError(400, "BAD_TANK", "tank_id 不存在")
                if d.get("origin_benchmark_id") and not one(
                        conn, "SELECT 1 FROM benchmark WHERE id=?",
                        (d["origin_benchmark_id"],)):
                    raise ApiError(400, "BAD_BENCHMARK", "origin_benchmark_id 不存在")
                try:
                    cur = conn.execute(
                        "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
                        "origin_reading_m,liquid_level_m,liquid_density,wall_temp_c,"
                        "load_coeff_m_per_m,loop_misclosure_m,loop_length_km,note) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d["tank_id"], d["seq"], d["measured_at"],
                         d.get("origin_benchmark_id"), d.get("origin_reading_m"),
                         d.get("liquid_level_m", 0), d.get("liquid_density"),
                         d.get("wall_temp_c"), d.get("load_coeff_m_per_m", 0),
                         d.get("loop_misclosure_m"), d.get("loop_length_km", 1),
                         d.get("note")))
                except sqlite3.IntegrityError:
                    raise ApiError(409, "DUP_ROUND", "该罐轮次号已存在")
                rid = cur.lastrowid
                for r in d.get("readings", []):
                    require(r, ["marker_id", "elevation_m"])
                    conn.execute(
                        "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx,note)"
                        " VALUES(?,?,?,?,?)",
                        (rid, r["marker_id"], r["elevation_m"],
                         r.get("order_idx"), r.get("note")))
                for t in d.get("ties", []):
                    require(t, ["benchmark_id"])
                    conn.execute(
                        "INSERT INTO round_tie(round_id,benchmark_id,tie_elevation_m,"
                        "delta_stable_m) VALUES(?,?,?,?)",
                        (rid, t["benchmark_id"], t.get("tie_elevation_m"),
                         t.get("delta_stable_m")))
                conn.commit()
            self._send(201, {"id": rid})
        elif self._method() == "GET":
            if parts:
                r = one(conn, "SELECT * FROM round WHERE id=?", (int(parts[0]),))
                if not r:
                    raise ApiError(404, "NOT_FOUND", "轮次不存在")
                out = dict(r)
                out["readings"] = allrows(
                    conn, "SELECT * FROM reading WHERE round_id=? ORDER BY order_idx",
                    (r["id"],))
                out["ties"] = allrows(
                    conn, "SELECT * FROM round_tie WHERE round_id=?", (r["id"],))
                self._send(200, out)
            else:
                sql = "SELECT * FROM round"
                args = ()
                if q.get("tank_id"):
                    sql += " WHERE tank_id=?"
                    args = (int(q["tank_id"][0]),)
                sql += " ORDER BY seq"
                self._send(200, allrows(conn, sql, args))
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    # -- 高程读数 -------------------------------------------------------------
    def _readings(self, conn, parts, q):
        if self._method() == "POST":
            data = self._json_body()
            rid_raw = q.get("round_id", [data.get("round_id")])[0]
            if rid_raw is None:
                raise ApiError(400, "MISSING_FIELD", "需要 round_id")
            rid = int(rid_raw)
            rnd = one(conn, "SELECT * FROM round WHERE id=?", (rid,))
            if not rnd:
                raise ApiError(404, "NOT_FOUND", "轮次不存在")
            items = data.get("readings") if "readings" in data else \
                (data if isinstance(data, list) else [data])
            ids = []
            with write_lock:
                for r in items:
                    require(r, ["marker_id", "elevation_m"])
                    try:
                        cur = conn.execute(
                            "INSERT INTO reading(round_id,marker_id,elevation_m,"
                            "order_idx,note) VALUES(?,?,?,?,?)",
                            (rid, r["marker_id"], r["elevation_m"],
                             r.get("order_idx"), r.get("note")))
                        ids.append(cur.lastrowid)
                    except sqlite3.IntegrityError:
                        raise ApiError(409, "DUP_READING",
                                       "标志 %s 在该轮已有读数" % r["marker_id"])
                conn.commit()
            self._send(201, {"ids": ids})
        else:
            if not q.get("round_id"):
                raise ApiError(400, "MISSING_FIELD", "需要 round_id 查询参数")
            self._send(200, allrows(
                conn,
                "SELECT * FROM reading WHERE round_id=? ORDER BY order_idx,id",
                (int(q["round_id"][0]),)))

    # -- 联系点 ---------------------------------------------------------------
    def _ties(self, conn, parts, q):
        if self._method() == "POST":
            data = self._json_body()
            rid_raw = q.get("round_id", [data.get("round_id")])[0]
            if rid_raw is None:
                raise ApiError(400, "MISSING_FIELD", "需要 round_id")
            rid = int(rid_raw)
            items = data.get("ties") if "ties" in data else \
                (data if isinstance(data, list) else [data])
            ids = []
            with write_lock:
                for t in items:
                    require(t, ["benchmark_id"])
                    cur = conn.execute(
                        "INSERT INTO round_tie(round_id,benchmark_id,tie_elevation_m,"
                        "delta_stable_m) VALUES(?,?,?,?)",
                        (rid, t["benchmark_id"], t.get("tie_elevation_m"),
                         t.get("delta_stable_m")))
                    ids.append(cur.lastrowid)
                conn.commit()
            self._send(201, {"ids": ids})
        else:
            if not q.get("round_id"):
                raise ApiError(400, "MISSING_FIELD", "需要 round_id 查询参数")
            self._send(200, allrows(
                conn, "SELECT * FROM round_tie WHERE round_id=?",
                (int(q["round_id"][0]),)))

    # -- 分析版本 -------------------------------------------------------------
    def _analyze(self, conn, parts, q):
        if self._method() != "POST":
            raise ApiError(404, "NOT_FOUND", "不支持的方法")
        d = self._json_body()
        require(d, ["tank_id", "round_a", "round_b", "reason"])
        tank = one(conn, "SELECT * FROM tank WHERE id=?", (d["tank_id"],))
        if not tank:
            raise ApiError(400, "BAD_TANK", "tank_id 不存在")

        def resolve_round(x):
            if isinstance(x, dict) and x.get("id"):
                return int(x["id"])
            if isinstance(x, int):
                r = one(conn, "SELECT id FROM round WHERE tank_id=? AND seq=?",
                        (tank["id"], x))
                if not r:
                    raise ApiError(400, "BAD_ROUND", "轮次 %s 不存在" % x)
                return r["id"]
            raise ApiError(400, "BAD_ROUND", "轮次引用应为 seq 或 {id}")

        ra_id, rb_id = resolve_round(d["round_a"]), resolve_round(d["round_b"])
        if ra_id == rb_id:
            raise ApiError(400, "BAD_ROUND", "两轮必须不同")

        excluded = {}
        for item in d.get("excluded_readings", []):
            if isinstance(item, dict):
                rid, reason = item.get("reading_id"), item.get("reason")
            else:
                rid, reason = item, d.get("exclude_reason")
            if not reason:
                raise ApiError(400, "MISSING_REASON",
                               "剔除读数 #%s 必须附理由" % rid)
            rr = one(conn, "SELECT * FROM reading WHERE id=?", (rid,))
            if not rr or rr["round_id"] not in (ra_id, rb_id):
                raise ApiError(400, "BAD_READING", "被剔除读数 %s 不属于这两轮" % rid)
            excluded[str(rid)] = reason

        origin_id = d.get("origin_benchmark_id")
        if origin_id:
            if not one(conn, "SELECT 1 FROM benchmark WHERE id=?", (origin_id,)):
                raise ApiError(400, "BAD_BENCHMARK", "改选原点不存在")
        if origin_id and not d.get("origin_change_reason"):
            raise ApiError(400, "MISSING_REASON", "改选原点必须附 origin_change_reason")

        with write_lock:
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM version "
                               "WHERE tank_id=?", (tank["id"],)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO version(tank_id,seq,round_a_id,round_b_id,"
                "origin_benchmark_id,excluded_readings,params,reason,status,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tank["id"], seq, ra_id, rb_id, origin_id,
                 json.dumps(excluded, ensure_ascii=False),
                 json.dumps(d.get("params", {}), ensure_ascii=False),
                 d["reason"] +
                 ("；改选原点理由：" + d["origin_change_reason"]
                  if origin_id else ""),
                 "draft", now_iso()))
            vid = cur.lastrowid
            conn.commit()
            v = one(conn, "SELECT * FROM version WHERE id=?", (vid,))
            result = analyze_version(conn, v)
            conn.execute("UPDATE version SET status=?, result=? WHERE id=?",
                         (result["status"],
                          json.dumps(result, ensure_ascii=False), vid))
            conn.commit()
        self._send(201, {"version_id": vid, "seq": seq,
                         "status": result["status"], "result": result})

    def _versions(self, conn, parts, q):
        method = self._method()
        if method == "GET" and not parts:
            sql = "SELECT id,tank_id,seq,round_a_id,round_b_id,status,reason," \
                  "created_at,locked_at FROM version"
            args = ()
            if q.get("tank_id"):
                sql += " WHERE tank_id=?"
                args = (int(q["tank_id"][0]),)
            sql += " ORDER BY id"
            self._send(200, allrows(conn, sql, args))
            return
        vid = int(parts[0])
        v = one(conn, "SELECT * FROM version WHERE id=?", (vid,))
        if not v:
            raise ApiError(404, "NOT_FOUND", "版本不存在")

        if method == "GET" and len(parts) == 1:
            out = dict(v)
            out["result"] = json.loads(v["result"]) if v["result"] else None
            self._send(200, out)
        elif method == "POST" and len(parts) == 2 and parts[1] == "lock":
            if v["status"] == "rejected":
                raise ApiError(409, "VERSION_REJECTED",
                               "版本含致命问题，不能锁定；请补测后新建版本")
            if v["status"] == "locked":
                raise ApiError(409, "VERSION_LOCKED", "版本已锁定")
            with write_lock:
                conn.execute("UPDATE version SET status='locked',locked_at=? WHERE id=?",
                             (now_iso(), vid))
                conn.commit()
            self._send(200, {"id": vid, "status": "locked"})
        elif method == "GET" and len(parts) == 2:
            kind = parts[1]
            if v["status"] != "locked":
                raise ApiError(409, "VERSION_NOT_LOCKED",
                               "版本未锁定，不能导出成果（当前状态 %s）" % v["status"])
            result = json.loads(v["result"])
            if kind == "recompute.json":
                self._send(200, result,
                           "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="recompute_v%d.json"' % v["seq"]})
            elif kind == "resurvey.csv":
                self._send(200, build_resurvey_csv(result), "text/csv; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="resurvey_v%d.csv"' % v["seq"]})
            elif kind == "settlement.svg":
                self._send(200, build_svg(result), "image/svg+xml")
            else:
                raise ApiError(404, "NOT_FOUND", "未知成果类型")
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    # -- 分级加载（水压试验） --------------------------------------------------
    def _hydro_tests(self, conn, parts, q):
        method = self._method()
        if method == "POST" and not parts:
            d = self._json_body()
            require(d, ["tank_id", "code", "stages"])
            if not one(conn, "SELECT 1 FROM tank WHERE id=?", (d["tank_id"],)):
                raise ApiError(400, "BAD_TANK", "tank_id 不存在")
            stages = d["stages"]
            if not isinstance(stages, list) or not stages:
                raise ApiError(400, "BAD_STAGES", "stages 必须为非空数组")
            prev_tgt = None
            for i, st in enumerate(stages, 1):
                require(st, list(HYDRO_STAGE_FIELDS))
                tgt = float(st["target_level_m"])
                if prev_tgt is not None and tgt <= prev_tgt:
                    raise ApiError(400, "TARGET_ORDER",
                                   "第 %d 级目标液位 %.2f 未高于上一级 %.2f，阶段倒序"
                                   % (i, tgt, prev_tgt))
                prev_tgt = tgt
                for f in ("min_hold_hours", "rate_limit_m_per_h",
                          "residual_limit_m"):
                    if float(st[f]) < 0:
                        raise ApiError(400, "BAD_STAGES",
                                       "第 %d 级 %s 不能为负" % (i, f))
            with write_lock:
                try:
                    cur = conn.execute(
                        "INSERT INTO hydro_test(tank_id,code,density_kg_m3,note,"
                        "created_at) VALUES(?,?,?,?,?)",
                        (d["tank_id"], d["code"], d.get("density_kg_m3", 1000),
                         d.get("note"), now_iso()))
                except sqlite3.IntegrityError:
                    raise ApiError(409, "DUP_HYDRO_TEST",
                                   "试验编号已存在: %s" % d["code"])
                tid = cur.lastrowid
                for i, st in enumerate(stages, 1):
                    conn.execute(
                        "INSERT INTO hydro_stage(test_id,seq,target_level_m,"
                        "min_hold_hours,rate_limit_m_per_h,residual_limit_m)"
                        " VALUES(?,?,?,?,?,?)",
                        (tid, i, float(st["target_level_m"]),
                         float(st["min_hold_hours"]),
                         float(st["rate_limit_m_per_h"]),
                         float(st["residual_limit_m"])))
                conn.commit()
            self._send(201, {"id": tid})
            return
        if method == "GET" and not parts:
            sql = "SELECT * FROM hydro_test"
            args = ()
            if q.get("tank_id"):
                sql += " WHERE tank_id=?"
                args = (int(q["tank_id"][0]),)
            sql += " ORDER BY id"
            self._send(200, allrows(conn, sql, args))
            return
        tid = int(parts[0])
        test = one(conn, "SELECT * FROM hydro_test WHERE id=?", (tid,))
        if not test:
            raise ApiError(404, "NOT_FOUND", "水压试验不存在")
        if method == "GET" and len(parts) == 1:
            out = dict(test)
            out["stages"] = allrows(
                conn, "SELECT * FROM hydro_stage WHERE test_id=? ORDER BY seq",
                (tid,))
            out["revisions"] = allrows(
                conn, "SELECT id,seq,status,reason,created_at,finalized_at "
                      "FROM hydro_revision WHERE test_id=? ORDER BY seq", (tid,))
            self._send(200, out)
        elif len(parts) >= 2 and parts[1] == "revisions":
            self._hydro_revisions(conn, test, parts[2:])
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    def _hydro_revisions(self, conn, test, parts):
        method = self._method()
        stages = allrows(conn, "SELECT * FROM hydro_stage WHERE test_id=? "
                               "ORDER BY seq", (test["id"],))
        if method == "POST" and not parts:
            d = self._json_body()
            require(d, ["bindings", "reason"])
            bindings = normalize_hydro_bindings(conn, test, stages, d["bindings"])
            thresholds = validate_hydro_thresholds(d.get("thresholds") or {},
                                                   len(stages))
            with write_lock:
                rev, result = create_hydro_revision(
                    conn, test, stages, bindings, d["reason"], thresholds)
            self._send(201, {"revision_id": rev["id"], "seq": rev["seq"],
                             "status": result["status"], "result": result})
            return
        if method == "GET" and not parts:
            self._send(200, allrows(
                conn, "SELECT id,seq,status,reason,created_at,finalized_at "
                      "FROM hydro_revision WHERE test_id=? ORDER BY seq",
                (test["id"],)))
            return
        rseq = int(parts[0])
        rev = one(conn, "SELECT * FROM hydro_revision WHERE test_id=? AND seq=?",
                  (test["id"], rseq))
        if not rev:
            raise ApiError(404, "NOT_FOUND", "试验修订不存在")
        if method == "GET" and len(parts) == 1:
            out = dict(rev)
            out["bindings"] = json.loads(rev["bindings"])
            out["thresholds"] = json.loads(rev["thresholds"] or "{}")
            out["result"] = json.loads(rev["result"]) if rev["result"] else None
            self._send(200, out)
        elif method == "POST" and len(parts) == 2 and parts[1] == "finalize":
            rev = finalize_hydro_revision(conn, rev)
            result = json.loads(rev["result"])
            self._send(200, {"id": rev["id"], "seq": rev["seq"],
                             "status": rev["status"],
                             "finalized_at": rev["finalized_at"],
                             "rounds_used": result.get("rounds_used"),
                             "revision_seq": result.get("revision_seq")})
        elif method == "GET" and len(parts) == 2 and parts[1] in \
                ("stages.json", "load-settlement.svg"):
            if rev["status"] != "final":
                raise ApiError(409, "HYDRO_NOT_FINAL",
                               "修订未定稿，不能导出成果（当前状态 %s）"
                               % rev["status"])
            result = json.loads(rev["result"])
            if parts[1] == "stages.json":
                self._send(200, result, "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="hydro_t%d_r%d.json"'
                            % (test["id"], rev["seq"])})
            else:
                self._send(200, build_hydro_svg(result), "image/svg+xml")
        else:
            raise ApiError(404, "NOT_FOUND", "不支持的操作")

    def _method(self):
        return self.command


def serve(host, port, db_path):
    init_db(connect(db_path))
    Handler.db_path = db_path
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("罐底沉降监测 API: http://%s:%d  (db=%s)" % (host, port, db_path))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


# ----------------------------------------------------------------------------
# 演示 / 自测数据
# ----------------------------------------------------------------------------

def seed_demo(conn):
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO tank(name,radius_m,height_m,wall_alpha,ref_height_m,"
        "bottom_slope_spec,created_at) VALUES('T-101',20,18,1.2e-5,15,0.0083,?)",
        (now_iso(),))
    tank_id = cur.lastrowid
    mk = {}
    npt = 16
    for i in range(npt):
        az = i * 360.0 / npt
        cur = conn.execute(
            "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order) VALUES(?,?,?,?)",
            (tank_id, "M%02d" % (i + 1), az, i + 1))
        mk[i] = cur.lastrowid
    conn.execute("INSERT INTO marker(tank_id,code,azimuth_deg,ring_order,is_center)"
                 " VALUES(?,?,?,?,1)", (tank_id, "C00", 0, 0))
    center_id = conn.execute(
        "SELECT id FROM marker WHERE tank_id=? AND code='C00'", (tank_id,)).fetchone()[0]
    conn.execute("UPDATE tank SET center_marker_id=? WHERE id=?",
                 (center_id, tank_id))

    cur = conn.execute("INSERT INTO benchmark(code,assumed_stable,description,created_at)"
                       " VALUES('BM0',1,'厂区假定稳定原点',?)", (now_iso(),))
    bm0 = cur.lastrowid
    cur = conn.execute("INSERT INTO benchmark(code,assumed_stable,description,created_at)"
                       " VALUES('BM1',0,'东侧校验基准点',?)", (now_iso(),))
    bm1 = cur.lastrowid

    import random
    random.seed(7)

    def make_round(seq, date, origin_reading, temp, level, drift_bm0=0.0,
                   misclosure=0.001, missing=(), tilt=0.004, local=0.003,
                   body=0.0):
        rid = conn.execute(
            "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
            "origin_reading_m,liquid_level_m,wall_temp_c,load_coeff_m_per_m,"
            "loop_misclosure_m,loop_length_km) VALUES(?,?,?,?,?,?,?,?,?,1.2)",
            (tank_id, seq, date, bm0, origin_reading, level, temp, 0.0002,
             misclosure)).lastrowid
        for i in range(npt):
            if i in missing:
                continue
            az = math.radians(i * 360.0 / npt)
            H = origin_reading + 1.0
            H -= body + tilt * math.cos(az - math.radians(60))
            H -= local * math.cos(3 * az)
            # 归算中会按 +α*h*T + cL 改正，造数据时反向扣除
            H -= 1.2e-5 * 15 * temp + 0.0002 * level
            H += random.uniform(-0.0006, 0.0006)
            conn.execute(
                "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx)"
                " VALUES(?,?,?,?)", (rid, mk[i], H, i + 1))
        Hc = origin_reading + 0.78 - 1.2e-5 * 15 * 0 - 0.0002 * level \
            - body * 0.3
        conn.execute("INSERT INTO reading(round_id,marker_id,elevation_m) VALUES(?,?,?)",
                     (rid, center_id, Hc))
        conn.execute("INSERT INTO round_tie(round_id,benchmark_id,tie_elevation_m,"
                     "delta_stable_m) VALUES(?,?,?,?)",
                     (rid, bm1, origin_reading + 2.35 - drift_bm0, -drift_bm0))
        return rid

    make_round(1, "2025-03-10T09:00:00Z", 10.000, 5, 0,
               body=0.0, tilt=0.0, local=0.0)
    make_round(2, "2025-06-10T09:00:00Z", 10.012, 28, 12, drift_bm0=0.0005,
               body=0.012, tilt=0.004, local=0.003, missing=(7,))
    conn.commit()
    return tank_id


def selftest(db_path=":memory:"):
    conn = connect(db_path)
    tank_id = seed_demo(conn)

    # 正常版本
    rounds = allrows(conn, "SELECT id FROM round WHERE tank_id=? ORDER BY seq",
                     (tank_id,))
    v_id = conn.execute(
        "INSERT INTO version(tank_id,seq,round_a_id,round_b_id,excluded_readings,"
        "params,reason,status,created_at) VALUES(1,1,?,?,?,?,?,'draft',?)",
        (rounds[0]["id"], rounds[1]["id"], "{}", "{}",
         "例行复算（自测）", now_iso())).lastrowid
    v = one(conn, "SELECT * FROM version WHERE id=?", (v_id,))
    res = analyze_version(conn, v)
    codes = {i["code"] for i in res["issues"]}
    assert "MISSING" in codes, "应检出漏测"
    assert abs(res["decomposition"]["body_m"] - 0.012) < 0.003, \
        "整体升降应≈12mm，实际 %s" % res["decomposition"]["body_m"]
    assert abs(res["tilt"]["diameter_diff_m"] - 0.008) < 0.004
    assert "3" in res["harmonics"], "应检出3阶谐波"
    blocked = [e for e in res["edges"] if e["blocked"]]
    assert blocked, "漏测点两侧弧段应被阻断"
    print("正常版本: 整体 %+.1fmm, 直径差 %.1fmm, 3阶振幅 %.1fmm, 阻断弧 %d/%d"
          % (res["decomposition"]["body_m"] * 1000,
             res["tilt"]["diameter_diff_m"] * 1000,
             res["harmonics"]["3"]["amp_m"] * 1000,
             len(blocked), len(res["edges"])))

    csv_out = build_resurvey_csv(res)
    svg = build_svg(res)
    assert "漏测补测".encode("utf-8") in csv_out
    assert svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert len(svg) > 500

    # 致命版本：环线闭合差超限 + 原点漂移
    bad_id = conn.execute(
        "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
        "origin_reading_m,liquid_level_m,wall_temp_c,load_coeff_m_per_m,"
        "loop_misclosure_m,loop_length_km) VALUES(1,3,?,(SELECT id FROM benchmark "
        "WHERE code='BM0'),10.020,12,30,0.0002,0.020,1.2)",
        ("2025-09-10T09:00:00Z",)).lastrowid
    markers = allrows(conn, "SELECT id FROM marker WHERE tank_id=? AND is_center=0 "
                            "ORDER BY azimuth_deg", (tank_id,))
    for m in markers:
        conn.execute("INSERT INTO reading(round_id,marker_id,elevation_m) VALUES(?,?,?)",
                     (bad_id, m["id"], 9.99))
    bm1 = one(conn, "SELECT id FROM benchmark WHERE code='BM1'")["id"]
    conn.execute("INSERT INTO round_tie(round_id,benchmark_id,tie_elevation_m,"
                 "delta_stable_m) VALUES(?,?,?,?)",
                 (bad_id, bm1, 10.020 + 2.35, -0.006))
    conn.commit()
    v2id = conn.execute(
        "INSERT INTO version(tank_id,seq,round_a_id,round_b_id,params,reason,"
        "status,created_at) VALUES(1,2,?,?,?,?,'draft',?)",
        (rounds[0]["id"], bad_id, "{}", "闭合差与原点失稳测试", now_iso())).lastrowid
    res2 = analyze_version(conn, one(conn, "SELECT * FROM version WHERE id=?",
                                     (v2id,)))
    c2 = {i["code"] for i in res2["issues"]}
    assert "LOOP_CLOSURE" in c2, "应检出环线闭合差超限"
    assert "ORIGIN_UNSTABLE" in c2, "应检出原点失稳"
    assert res2["status"] == "rejected"
    assert all(e["blocked"] for e in res2["edges"])
    assert any(r["code"] == "LOOP_CLOSURE" for r in res2["resurvey"])
    print("致命版本: 检出 %s，全部弧段阻断，补测项 %d 条，版本拒绝锁定"
          % (",".join(sorted(c2)), len(res2["resurvey"])))

    reg1_origin_switch_rereduce(conn)
    reg2_stable_zero(conn)
    reg3_reading_order_idx(conn)
    reg4_hydro_staging(conn)

    print("SELF-TEST PASS")
    return True


def _make_version_row(conn, tank_id, seq, ra_id, rb_id, reason,
                      origin_id=None, excluded=None):
    return one(conn,
        "SELECT * FROM version WHERE id=(?)",
        (conn.execute(
            "INSERT INTO version(tank_id,seq,round_a_id,round_b_id,"
            "origin_benchmark_id,excluded_readings,params,reason,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?, 'draft', ?)",
            (tank_id, seq, ra_id, rb_id, origin_id,
             json.dumps(excluded or {}, ensure_ascii=False), "{}",
             reason, now_iso())).lastrowid,))


# ---------------------------------------------------------------------------
# 回归 1：改选原点必须按各轮 round_tie.tie_elevation_m 重新归算
# ---------------------------------------------------------------------------

def reg1_origin_switch_rereduce(conn):
    tid = conn.execute(
        "INSERT INTO tank(name,radius_m,wall_alpha,ref_height_m,created_at)"
        " VALUES('RG1',10,1.2e-5,0,?)", (now_iso(),)).lastrowid
    nm = 8
    mids = []
    for i in range(nm):
        mids.append(conn.execute(
            "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order) VALUES(?,?,?,?)",
            (tid, "R1-%02d" % (i + 1), i * 360.0 / nm, i + 1)).lastrowid)

    bm0 = conn.execute(
        "INSERT INTO benchmark(code,assumed_stable,description,created_at)"
        " VALUES('R1-BM0',1,'原观测原点',?)", (now_iso(),)).lastrowid
    bm1 = conn.execute(
        "INSERT INTO benchmark(code,assumed_stable,description,created_at)"
        " VALUES('R1-BM1',1,'改选原点（假定稳定，无校准）',?)", (now_iso(),)).lastrowid

    # 两轮无温度/液位改正；O0 读数不变，O1 两轮读数相差 -0.0005 m；
    # 基础真实沉降：轮1→轮2 为 0.010 m（标志高程两轮差 -0.010）
    O0_1, O0_2 = 20.000, 20.000
    O1_1, O1_2 = 22.350, 22.3495
    def mk_round(seq, date, O0, O1):
        rid = conn.execute(
            "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
            "origin_reading_m,liquid_level_m,wall_temp_c,loop_misclosure_m,"
            "loop_length_km) VALUES(?,?,?,?,?,0,0,0.001,1)",
            (tid, seq, date, bm0, O0)).lastrowid
        for j, mid in enumerate(mids):
            conn.execute(
                "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx)"
                " VALUES(?,?,?,?)",
                (rid, mid, O0 + 1.0 - (0.010 if seq == 2 else 0.0), j + 1))
        conn.execute("INSERT INTO round_tie(round_id,benchmark_id,tie_elevation_m)"
                     " VALUES(?,?,?)", (rid, bm1, O1))
        return rid

    r1 = mk_round(1, "2025-01-10T00:00:00Z", O0_1, O1_1)
    r2 = mk_round(2, "2025-04-10T00:00:00Z", O0_2, O1_2)
    conn.commit()

    # 原原点版本：body 应恰为 0.010
    v0 = _make_version_row(conn, tid, 101, r1, r2, "回归1-原原点对照")
    res0 = analyze_version(conn, v0)
    assert res0["status"] == "draft", res0
    assert abs(res0["decomposition"]["body_m"] - 0.010) < 1e-9, \
        res0["decomposition"]["body_m"]

    # 改选 BM1：必须逐轮按 tie_elevation_m 重算，整体升降相应 -0.0005
    v1 = _make_version_row(conn, tid, 102, r1, r2,
                           "回归1-改选 R1-BM1（联测显示其更稳定）",
                           origin_id=bm1)
    res1 = analyze_version(conn, v1)
    fatal = [i for i in res1["issues"] if i["severity"] == "fatal"]
    assert not fatal, [i["code"] for i in fatal]
    codes = {i["code"] for i in res1["issues"]}
    assert "ORIGIN_SWITCHED" in codes
    assert res1["rounds"]["a"]["origin_source"] == "round_tie.tie_elevation_m"
    assert res1["rounds"]["b"]["origin_source"] == "round_tie.tie_elevation_m"
    assert abs(res1["rounds"]["a"]["origin_reading_m"] - O1_1) < 1e-12
    assert abs(res1["rounds"]["b"]["origin_reading_m"] - O1_2) < 1e-12
    body1 = res1["decomposition"]["body_m"]
    delta = body1 - res0["decomposition"]["body_m"]
    assert abs(body1 - 0.0095) < 1e-9, body1
    assert abs(delta - (-0.0005)) < 1e-9, delta
    assert res1["points"][0]["settlement_m"] is not None
    print("回归1 改选原点重归算: body %.4f -> %.4f（Δ %+.4f m，符合 -0.0005）"
          % (res0["decomposition"]["body_m"], body1, delta))


# ---------------------------------------------------------------------------
# 回归 2：calibration.stable=0 必须判失稳，阻止生成可锁定结果
# ---------------------------------------------------------------------------

def reg2_stable_zero(conn):
    tid = conn.execute(
        "INSERT INTO tank(name,radius_m,wall_alpha,ref_height_m,created_at)"
        " VALUES('RG2',10,1.2e-5,0,?)", (now_iso(),)).lastrowid
    mids = []
    for i in range(8):
        mids.append(conn.execute(
            "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order) VALUES(?,?,?,?)",
            (tid, "R2-%02d" % (i + 1), i * 45.0, i + 1)).lastrowid)
    bm = conn.execute(
        "INSERT INTO benchmark(code,assumed_stable,description,created_at)"
        " VALUES('R2-BM0',0,'有校准史但近期失稳',?)", (now_iso(),)).lastrowid

    # 稳定校准：2024 年内 delta=0 stable=1
    conn.execute("INSERT INTO calibration(benchmark_id,date,delta_m,stable,note)"
                 " VALUES(?,?,0,1,'年度复测稳定')", (bm, "2024-06-01"))
    # 失稳校准：2025-03 复查 stable=0（两轮观测都在此之后，必须命中）
    conn.execute("INSERT INTO calibration(benchmark_id,date,delta_m,stable,note)"
                 " VALUES(?,?,-0.005,0,'厂区新管线沉降，点位移出')",
                 (bm, "2025-03-01"))

    def mk_round(seq, date):
        rid = conn.execute(
            "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
            "origin_reading_m,wall_temp_c,loop_misclosure_m,loop_length_km)"
            " VALUES(?,?,?,?,?,0,0.001,1)",
            (tid, seq, date, bm, 15.0)).lastrowid
        for j, mid in enumerate(mids):
            conn.execute(
                "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx)"
                " VALUES(?,?,?,?)", (rid, mid, 16.0 - 0.002 * seq, j + 1))
        return rid

    r1 = mk_round(1, "2025-04-01T00:00:00Z")
    r2 = mk_round(2, "2025-05-01T00:00:00Z")
    conn.commit()

    v = _make_version_row(conn, tid, 201, r1, r2, "回归2-stable=0 失稳判定")
    res = analyze_version(conn, v)
    inst = [i for i in res["issues"] if i["code"] == "ORIGIN_UNSTABLE"
            and i["severity"] == "fatal"]
    assert len(inst) == 2, [i["message"] for i in inst]   # 两轮各一条
    assert all(i["metric"] == "calibration.stable" for i in inst)
    assert res["status"] == "rejected"
    assert all(e["blocked"] for e in res["edges"])
    assert any(r.get("benchmark") == "R2-BM0" for r in res["resurvey"])
    assert res["decomposition"] is None
    print("回归2 stable=0: 两轮均判原点失稳，状态 rejected，全部 %d 弧阻断，不可锁定"
          % len(res["edges"]))


# ---------------------------------------------------------------------------
# 回归 3：次序校验读取逐轮 reading.order_idx；15 条有效读数反向触发并阻断
# ---------------------------------------------------------------------------

def reg3_reading_order_idx(conn):
    tid = conn.execute(
        "INSERT INTO tank(name,radius_m,wall_alpha,ref_height_m,created_at)"
        " VALUES('RG3',10,1.2e-5,0,?)", (now_iso(),)).lastrowid
    n = 16
    mids = []
    for i in range(n):
        mids.append(conn.execute(
            "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order) VALUES(?,?,?,?)",
            # ring_order 故意保持正常升序：若仍误用它就不会报倒置
            (tid, "R3-%02d" % (i + 1), i * 22.5, i + 1)).lastrowid)
    bm = conn.execute(
        "INSERT INTO benchmark(code,assumed_stable,description,created_at)"
        " VALUES('R3-BM0',1,'原点',?)", (now_iso(),)).lastrowid

    exclude_idx = 8   # R3-09 读数经版本剔除：16 条观测 -> 15 条有效读数

    def mk_round(seq, date, reversed_order):
        rid = conn.execute(
            "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
            "origin_reading_m,wall_temp_c,loop_misclosure_m,loop_length_km)"
            " VALUES(?,?,?,?,?,0,0.001,1)",
            (tid, seq, date, bm, 30.0)).lastrowid
        for i, mid in enumerate(mids):
            if reversed_order:
                # 反向施测：方位 0° 的点最后测；order_idx 随方位递减
                order = n - i
            else:
                order = i + 1
            conn.execute(
                "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx)"
                " VALUES(?,?,?,?)", (rid, mid, 31.0 - 0.001 * seq, order))
        return rid

    r_ok = mk_round(1, "2025-02-01T00:00:00Z", reversed_order=False)
    r_rev = mk_round(2, "2025-03-01T00:00:00Z", reversed_order=True)
    excl_row = one(conn, "SELECT id FROM reading WHERE round_id=? AND marker_id=?",
                   (r_rev, mids[exclude_idx]))
    conn.commit()

    excluded = {excl_row["id"]: "扶尺碰动，按流程剔除（回归3）"}
    v = _make_version_row(conn, tid, 301, r_ok, r_rev,
                          "回归3-order_idx 反向施测", excluded=excluded)
    res = analyze_version(conn, v)
    inv = [i for i in res["issues"] if i["code"] == "ORDER_INVERSION"]
    # 剔除点把反向序列拆成两段（7 弧 + 6 弧），故有两条问题
    assert len(inv) == 2, [i["message"] for i in inv]
    assert all(x["round_ids"] == [r_rev] for x in inv)
    assert sum("条有效读数弧段" in x["message"] for x in inv)
    arc_counts = [int(x["message"].split("（")[1].split(" ")[0]) for x in inv]
    assert sorted(arc_counts) == [6, 7], arc_counts
    # 15 条有效读数反向：收测闭合边（序16点→序1点）不判倒置，是唯一保留弧；
    # 剔除点两侧邻边 2 条（含闭合边）按"读数已剔除"阻断；倒置阻断
    # 16-1-2=13 条（两段 7+6），剔除补阻断 2 条，合计 15/16
    blocked = [e for e in res["edges"] if e["blocked"]]
    inv_block = [e for e in res["edges"]
                 if any("观测次序倒置" in x for x in e["block_reasons"])]
    excl_block = [e for e in res["edges"]
                  if any("已剔除" in x for x in e["block_reasons"])]
    assert len(inv_block) == 13, len(inv_block)
    assert len(excl_block) == 2, [(e["from_code"], e["to_code"]) for e in excl_block]
    assert len(blocked) == 15, len(blocked)
    rev_readings = allrows(conn, "SELECT id,marker_id FROM reading WHERE round_id=?",
                           (r_rev,))
    valid_ids = {r["id"] for r in rev_readings if r["id"] != excl_row["id"]}
    reported = {rid for x in inv for rid in x["reading_ids"]}
    assert valid_ids == reported, "应回指 15 条有效原始读数 id"
    print("回归3 reading.order_idx: 15 条有效读数反向 -> ORDER_INVERSION"
          "（%d+%d 弧两段），倒置阻断 %d、剔除补阻断 %d、合计 %d/%d"
          "（闭合边保留），回指 15 个读数 id"
          % (sorted(arc_counts)[0], sorted(arc_counts)[1],
             len(inv_block), len(excl_block), len(blocked), len(res["edges"])))

    # 对照：两轮均按升序施测时，不应产生 ORDER_INVERSION
    r_ok2 = mk_round(3, "2025-04-01T00:00:00Z", reversed_order=False)
    conn.commit()
    vc = _make_version_row(conn, tid, 302, r_ok, r_ok2, "回归3-升序对照")
    resc = analyze_version(conn, vc)
    assert not any(i["code"] == "ORDER_INVERSION" for i in resc["issues"])
    print("回归3 对照组: 升序施测无误报 ORDER_INVERSION")


# ---------------------------------------------------------------------------
# 回归 4：分级加载（水压试验）——槽位绑定、待判规则、限值回指与修订定稿
# ---------------------------------------------------------------------------

def reg4_hydro_staging(conn):
    tid = conn.execute(
        "INSERT INTO tank(name,radius_m,wall_alpha,ref_height_m,created_at)"
        " VALUES('RG4',10,1.2e-5,0,?)", (now_iso(),)).lastrowid
    n = 8
    mids = []
    for i in range(n):
        mids.append(conn.execute(
            "INSERT INTO marker(tank_id,code,azimuth_deg,ring_order) VALUES(?,?,?,?)",
            (tid, "R4-%02d" % (i + 1), i * 45.0, i + 1)).lastrowid)
    bm = conn.execute(
        "INSERT INTO benchmark(code,assumed_stable,description,created_at)"
        " VALUES('R4-BM0',1,'原点',?)", (now_iso(),)).lastrowid

    # 均匀沉降序列（m）：空罐 0 → 充1 0.012 → 保1 0.0195（30h，速率 0.25mm/h
    # 超 0.2 限值）→ 充2 0.0315 → 保2 0.0351（36h，0.1mm/h 合格）→
    # 卸载 0.0135（回弹 61.5%）→ 复测 0.012（残余 12mm 超 10mm 限值）
    LEVEL = {1: 0, 2: 6, 3: 6, 4: 12, 5: 12, 6: 0, 7: 0}
    SETTLE = {1: 0.0, 2: 0.012, 3: 0.0195, 4: 0.0315, 5: 0.0351,
              6: 0.0135, 7: 0.012}
    TIME = {1: "2025-01-01T00:00:00Z", 2: "2025-01-02T00:00:00Z",
            3: "2025-01-03T06:00:00Z", 4: "2025-01-04T00:00:00Z",
            5: "2025-01-05T12:00:00Z", 6: "2025-01-06T00:00:00Z",
            7: "2025-01-08T00:00:00Z"}
    rounds = {}

    def mk_round(seq, level, date, settle, misclosure=0.001):
        rid = conn.execute(
            "INSERT INTO round(tank_id,seq,measured_at,origin_benchmark_id,"
            "origin_reading_m,liquid_level_m,wall_temp_c,loop_misclosure_m,"
            "loop_length_km) VALUES(?,?,?,?,10.0,?,0,?,1)",
            (tid, seq, date, bm, level, misclosure)).lastrowid
        for j, mid in enumerate(mids):
            conn.execute(
                "INSERT INTO reading(round_id,marker_id,elevation_m,order_idx)"
                " VALUES(?,?,?,?)", (rid, mid, 11.0 - settle, j + 1))
        rounds[seq] = rid
        return rid

    for sq in range(1, 8):
        mk_round(sq, LEVEL[sq], TIME[sq], SETTLE[sq])
    # 负例专用轮次
    mk_round(8, 6, "2025-01-02T06:00:00Z", 0.0125)              # 保压仅 6h
    mk_round(9, 6, "2025-01-04T00:00:00Z", 0.020)               # 充水液位未升高
    mk_round(10, 0, "2025-01-08T00:00:00Z", 0.012, misclosure=0.05)  # 闭合差超限
    conn.commit()

    test_id = conn.execute(
        "INSERT INTO hydro_test(tank_id,code,density_kg_m3,created_at)"
        " VALUES(?,'RG4-HT',1000,?)", (tid, now_iso())).lastrowid
    for sq, (tgt, hold, rate, res) in enumerate(
            [(6, 24, 0.0002, 0.010), (12, 24, 0.0002, 0.010)], 1):
        conn.execute(
            "INSERT INTO hydro_stage(test_id,seq,target_level_m,min_hold_hours,"
            "rate_limit_m_per_h,residual_limit_m) VALUES(?,?,?,?,?,?)",
            (test_id, sq, tgt, hold, rate, res))
    conn.commit()
    test = one(conn, "SELECT * FROM hydro_test WHERE id=?", (test_id,))
    stages = allrows(conn, "SELECT * FROM hydro_stage WHERE test_id=? "
                           "ORDER BY seq", (test_id,))

    def bindings(**kw):
        b = {"empty": rounds[1],
             "stages": [{"fill": rounds[2], "hold": rounds[3]},
                        {"fill": rounds[4], "hold": rounds[5]}],
             "unload": rounds[6], "recheck": rounds[7]}
        b.update(kw)
        return b

    # 1) 正常修订：速率/残余越限回指阶段与读数，但不阻断定稿
    rev1, res1 = create_hydro_revision(conn, test, stages, bindings(),
                                       "回归4-正常分级试验")
    assert res1["status"] == "draft", res1["status"]
    st1, st2 = res1["stages"]
    assert abs(st1["hold_hours"] - 30.0) < 1e-9
    assert abs(st2["hold_hours"] - 36.0) < 1e-9
    m0 = st1["markers"][0]
    assert abs(m0["load_increment_m"] - 0.012) < 1e-9
    assert abs(m0["hold_increment_m"] - 0.0075) < 1e-9
    assert abs(m0["hold_rate_m_per_h"] - 0.00025) < 1e-9
    assert abs(st2["markers"][0]["hold_rate_m_per_h"] - 0.0001) < 1e-9
    rate_issues = [i for i in res1["issues"] if i["code"] == "SETTLEMENT_RATE"]
    assert len(rate_issues) == 1 and rate_issues[0]["stage_seq"] == 1
    assert abs(rate_issues[0]["value"] - 0.00025) < 1e-9
    assert rate_issues[0]["limit"] == 0.0002
    assert len(rate_issues[0]["reading_ids"]) == 2 * n   # 8 标志 × 充/保读数
    res_issues = [i for i in res1["issues"] if i["code"] == "RESIDUAL_SETTLEMENT"]
    assert len(res_issues) == 1 and res_issues[0]["stage_seq"] == 2
    assert abs(res_issues[0]["value"] - 0.012) < 1e-9
    assert res_issues[0]["limit"] == 0.010
    mk0 = res1["markers"][0]
    assert abs(mk0["rebound_ratio"] - (0.0351 - 0.0135) / 0.0351) < 1e-9
    assert abs(mk0["residual_m"] - 0.012) < 1e-9
    assert mk0["hysteresis_area_kpa_mm"] > 0
    assert len(res1["rounds_used"]) == 7
    assert abs(res1["stages"][0]["fill_load_kpa"] - 1000 * 9.80665 * 6 / 1000) < 1e-6
    print("回归4 正常修订: 保压速率 %.3f/%.3f mm/h（限 0.200），残余 %.1f mm"
          "（限 10.0），回弹率 %.1f%%，滞回环 %.1f kPa·mm，状态 draft"
          % (st1["markers"][0]["hold_rate_m_per_h"] * 1000,
             st2["markers"][0]["hold_rate_m_per_h"] * 1000,
             mk0["residual_m"] * 1000, mk0["rebound_ratio"] * 100,
             mk0["hysteresis_area_kpa_mm"]))

    # 2) 待判规则：五类致命情形均保持 pending
    _, res = create_hydro_revision(
        conn, test, stages,
        bindings(stages=[{"fill": rounds[2], "hold": rounds[2]},
                         {"fill": rounds[4], "hold": rounds[5]}]),
        "回归4-同一轮次重复绑定")
    codes = {i["code"] for i in res["issues"]}
    assert "DUPLICATE_BINDING" in codes and res["status"] == "pending"

    _, res = create_hydro_revision(
        conn, test, stages,
        bindings(stages=[{"fill": rounds[3], "hold": rounds[2]},
                         {"fill": rounds[4], "hold": rounds[5]}]),
        "回归4-阶段倒序")
    codes = {i["code"] for i in res["issues"]}
    assert "STAGE_ORDER" in codes and res["status"] == "pending"

    _, res = create_hydro_revision(
        conn, test, stages,
        bindings(stages=[{"fill": rounds[2], "hold": rounds[3]},
                         {"fill": rounds[9], "hold": rounds[5]}]),
        "回归4-荷载方向不符")
    codes = {i["code"] for i in res["issues"]}
    assert "LOAD_DIRECTION" in codes and res["status"] == "pending"

    _, res = create_hydro_revision(
        conn, test, stages,
        bindings(stages=[{"fill": rounds[2], "hold": rounds[8]},
                         {"fill": rounds[4], "hold": rounds[5]}]),
        "回归4-观测间隔不足")
    codes = {i["code"] for i in res["issues"]}
    assert "HOLD_TOO_SHORT" in codes and res["status"] == "pending"

    _, res = create_hydro_revision(conn, test, stages,
                                   bindings(recheck=rounds[10]),
                                   "回归4-来源轮次含致命问题")
    codes = {i["code"] for i in res["issues"]}
    assert "LOOP_CLOSURE" in codes and "ROUND_FATAL" in codes
    assert res["status"] == "pending"
    print("回归4 待判规则: 重复绑定/阶段倒序/荷载方向/间隔不足/轮次致命"
          " 五类均保持 pending")

    # 3) 改取阈值 → 新修订留痕，越限判定随阈值变化
    rev6, res6 = create_hydro_revision(
        conn, test, stages, bindings(),
        "回归4-改取阈值：速率限值放宽至 0.3 mm/h（岩土复核意见）",
        thresholds={"stages": {"1": {"rate_limit_m_per_h": 0.0003},
                               "2": {"rate_limit_m_per_h": 0.0003}}})
    assert res6["status"] == "draft"
    assert not any(i["code"] == "SETTLEMENT_RATE" for i in res6["issues"])
    assert abs(res6["stages"][0]["rate_limit_m_per_h"] - 0.0003) < 1e-12
    assert abs(res6["stages"][0]["plan"]["rate_limit_m_per_h"] - 0.0002) < 1e-12
    assert rev6["seq"] == 7                       # 本试验第 7 个修订
    assert "改取阈值" in rev6["reason"]
    _, res7 = create_hydro_revision(
        conn, test, stages, bindings(),
        "回归4-改取残余限值至 15 mm",
        thresholds={"stages": {"2": {"residual_limit_m": 0.015}}})
    assert not any(i["code"] == "RESIDUAL_SETTLEMENT" for i in res7["issues"])
    print("回归4 改取阈值: 修订 R%d 速率限值 0.2→0.3 mm/h 后 SETTLEMENT_RATE "
          "消除；残余限值 10→15 mm 后 RESIDUAL_SETTLEMENT 消除" % rev6["seq"])

    # 4) 定稿与成果：draft 可定稿并保存所用轮次；pending 拒绝定稿
    fin = finalize_hydro_revision(
        conn, one(conn, "SELECT * FROM hydro_revision WHERE id=?",
                  (rev1["id"],)))
    assert fin["status"] == "final" and fin["finalized_at"]
    pend = one(conn, "SELECT * FROM hydro_revision WHERE test_id=? AND "
                     "status='pending' ORDER BY seq", (test_id,))
    try:
        finalize_hydro_revision(conn, pend)
        raise AssertionError("待判修订不应允许定稿")
    except ApiError as e:
        assert e.code == "HYDRO_PENDING"
    svg = build_hydro_svg(res1)
    assert svg.lstrip().startswith("<svg") and "滞回" in svg and "荷载" in svg
    js = json.dumps(res1, ensure_ascii=False)
    assert "hold_rate_m_per_h" in js and "rounds_used" in js
    print("回归4 定稿导出: 修订 R%d 定稿（保存 %d 个所用轮次），待判修订拒绝"
          "定稿，逐级 JSON 与荷载—沉降 SVG 齐备"
          % (rev1["seq"], len(res1["rounds_used"])))


def main():
    ap = argparse.ArgumentParser(description="罐底沉降监测 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="tank_settlement.db")
    ap.add_argument("--selftest", action="store_true", help="运行内置自测")
    ap.add_argument("--demo", action="store_true", help="植入演示数据后启动服务")
    args = ap.parse_args()

    if args.selftest:
        selftest(":memory:")
        return
    if args.demo:
        conn = connect(args.db)
        seed_demo(conn)
        conn.close()
        print("演示数据已写入 %s" % args.db)
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
