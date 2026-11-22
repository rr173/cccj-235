# agrimosaic —— 共原点航扫格网的离线入仓与矩形窗口提取

只处理**共享原点、轴向和整数坐标系**的格网批次：像元边长是基本单元的
2 的非负整数次幂（1, 2, 4, …）。所有运算基于整数坐标的半开矩形
`[x0,x1) × [y0,y1)` 几何，**不做投影、不做插值、不逐基本单元栅格化**。

纯 Python 标准库实现，建议一律以隔离模式运行：

```bash
python3 -I run_tests.py                      # 63 项测试
python3 -I agrimosaic/__main__.py build   --config <config.json> --out <pkg_dir>
python3 -I agrimosaic/__main__.py extract --package <pkg_dir> \
        --x0 0 --y0 0 --x1 8 --y1 8 --target-size 2 [--output result.json]
python3 -I agrimosaic/__main__.py season  --manifest <season.json> [--output ledger.json]
```

## 1. 数据契约

### 构建配置（JSON）

```json
{
  "value_domain": {"reflectance_min": 0.0, "reflectance_max": 1.0,
                   "quality_min": 0, "quality_max": 100},
  "batches": [
    {"batch_id": "B1",
     "acquired_at": "2026-01-01T00:00:00+00:00",
     "cell_size": 1,
     "coverage": {"x0": 0, "y0": 0, "x1": 8, "y1": 4},
     "source_file": "../data/b1_fine.jsonl"}
  ]
}
```

- `cell_size` 必须是 2 的非负整数次幂；`coverage` 必须按该尺寸对齐。
- `acquired_at` 必须是带时区偏移的 ISO 8601 时刻。
- 批号全局唯一，重复批号直接拒收。

### 源格文件（JSONL，每行一格）

```json
{"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.21, "quality": 21}
{"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "state": "cloud"}
{"cell": {"x0": 4, "y0": 0, "x1": 6, "y1": 2}, "state": "missing_channel"}
```

- 源格必须是边长等于批次 `cell_size`、按格网对齐、落在批次覆盖矩形内的半开方格。
- `state` 缺省或为 `valid` 时，反射值与质量值**同时在认证值域内**才算整对有效；
  缺通道、非有限数或越出值域一律归为 `missing_channel` 不可用记录。
- `cloud` / `missing_channel` 记录不得同时携带认证整对，否则视为语义矛盾拒收。
- **拒收条件（与到达顺序无关，不用顺序裁决）**：
  同批次对同一物理源格重复定义、坐标越界、格网未对齐、边长与批次不符、
  非 2 的幂、裸时间戳、批号重复、源文件无法打开。拒收时退出码 2，不留半成品目录。

### 产物（一个目录，标准格网镶嵌表）

| 文件 | 内容 |
| --- | --- |
| `meta.json` | 版本、认证值域、批次覆盖矩形、记录统计 |
| `records.jsonl` | 全部源格记录（按 y,x 排序，行号即索引叶子 id） |
| `rtree.json` | STR 打包静态 R 树（纯矩形 bbox，无栅格化） |

## 2. 裁决规则

对目标分辨率下的每个目标格：

1. R 树取出与它正面积相交的全部候选源格；
2. 用候选源格的**边线**把目标格切成互不重叠的小片段，片段内候选集合恒定
   （片段数只与边线数有关，与物理面积无关，极粗格不会被展开）；
3. 每个片段从**有效**候选中选赢家，偏好依次为
   **更细分辨率 → 较新采集时刻 → 字典序较小批号**；
4. 无有效候选的片段按下列优先级归因（多种失败并存取最高者）：
   - `unavailable_cloud`（显式云遮，最高）
   - `unavailable_missing_channel`（缺一个通道/值域不可用）
   - `declared_no_record`（处在某批次覆盖内却没有任何记录，即文件缺页）
   - `no_declared_coverage`（没有任何批次声明覆盖，最低）
5. 相同赢家或相同原因的相邻片段合并为矩形，面积逐项可对平。

目标格状态：

