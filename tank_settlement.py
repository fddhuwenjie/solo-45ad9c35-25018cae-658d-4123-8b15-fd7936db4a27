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
        "SELECT delta_m FROM calibration WHERE benchmark_id=? AND date<=? "
        "ORDER BY date DESC, id DESC LIMIT 1", (benchmark_id, date)).fetchone()
    return (float(r["delta_m"]), True) if r else (0.0, False)


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

    # -- 原点与校准 -----------------------------------------------------------
    origin = one(conn, "SELECT * FROM benchmark WHERE id=?",
                 ((version["origin_benchmark_id"] or ra["origin_benchmark_id"]),))
    if version["origin_benchmark_id"] and \
            version["origin_benchmark_id"] != ra["origin_benchmark_id"]:
        issue("ORIGIN_SWITCHED", "info",
              "本版本改选原点为 %s（理由：%s）" % (origin["code"], version["reason"]),
              benchmark_ids=[origin["id"]])

    def origin_delta(rnd):
        bid = origin["id"]
        d, has = cal_delta(conn, bid, rnd["measured_at"])
        if has or origin["assumed_stable"]:
            return d, has
        return None, False

    d_o_a, cal_a = origin_delta(ra)
    d_o_b, cal_b = origin_delta(rb)

    round_fatal = {ra["id"]: [], rb["id"]: []}   # 每轮的致命问题代码

    if d_o_a is None:
        issue("UNTIED", "fatal",
              "轮次 %d 的水准原点 BM-%s 无校准资料且非假定稳定点，无法归算"
              % (ra["seq"], origin["code"]),
              round_ids=[ra["id"]], benchmark_ids=[origin["id"]],
              resurvey={"type": "原点校准补测", "benchmark": origin["code"]})
        round_fatal[ra["id"]].append("UNTIED")
    if d_o_b is None:
        issue("UNTIED", "fatal",
              "轮次 %d 的水准原点 BM-%s 无校准资料且非假定稳定点，无法归算"
              % (rb["seq"], origin["code"]),
              round_ids=[rb["id"]], benchmark_ids=[origin["id"]],
              resurvey={"type": "原点校准补测", "benchmark": origin["code"]})
        round_fatal[rb["id"]].append("UNTIED")
    if not cal_a:
        issue("CAL_MISSING", "info", "原点在轮次 %d 观测日前无校准记录，按 0 处理" % ra["seq"],
              round_ids=[ra["id"]])
    if not cal_b:
        issue("CAL_MISSING", "info", "原点在轮次 %d 观测日前无校准记录，按 0 处理" % rb["seq"],
              round_ids=[rb["id"]])

    # -- 环线闭合差 -----------------------------------------------------------
    def check_loop(rnd):
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
            round_fatal[rnd["id"]].append("LOOP_CLOSURE")

    check_loop(ra)
    check_loop(rb)

    # -- 联系水准：原点稳定性（跨轮） -----------------------------------------
    ties_a = {t["benchmark_id"]: dict(t) for t in conn.execute(
        "SELECT * FROM round_tie WHERE round_id=?", (ra["id"],))}
    ties_b = {t["benchmark_id"]: dict(t) for t in conn.execute(
        "SELECT * FROM round_tie WHERE round_id=?", (rb["id"],))}

    rel_changes = []
    for bid in sorted(set(ties_a) & set(ties_b)):
        bm = one(conn, "SELECT * FROM benchmark WHERE id=?", (bid,))
        dja, _ = cal_delta(conn, bid, ra["measured_at"])
        djb, _ = cal_delta(conn, bid, rb["measured_at"])
        if not bm["assumed_stable"]:
            dja = ties_a[bid]["delta_stable_m"] if ties_a[bid]["delta_stable_m"] is not None else dja
            djb = ties_b[bid]["delta_stable_m"] if ties_b[bid]["delta_stable_m"] is not None else djb
        ch = (ties_b[bid]["tie_elevation_m"] - (rb["origin_reading_m"] or 0)) \
             - (ties_a[bid]["tie_elevation_m"] - (ra["origin_reading_m"] or 0)) \
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

    # -- 每轮：漏测 + 观测次序倒置 --------------------------------------------
    observed = {ra["id"]: set(rda), rb["id"]: set(rdb)}

    def order_check(rnd, rdict):
        obs = [m for m in ring if m["id"] in rdict and m["ring_order"] is not None]
        inversions = set()
        for i in range(len(obs) - 1):
            m1, m2 = obs[i], obs[i + 1]
            if int(m2["ring_order"]) <= int(m1["ring_order"]):
                inversions.add((m1["id"], m2["id"]))
                issue("ORDER_INVERSION", "warning",
                      "轮次 %d 观测次序倒置：%s(序%s) -> %s(序%s)，该邻边不计算"
                      % (rnd["seq"], m1["code"], m1["ring_order"],
                         m2["code"], m2["ring_order"]),
                      round_ids=[rnd["id"]], marker_ids=[m1["id"], m2["id"]],
                      edge=[m1["id"], m2["id"]],
                      resurvey={"type": "观测次序核查", "round_seq": rnd["seq"],
                                "markers": [m1["code"], m2["code"]]})
        return inversions

    inv_edges = order_check(ra, rda) | order_check(rb, rdb)
    inv_markers = set(x for e in inv_edges for x in e)

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
        H = float(reading_row["elevation_m"])
        rel = H - (rnd["origin_reading_m"] if rnd["origin_reading_m"] is not None else 0)
        d_o, _ = origin_delta(rnd)
        d_o = d_o or 0.0
        therm = 0.0 if m["is_center"] else alpha * href * float(rnd["wall_temp_c"] or 0)
        load = float(rnd["load_coeff_m_per_m"] or 0) * float(rnd["liquid_level_m"] or 0)
        return rel + d_o + therm + load, {"origin_delta_m": d_o,
                                          "thermal_m": therm, "load_m": load}

    corr_a, corr_b = {}, {}
    corr_terms = {}
    for rnd, rdict, store in ((ra, rda, corr_a), (rb, rdb, corr_b)):
        terms = {}
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
            "a": _round_brief(ra, origin, d_o_a, corr_terms.get(ra["id"], {}),
                              center, ring, ra["origin_reading_m"]),
            "b": _round_brief(rb, origin, d_o_b, corr_terms.get(rb["id"], {}),
                              center, ring, rb["origin_reading_m"]),
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


def _round_brief(rnd, origin, d_o, terms, center, ring, origin_reading):
    used = [terms.get(m["id"], {}) for m in ring]
    return {
        "id": rnd["id"], "seq": rnd["seq"], "measured_at": rnd["measured_at"],
        "origin_benchmark": origin["code"],
        "origin_reading_m": origin_reading,
        "origin_delta_m": d_o,
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
                                 "GET  /versions/<id>/recompute.json"]})
            return

        root = parts[0]
        routes = {
            "tanks": self._tanks, "markers": self._markers,
            "benchmarks": self._benchmarks, "calibrations": self._calibrations,
            "rounds": self._rounds, "readings": self._readings,
            "ties": self._ties, "versions": self._versions,
            "analyze": self._analyze,
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

    print("SELF-TEST PASS")
    return True


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
