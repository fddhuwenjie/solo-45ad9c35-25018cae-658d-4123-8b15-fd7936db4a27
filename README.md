# 罐底沉降监测复算 API

单文件 Python 服务，仅使用标准库（`http.server` / `json` / `sqlite3`），用于管理罐区
**罐体尺寸、壳底标志方位、历次高程、装液工况、水准环线及原点校准资料**，并完成两轮观测的
归算、沉降分离、质检与成果导出。

## 运行

```bash
python3 tank_settlement.py --host 0.0.0.0 --port 8080 --db settlement.db
python3 tank_settlement.py --demo          # 植入一个 16 点示范罐后启动
python3 tank_settlement.py --selftest      # 内置数值/质检自测
```

## 数据模型（SQLite）

| 表 | 内容 |
|---|---|
| `tank` | 罐半径/高、罐壁线膨胀系数 α、检尺基准高 `ref_height_m`、设计排水坡度、中心标志 |
| `marker` | 壳底标志编号、方位角（北=0°顺时针）、环向次序、是否中心点 |
| `benchmark` | 水准原点/联系点，`assumed_stable` 标记假定稳定原点 |
| `calibration` | 各基准点按日期的稳定高程系累计改正 `delta_m`、稳定性结论 |
| `round` | 观测轮次：原点读数、液位、罐壁温度、液位-下沉系数、环线闭合差与环线长 |
| `reading` | 逐标志原始高程（**问题回指的原始凭据**） |
| `round_tie` | 每轮对其它基准点的联测高程（判定原点漂移） |
| `version` | 分析版本：选定两轮、可选改选原点、剔除读数及理由、容差参数、锁定状态、结果 JSON |

## 归算与分析

每轮每个标志先归算到稳定高程系（`s>0` 表示下沉）：

```
h_r(m) = H_r(m) − H_r(原点) + Δ原点(date) + α·h_ref·T + c·L
```

- `Δ原点(date)`：取不晚于观测日的最新校准改正，消除**基点漂移**；
- `α·h_ref·T`：罐壁温差改正（钢罐默认 α=1.2×10⁻⁵/℃）；
- `c·L`：装液工况改正（`load_coeff_m_per_m × 液位`）。

两轮差 `s = h_a − h_b` 后用最小二乘分离：

- **整体升降** `a0`；
- **刚性倾斜**：`a_c cosθ + a_s sinθ`，给出振幅、最大下沉方位、直径方向差异沉降与倾斜坡度；
- **环向局部沉降**：残差再做 2..K 阶谐波（默认到 6 阶，按有效点数自动降阶）；
- **邻点变化**：逐弧段沉降差与切向坡度；
- **底板排水坡度**：相对中心标志的径向坡度（反坡/不足报警）。

## 质检规则（命中则对应弧段不计算，并列补测位置）

| 代码 | 规则 | 后果 |
|---|---|---|
| `LOOP_CLOSURE` | 闭合差 > `k·√L`（默认 k=4 mm/√km） | 整轮致命，全环重测 |
| `ORIGIN_UNSTABLE` | 公共联系点相对变化中位值超 `origin_drift_m`（默认 3 mm） | 致命，原点/联系点重新联测 |
| `UNTIED` | 原点无校准且非假定稳定点 | 致命，补原点校准 |
| `DUPLICATE_AZIMUTH` | 方位重号 | 相关弧段阻断、核查编号 |
| `ORDER_INVERSION` | 环向观测次序倒置 | 该邻边阻断、次序核查 |
| `MISSING` | 标志某轮漏测 | 相邻弧段阻断、逐点补测 |
| `GAP_TOO_LONG` | 连续空缺弧 > `max_gap_deg`（默认 90°） | 致命，弧中给定点位补测 |
| `TIE_SPREAD` / `MISSING_FRACTION` / `LOOP_DATA_MISSING` | 警告级 | 结果保留但提示 |
| `TOTAL_SETTLEMENT` / `POINT_SETTLEMENT` / `DIAMETER_DIFF` / `ADJACENT_DIFF` / `HARMONIC_AMPLITUDE` / `BOTTOM_SLOPE_LOW` / `BOTTOM_SLOPE_REVERSED` | 指标超允许值 | 不阻断计算，issue 中带 `reading_ids / marker_ids / round_ids / metric / value / limit` 回指原始数据 |