| 状态 | 条件 | 输出 |
| --- | --- | --- |
| `homogeneous` | 整个范围都有赢家且所有赢家是**同一物理源格** | 给原始反射值/质量值与出处 |
| `fully_mixed` | 全范围有赢家，但赢家不止一个物理源格 | 各赢家分区，**不平均、不虚构值** |
| `partial_coverage` | 既有赢家片段也有缺口片段 | 赢家分区 + 缺口分类分区 |
| `empty` | 没有任何赢家片段 | 仅缺口分类 |

## 3. 小尺度逐格核对证据

`demo/build_demo_data.py` 生成三批夹具（B1 size 1、B2 size 2、B3 size 4），
窗口 `(0,0)-(8,8)`、目标格边长 2，共 16 格。构建与提取：

```bash
python3 -I demo/build_demo_data.py
python3 -I agrimosaic/__main__.py build --config demo/configs/config_a.json --out demo/output/pkg_a
python3 -I agrimosaic/__main__.py extract --package demo/output/pkg_a \
        --x0 0 --y0 0 --x1 8 --y1 8 --target-size 2 --output demo/output/window_a.json
```

人工核对表（节选，完整 16 格见 `window_a.json`，总面积 48+16=64 对平）：

| 目标格 | 情形 | 期望结果 |
| --- | --- | --- |
| (0,0,2,2) | B1 四个有效细格铺满，B2/B3 更粗 | `fully_mixed`，4 个 B1 赢家各面积 1，不给混合值 |
| (2,0,4,2) | B1 两格分别缺通道、云遮；B2 整格有效 | `homogeneous`，B2 原始值 (0.21,21) |
| (4,0,6,2) | B2 size2 与 B3 size4 均有效 | `homogeneous`，更细的 B2 胜 (0.22,22) |
| (0,2,2,4) | B2 云遮；B3 粗格有效覆盖 | `homogeneous`，B3 (0.30,30)，云遮不参与胜出 |
| (2,2,4,4) | B2 声明覆盖但缺页；B3 有效 | `homogeneous`，B3 (0.30,30) |
| (4,4)–(6,8) | B2/B3 均声明覆盖但都缺记录 | `empty`，原因 `declared_no_record`，各面积 4 |

## 4. 极粗格被单个小格盖住的窗口案例

`config_b.json` 含一个边长 **2²⁰ = 1048576** 的粗格 `COARSE` 和一个
基本单元小格 `FINE@(1,1,2,2)`，另有一个**空 JSONL 文件**的批次 `EMPTY`：

```bash
python3 -I agrimosaic/__main__.py build --config demo/configs/config_b.json --out demo/output/pkg_b
python3 -I agrimosaic/__main__.py extract --package demo/output/pkg_b \
        --x0 0 --y0 0 --x1 4 --y1 4 --target-size 2 --output demo/output/window_b_coarse.json
```

目标格 `(0,0,2,2)` 输出 `fully_mixed`：`FINE` 凭更细分辨率赢得面积 1，
`COARSE` 赢得面积 3；`fragment_count` 仅 4，与 2²⁰ 的物理面积无关，
证明粗覆盖从未被逐基本单元展开。

文件缺页与无声明的区别（同包另两个窗口）：

- 窗口落在 `EMPTY` 批次声明矩形内、该批次 JSONL 为空 →
  `empty` + `declared_no_record`（见 `window_b_empty.json`）；
- 窗口落在所有覆盖矩形之外 →
  `empty` + `no_declared_coverage`（见 `window_b_nodecl.json`）。

四类失败原因同框对照与优先级由 `demo/build_gap_evidence.py` 生成
（`window_gap_classified.json`）：云遮 1 + 缺通道 1 + 声明内缺记录 13，
外加有效赢家 1，面积 1+1+13+1=16 对平，缺口按云遮 → 缺通道 → 缺记录排序。

## 5. 季末离线作业：逐地块复测资格台账（season）

`season` 回答调度员三个问题：已经确认达到连续低值条件的面积有多少；
缺失或不够可靠的观测**是否还可能**改变地块的复测资格；已确认面积是在哪一轮
首次达标的。作业只读镶嵌包，不修改输入，也不产出地图界面或逐位置输出。

### 季末清单（JSON）