含 fatal 问题的版本自动置为 `rejected`，不能锁定，也不能导出成果。

## 版本控制

- `POST /analyze` 必须给 `reason`；
- `excluded_readings` 每项必须附剔除理由，否则 400；
- `origin_benchmark_id` 改选原点时必须附 `origin_change_reason`，版本中记录
  `ORIGIN_SWITCHED`；
- 容差通过 `params` 覆盖 `DEFAULT_PARAMS`；版本号同罐递增，结果随版本冻结；
- `POST /versions/<id>/lock` 锁定后成果方可导出（锁定即不可再改，复算请新建版本）。

## HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `POST /tanks`、`GET /tanks[/{id}]`、`POST /tanks/{id}/center` | 罐体维护、设中心标志 |
| `POST /markers`、`GET /markers?tank_id=` | 标志（支持数组批量） |
| `POST /benchmarks`、`GET /benchmarks` | 水准原点/联系点 |
| `POST /calibrations`、`GET /calibrations?benchmark_id=` | 校准资料 |
| `POST /rounds` | 建轮次，可内嵌 `readings` 与 `ties` |
| `GET /rounds[/{id}]` | 轮次明细（含读数、联系点） |
| `POST /readings?round_id=`、`GET /readings?round_id=` | 补录高程 |
| `POST /ties?round_id=`、`GET /ties?round_id=` | 补录联系点 |
| `POST /analyze` | 建分析版本并立即复算，返回完整结果 |
| `GET /versions?tank_id=`、`GET /versions/{id}` | 版本清单/详情 |
| `POST /versions/{id}/lock` | 锁定 |
| `GET /versions/{id}/recompute.json` | **复算 JSON**（仅锁定） |
| `GET /versions/{id}/resurvey.csv` | **补测表**（UTF-8 带 BOM，Excel 直接打开） |
| `GET /versions/{id}/settlement.svg` | **环向沉降图**（极坐标环形图） |

### 建轮次示例

```json
POST /rounds
{
  "tank_id": 1, "seq": 2, "measured_at": "2025-06-10T09:00:00Z",
  "origin_benchmark_id": 1, "origin_reading_m": 10.012,
  "liquid_level_m": 12, "wall_temp_c": 28, "load_coeff_m_per_m": 0.0002,
  "loop_misclosure_m": 0.001, "loop_length_km": 1.2,
  "readings": [{"marker_id": 1, "elevation_m": 10.987, "order_idx": 1}],
  "ties": [{"benchmark_id": 2, "tie_elevation_m": 12.36, "delta_stable_m": -0.0005}]
}
```

### 建版本示例

```json
POST /analyze
{
  "tank_id": 1, "round_a": 1, "round_b": 2,
  "reason": "半年度沉降复算",
  "origin_benchmark_id": 2,
  "origin_change_reason": "BM0 联系水准显示漂移，改用经检定稳定的 BM1",
  "excluded_readings": [
    {"reading_id": 23, "reason": "扶尺碰动，气泡未居中（观测手簿 #6 记录）"}
  ],
  "params": {"adjacent_diff_m": 0.006}
}
```

## 成果说明

- **补测表 CSV**：问题代码、补测类型（漏测补测/整圈重测/原点联测/弧中补测/编号核查）、
  轮次、标志或基准点、方位角；
- **SVG**：虚线圆为平均高程基准，折线为有效弧段沉降（下沉向外），灰虚线为阻断弧，
  空心红菱形为漏测，橙/红点为超限点，橙色箭头为整体倾斜方向；
- **复算 JSON**：原始读数 id、各项改正量（原点/温度/液位）、逐点沉降与模型/残差/谐波、
  逐弧段邻点差及阻断原因、全部 issue（含超限量值与允许值），可据其完全复算。