```json
{
  "plots": [
    {"plot_id": "FIELD-A", "bounds": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}}
  ],
  "rounds": [
    {"date": "2026-05-01", "package": "pkg/r1.mosaic"},
    {"date": "2026-05-10", "package": null},
    {"date": "2026-05-20", "package": "pkg/r3.mosaic"}
  ],
  "criteria": {
    "reflectance_cap": 0.3,
    "min_quality": 10,
    "min_consecutive_rounds": 2,
    "min_span_days": 0,
    "area_threshold_bp": 2500
  }
}
```

- 地块沿用整数坐标系半开矩形，边界可以切过源格；地块编号唯一，地块之间
  允许重叠且各算各的。
- 轮次日期必须按日**严格递增**（以清单为准；包内采集时刻只用于既有镶嵌裁决）；
  `package` 为 `null` 表示该轮未取得，非空路径无法读取时整个作业失败
  （退出码 2），不会被当成一次未观测后继续出表。
- 门槛：`min_consecutive_rounds`(K) 为正整数、`min_span_days`(D) 为非负整数、
  `area_threshold_bp` 为 1..10000 的整数基点；清单任何字段非法同样拒收。

### 单轮与跨轮裁决

对地块内同一位置，每轮先按第 2 节既有规则选赢家，**再**应用本作业的质量门槛：

| 情形 | 该轮状态 |
| --- | --- |
| 无赢家（云遮/缺通道/缺页/无声明）、轮次缺包 | `unknown` |
| 赢家质量低于 `min_quality` | `unknown`（绝不改选排名靠后的源格） |
| 有赢家且质量达标、反射 ≤ `reflectance_cap` | `low` |
| 有赢家且质量达标、反射 > 上限 | `not_low` |

任意一段**相邻**轮次全为低值、轮数达到 K 且末轮日期减首轮日期至少为 D，
该片即确认达标；跨过未知轮次拼接两个低值段不算确认，但允许从下一轮重新
开始。可能达标面积上界 = 把所有未知乐观视为低值后会达标的面积，包含已确认
部分。已确认的每片面积只记在它**首次达标**的末轮日期，之后再次达标不重复记账。

### 台账字段与资格判定

每块地输出 `total_area`、`confirmed_area`（下界）、
`possible_area_upper_bound`（上界）、两者的基点占比、
`first_confirmed_area_by_date`（按日期归集的首次确认面积）与 `qualification`：

- `retest`：下界占比达到门槛 → 列入复测；
- `excluded`：上界占比仍不到门槛 → 不列入；
- `pending`：两者之间 → 缺测/不可靠观测仍可能改变资格。

面积都是整数基本单元平方数；门槛比较用整数交叉相乘
（`part × 10000 ≥ 阈值 × total`），门槛处不会因四舍五入换档。

### 几何复杂度保证

多轮叠加采用**增量真实矩形交集**：每个原子只与真正和它正面积相交的分区矩形
求交，互不相交的局部边界不会被拉通成横纵边线的全笛卡尔积；全程不枚举基本
单元，极粗格（如边长 2²⁰）上只有零星细格修正时，原子数仍是与物理面积无关的
小常数，面积总数精确对平。可复跑证据：

```bash
python3 -I demo/build_season_evidence.py
# 小地块：低值移位 + 缺轮（pending）、移位后末轮首次确认（retest）、
# 门槛 2500bp 复测而 2501bp 不列入；极大地块：2^40 面积上两个细格修正精确扣减
```

## 6. 模块布局

```
agrimosaic/
  geometry.py  整数半开矩形：交集、边线切割、相邻合并（面积守恒）
  rtree.py     STR 打包静态 R 树（标准库）
  model.py     批次/源格模型、认证值域、输入校验、赢家排序键
  store.py     离线入仓、原子落盘、读包
  extract.py   窗口/目标格提取、赢家裁决、缺口归因、面积对平
  season.py    季末清单校验、逐轮裁决、多轮增量叠加、复测资格台账
  cli.py       build / extract / season 命令行
tests/         63 项 unittest（几何、裁决、归因、拒收、季末台账、CLI 端到端）
demo/          夹具生成脚本与证据输出
```
