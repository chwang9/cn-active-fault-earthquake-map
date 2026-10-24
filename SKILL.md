---
name: cn-active-fault-earthquake-map
version: "1.4.0"
display_name: "城市活动断层与历史地震可视化"
display_name_en: "CN City Active Fault & Earthquake Map"
description_zh: >-
  输入一个中国城市名，调用中国活断层数据库 CAFDv2023 提取活动断层，
  地震目录双源融合：近期事件取国家地震台网 CENC（正式测定、中文地名），
  历史/区域完整性由 USGS FDSNWS 兜底（同事件去重、优先保留 CENC）；
  叠加阿里 DataV.GeoAtlas 三级行政边界（地名解析亦用同一索引、单一数据源），
  输出 Leaflet + 高德卫星影像的交互式 HTML 地图。
  断层与地震默认都只取**市域内**部分（`--scope=radius` 可切回圆形半径）。
  左侧栏三个 tab：概览（统计概览 + 筛选）、地震目录（默认时间倒序）、断层清单；
  右侧面板不用 tab，图层 / 分布 / 图例三段常驻展开；地图上标注最大与最近地震。
  免 API Key，零第三方依赖，支持离线回退。
description_en: >-
  Given a Chinese city name, extract active faults from the CAFDv2023 China Active
  Fault Database and earthquakes from a dual-source catalogue (recent events from
  the national CENC network with Chinese place names, historical/regional coverage
  from USGS FDSNWS; duplicates merged, CENC preferred), overlay the administrative
  boundary (Aliyun DataV.GeoAtlas areas_v3, falling back to GADM 4.1; the same
  index is also the single geocoding source), then render an interactive Leaflet +
  AMap satellite-imagery HTML map.
  Faults and earthquakes are clipped to the city boundary (crossing faults kept);
  earthquakes are counted inside the boundary by default. The left panel has three
  tabs (overview / quake catalogue / fault list); the right panel is a single
  always-expanded card (layers / distribution / legend) with the largest and most
  recent earthquakes annotated on the map. No API key, no third-party dependencies,
  offline fallback supported.
agent_created: true
argument-hint: "<城市名> [输出目录] [--province 省名] [--radius 200] [--min-mag 3.0] [--scope city|radius] [--standalone] [--offline] [--no-boundary]"
---

# 城市活动断层与历史地震可视化

给定一个中国城市，输出一张**可交互的 HTML 地图**：高德卫星影像底图上叠加
该城市周边的**活动断层**（CAFDv2023）、**历史 M≥3 级地震**（USGS FDSNWS）
与**城市行政边界**（阿里 DataV.GeoAtlas 三级行政边界）。

界面结构：

- **左侧栏三个 tab**，一次只呈现一件事：
  1. **概览** —— 统计概览（地震数 / 最大震级 / 最深震源 / 断层条数 / 统计范围 /
     断层范围 / 时间跨度 / 边界来源）+ 地震筛选器（震级上下限、年代区间、
     震级档位芯片、快捷筛选、命中统计）
  2. **地震目录** —— 完整目录表，**默认时间倒序**，点表头排序、点行定位到地图，支持搜索
  3. **断层清单** —— 断层清单（名称 / 活动方式 / 最新活动年代），支持搜索、点行定位、
     hover 高亮地图对应断层
- **右侧面板不用 tab**，「图层 / 分布 / 图例」三段**常驻展开**
- **地图上标注「最大地震」与「最近地震」**（虚线环 + 永久胶囊标签）

全部数据源**免 API Key**，**零第三方依赖**（仅 Python 标准库）。

## 何时使用

- 「把 XX 市的历史地震画在地图上」「XX 附近有哪些断层」
- 「生成地震分布图 / 活动断层图 / 地质构造图」
- 「列出某城市历史上所有 M3 以上地震」
- 「某城市地震活动性分析 / 场地地震安全性调查的图形化初筛」
- 「在地图上画出 XX 市的行政边界范围」

**不适用**：全球尺度板块构造研究、需要精确震源机制矩张量、要求中国地震台网
正式目录精度的正式工程报告（本技能为公开数据初筛工具）。

## 输入

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| 城市名 | 是 | — | 中文名即可，如 `西安`、`唐山`、`汶川` |
| `--out` | 否 | `<城市名>_map` | 输出目录 |
| `--province` | 否 | 自动 | 省级消歧，同名城市必填（如 `--province 河北`） |
| `--radius` | 否 | `200` | 搜索半径（km） |
| `--min-mag` | 否 | `3.0` | 最小震级 |
| `--start-year` / `--end-year` | 否 | `1900` / `2026` | 时间范围 |
| `--offline` | 否 | 关 | 附本地 leaflet 副本，CDN 不可达时自动回退 |
| `--standalone` | 否 | 关 | 单文件自包含（leaflet 内联进 HTML），断网也能打开 |
| `--no-boundary` | 否 | 关 | 不加载行政边界（此时断层退回按半径裁剪） |
| `--scope` | 否 | `city` | 统计范围：`city`=只保留行政边界内；`radius`=保留半径内全部 |
| `--lon` / `--lat` | 否 | 自动 | 直接指定 WGS84 坐标，跳过地名解析 |

**同名城市必须消歧**：按「名称精确匹配 + 省级匹配 + 行政级别 + 人口」打分；
指定省份无匹配时**直接报错并列出候选**，不静默猜一个。执行后核对打印出的城市全名。

**不使用任何磁盘缓存**（v1.0.5 起，应用户要求「不要缓存」）：每次运行
实时取数（地震目录 / 断层库 / 行政边界 / 地名索引），下载中的大文件放
一次性临时目录、进程退出即删。技能目录内同样零缓存文件（含 `__pycache__`，
见 `sys.dont_write_bytecode`）。好处是数据永远最新，且「旧缓存静默遮蔽
新逻辑」这类故障模式（历史教训：belt 字段恒为空）从根上消失；
速度由「断库 16 路分块并发下载 + 四路取数并行」保证（合肥实测冷跑 83 s，
其中 ~62–82 s 是断库下载，受本机网关对 GitHub 系主机 ~140 KB/s 的
聚合限速压制，属网络上限而非代码上限）。

## 执行步骤

1. **解析城市坐标** —— 单一数据源：阿里 DataV.GeoAtlas 行政区划索引
   （覆盖全国 3238 条省/市/区县，中文名齐全，与行政边界同源）。
   不再使用 GeoNames / Open-Meteo Geocoding：实测其对中国城市主名常为拉丁
   转写（淮南→`Huainan`、厦门→`Xiamen`、温州→`Wenzhou`、宿州→`Suzhou`、
   宣城→`Xuancheng`），中文查询恒 0 命中（52 城抽样失败 5 城，9.6%）。
   DataV 索引坐标是 **GCJ-02**，须做 GCJ-02 → WGS84 逆变换才能用于 USGS 查询；
   展示名做 norm_prov 归一化（「安徽省·合肥市」→「安徽·合肥」）。

2. **四路取数并行** —— 地名解析完成后，USGS 地震目录 / 国家地震台网 CENC
   近期目录 / 活动断层库 / 行政边界四路互不依赖，`ThreadPoolExecutor(4)`
   并发执行，墙钟取最慢一路（实测合肥：断库下载 ~62–82 s 为主干，
   其余三路被完全覆盖）。地震目录：以城市为中心换算矩形 bbox 请求
   USGS FDSNWS，先用 `/count` 预估总量，超 20000 条时按年份分批拉取，
   避免静默丢事件；再按 haversine 严格筛出圆形范围（bbox 只是粗筛）。
   CENC 近期目录与 USGS 按「时间±120 s 且位置<50 km」去重，优先保留 CENC。

3. **拉取并裁剪活动断层** —— 下载 `CN-faults.gmt`（GMT 中国社区
   `china-geospatial-data` 仓库分发，内容为中国活断层数据库 CAFDv2023），
   **16 路分块并发下载**（实测 8 路 92 s → 16 路 58 s；单块失败重试一次；
   主镜像失败自动换下一个镜像，全部失败才报错退出），解析 GMT 段式文本后
   按 bbox 裁剪。

4. **坐标转换** —— 高德瓦片是 **GCJ-02**，地震与断层数据是 **WGS84**，
   不转换会整体偏移 400–600 m。地震点、断层线、城市中心写入前统一
   WGS84 → GCJ-02 偏移。

5. **查找行政边界** —— 主源阿里 DataV.GeoAtlas `areas_v3`（免 Key，省/市/县三级）。
   名称匹配只做**双向精确匹配 + 双向剥后缀匹配**，不做前缀包含（否则「西安」
   会命中「西安新城区」）。**DataV 坐标本身就是 GCJ-02**，与底图同坐标系，
   不再偏移；GADM 4.1 保留为回退源（它是 WGS84，需逐点转换）——
   边界 dict 带 `crs` 字段，`build_map` 按它决定是否转换。

6. **统计范围收敛到行政边界** —— 半径查询只是取数手段，页面回答的是
   「这个城市发生过什么地震」。逐点射线法只保留边界内事件。
   **合肥实测**：半径 200 km 内 20 次、最大震级 6.4（那是 131 km 外六安霍山
   1917 年地震）；市域内只有 2 次（2024-09 肥东 M4.2 / M4.9）。
   **退化保护**：只有市域内 0 条才退回半径结果，并在页面写明原因。

7. **断层按行政边界裁剪** —— 多边形精确裁剪（不是 bbox 裁剪）：对折线每段
   求它与边界各边的交点参数 t，按 t 排序后用「区间中点是否落在多边形内」
   判定归属。**跨界断层保留**（擦过边界即保留，只裁掉越界部分），
   **完全在市域外的剔除**。实现上用均匀网格空间索引 `_EdgeGrid`，
   求交量从 42 万次降到约 1 万次（耗时 0.33 s → 0.05 s）。

8. **压缩几何并生成 HTML** —— Leaflet 1.9.4 + 高德卫星瓦片（影像 `style=6`
   / 注记 `style=8`）。断层线「深色细描边 + 暖色柔光晕 + 亮红主线」三层绘制；
   地震点按震级分色，自绘发光标记；坐标降 4 位小数 + Douglas-Peucker 抽稀；
   目录表分页（排序作用于全量索引）。

## 输出

```
<输出目录>/
├── <城市名>_断层地震分布图.html   # 单文件，含全部数据，可直接双击打开
├── data.json                     # 结构化数据：stats / quakes / faults / boundary
├── leaflet.js / leaflet.css       # 仅 --offline 时生成
```

右侧地图区浮层：

- **右上角单面板**（`.lctrl`）：**三段常驻展开** ——
  【图层】5 个 switch + 全选/全不选、【分布】震级分布表 + 年代直方图、
  【图例】行政边界 / 活动断层 / 最大地震 / 最近地震 / 震级分色 / 城市中心。
  取消 tab 的原因：用户不必逐个点开去找信息，一屏看全
- **右下角数据源角标**（`.mfoot`）
- **地图上标注最大地震与最近地震**（虚线环 + 永久胶囊标签），置于独立
  `specialLayer`，**不随震级筛选隐藏**
- **数据导出** —— 地震 / 断层各出 **GeoJSON**（WGS84，CRS84，可直接导入
  QGIS/ArcGIS）与 **CSV**（UTF-8 BOM + CRLF，Excel 打开中文不乱码）。
  地震导出只含当前筛选结果，按钮实时显示条数；断层导出全部
- **搜索与快捷键** —— 地震目录可搜日期/震级/地点/事件号，断层清单可搜名称；
  `1`/`2`/`3` 切侧栏 tab、`/` 聚焦搜索框、`Esc` 关弹窗

**断层的「活动方式」与「最新活动年代」已做通俗化**：

- 活动方式：`正断` → 拉张型、`逆断` → 挤压型、`走滑` → 水平错动型
- 最新活动年代：`Qh` → 全新世（约 1 万年前至今）、`Qp3` → 晚更新世
  （约 1.3 万–21 万年前）、`Pre-Q` → 前第四纪（260 万年前，属古老基岩断层）
  —— 越新说明地壳活动越频繁、潜在危险性越高

`data.json` 同时保留 `center_wgs84` 与 `center_gcj02`，便于二次分析。

## 调用示例

```bash
# 最简：默认 200 km 半径、M≥3
python scripts/build_map.py 西安 --out ./xi_an

# 同名城市消歧 + 更大范围 + 只看 M≥4
python scripts/build_map.py 唐山 --province 河北 --radius 300 --min-mag 4.0 --out ./tangshan

# 指定时间范围
python scripts/build_map.py 汶川 --province 四川 --radius 150 --start-year 1950 --out ./wenchuan

# 产出可离线浏览版本（CDN 挂了也能看）
python scripts/build_map.py 西安 --offline --out ./xi_an

# 不画行政边界
python scripts/build_map.py 西安 --no-boundary --out ./xi_an

# 回退到「半径内全部地震」的旧口径
python scripts/build_map.py 合肥 --province 安徽 --scope radius --out ./hefei_radius

# 索引中查不到的县直接给坐标
python scripts/build_map.py 汶川 --province 四川 --lon 103.59 --lat 31.48 --out ./wenchuan
```

用户最可能只给一个城市名，默认参数即可满足：
> 「把西安的断层和历史地震画在地图上」→ `python scripts/build_map.py 西安 --out ./xi_an`

## 数据源与精度说明

| 数据 | 来源 | 说明 |
|---|---|---|
| 活动断层 | CAFDv2023（中国活断层数据库） | 全国 10534 条，7.9 MB。**线要素为地表破裂迹线位置**，不表示破裂深度或分段。逐条保留 `ref`（参考文献，断库实测 100% 有值）与 `zone`（所属断裂带，18% 有值），可溯源。 |
| 历史地震 | **双源融合**：近期取 **国家地震台网 CENC**（正式测定，Wolfx 镜像转发，中文地名＋震中烈度）＋ 历史/区域完整性由 **USGS FDSNWS**（ISC-GEM）兜底 | 同事件（时间±120 s 且位置<50 km）去重并优先保留 CENC；CENC 仅实时/近期，历史缺口由 USGS 补。逐条标注**震级量规**（mb/mw/ml/ms…）与审定状态，并默认剔除爆破/矿震等非天然事件 |
| 行政边界 | **阿里 DataV.GeoAtlas** `areas_v3` | 全国 3238 条（省 34 / 市 363 / 县 2840）。**数据源为高德开放平台，仅供学习交流，不可再分发**；仅供范围示意，非法定界线。回退源 GADM 4.1。 |
| 底图 | 高德卫星影像 + 影像注记 | 商业瓦片服务，仅供浏览参考。 |
| 地名 | **阿里 DataV.GeoAtlas 行政区划索引** | 与行政边界同源、中文名齐全；GCJ-02 坐标须逆变换为 WGS84。 |
| 地图库 | Leaflet 1.9.4（CDN + 本地回退） | |

**重要提示 —— 地震目录完整性有客观缺口。** ISC-GEM 合并目录对中国的仪器记录
约始于 1960 年代，1970 年后 M≥3 基本完整；此前只有大震级事件被系统编目。
已验证这不是查询参数问题（`includeallorigins=true&includeallmagnitudes=true`
与默认查询事件数相同，160 vs 160），上限来自数据源本身。脚本会在面板顶部给出
完整性提示（sparse / modern / full）。做完整性分析时应结合《中国地震年鉴》
或中国地震台网正式目录交叉验证，**不要把「历史地震总数」当作完整统计**。

**震级不是同一把尺子量出来的（v1.1.0）。** USGS 目录里 mb / mw / ml / ms /
mb_Lg 混用 —— 实测合肥半径 200 km 的 22 条里就有 5 种量规（mb 11、mw 5、
ml 4、ms 1、mb_Lg 1），同一次地震用不同量规可差 0.3~0.7 级。因此：
① 每条地震都保留量规并在弹窗显示「震级口径」；② 概览面板给出本次涉及
的量规及条数；③ 中国台网（CENC）**不随事件发布量规代码**，一律显示
「中国台网测定（未标注量规）」——把未知标成已知属于数据造假。
含自动测定（`status=automatic`）事件时概览另有提示，其定位与震级误差更大。

**非天然事件默认剔除。** USGS 是全球目录，`type` 字段含 quarry blast /
mining explosion 等非构造事件。实测中国区域 M≥3 取样（合肥 22 / 唐山 161 /
淮南 17 / 阜新 65 条）暂全为天然地震，但这是**当前的区域取样**、不能当作
放行依据，故默认只保留 `type=earthquake`，且剔除条数会打印到终端
（静默剔除等同于静默丢数据）。

## 关键实现要点（踩过的坑）

### 数据与坐标

- **一律用 GET** —— 本机网关拦截 POST 并返回 504。
- **Windows 的 `time.gmtime` 不支持负时间戳**（1900 年前地震抛
  `OSError: Invalid argument`），用 `datetime` + `timedelta`。
- **`CN-faults.gmt` 是 GMT 段式文本不是 JSON** —— 属性行 `# @D…`，字段序
  `FZN_Ch|FZN_En|FN_Ch|FN_En|FSN_Ch|FSN_En|Fea_Ch|Fea_En|AGE|AGE_Des|RefE`：
  断裂带名 0、断裂名 2、性质 6、时代 8、参考文献 10。属性行有两种书写
  （`# @D|` 前导空 / `# @D名称|`），但字段索引一致。
  实测填充率（前 541 段）：带名 18% / 断裂名 69% / 性质 80% /
  年代 100% / **参考文献 100%**。
  **不要凭缓存里见到的空值下结论**：v3.6.0 曾据「带名恒为空」把该字段下线，
  实为解析 pickle 缓存 key 只含源文件 size+mtime 造成的误判，v1.1.0 已复测纠正。
  断裂名**只能**取自 index 2，不得回退到 index 0（同带不同断裂会被错误合并）。
- **裁剪断层时不要逐字段重建 dict** —— `clip_faults` 曾手写
  `{name, en_name, kind, age, coords}`，每新增一个属性就漏一次，
  属性在裁剪后**静默消失**而图照出、不报错。v1.1.0 改为 `dict(f)` 复制后覆写。
- **GADM 的 `NL_NAME_1` 是「繁体|简体」竖线格式**（`陝西|陕西`），
  比较省级名必须取竖线**后**一段再归一化。
- **行政区名打分函数不能有恒真条件** —— 早期 `stripped == want`（`stripped`
  就来自 `want`）恒真，任意候选都拿 90 分，按遍历顺序返回了「安康市」而非
  「西安市」。正确写法是双方各自 strip 后再比较。
- **不要剥「市」后缀做地名归一化** —— `西安市`→`西安` 会与 `西安新城区` 混淆；
  行政级别另用 `TYPE_2`（`Dìjíshì` / `Shìxiáqū` / `Xiàn`）判定。
- **边界匹配必须用原始输入名** `args.city`，不能用「省·市」拼接展示名。
- **换行政边界数据源时坐标系必须重新确认** —— DataV 本身就是 GCJ-02，
  再转一次会二次偏移 400–600 m。判据用**共边界接缝**：唐山与承德相邻，
  承德 1542 个顶点里落在唐山边界 20 m 内的占 9%；误做二次偏移会崩到接近 0。
- **DataV 省级过滤要沿 parent 链上溯** —— 县级的直接 parent 是地级市 adcode
  （汶川县 → 阿坝州 → 四川省），只比对直接 parent 会让县级候选被整体过滤，
  表现是**静默回退 GADM**（不报错，只是边界换了源）。
- **索引条目没有 `center` 键，只有 `lng`/`lat`** —— 按 `center` 取坐标会让
  所有候选距离恒为 `1e9`，坐标决胜形同虚设。
- **GCJ-02 → WGS84 逆变换必须每轮从原始 GCJ 重算** —— 写成累积形式偏移会
  累加到公里级。正确形式 `w = gcj - f(w)`，迭代 10 轮，往返误差 0.0000 m。

### 裁剪与几何

- **折线裁剪必须按连通性分段** —— 逐段求交后用单缓冲拼接，遇到「先出界、
  再入界」的多边形会把两段**跨空隙直连**成一条不存在的断层。改为按区间
  连通性跟踪（同段相邻区间共享端点，或跨段且上一段保持内部至段末），
  并在整段完全在界外时显式断开。合成双矩形用例：修复前跨间隙连线段 > 0，
  修复后 runs = 2、跨间隙连线段 = 0。
- **空间索引提速的前提是它不改变结果** —— `_EdgeGrid` 必须用**合成凹多边形**
  做「网格路径 vs 逐边扫描路径」逐点等价性对照。
- **射线法判定前可先做逐环 bbox 剪枝** —— 纯剪枝、不改判定结果，
  `_point_in_rings` tottime 0.100 s → 0.079 s。
- **边界环只取外环会丢掉内环（洞）** —— 洞里的点会被判为在边界内，
  `_iter_rings` 必须返回全部环。
- **地震去重要用指纹** —— 原逻辑只用 `feat.get("id")`，无 id 的要素全被判为
  重复；改用 `time + lon + lat + mag`。
- **断层名不得用断裂带名回填** —— 带名比断层名更粗，8995 条属性行里
  7073 条两者不同，回填会**错误合并**不同断层。

### 前端与 CSS

- **`L.tooltip(options, content)` 第二参数是「图层」不是文本** —— 传字符串会抛
  `Cannot use 'in' operator`，且**中断整个渲染循环**（地图全空）。
  正确写法 `.setContent(text)`。
- **地图初始化必须幂等** —— CDN script 与本地回退 script 都可能触发启动，
  需统一 `__mapInited` 守卫，否则出现「L 已定义但地图空白」。
- **给 tab 面板设 `display` 必须带 `.on` 限定** —— `#qsec{display:flex}` 权重
  (1,0,0) 压过 `.pane{display:none}` (0,1,0)，未激活面板照样显示
  （概览与目录同屏）。正确写法 `#qsec.on{display:flex}`。已加 CSS 扫描型断言。
- **CSS 动画值优先于普通声明，即使选择器权重更高** —— `.qkhl .halo` 的
  `opacity` 被无限循环动画压过，必须 `!important` + `animation-play-state:paused`。
- **`getComputedStyle` 在 transition 未走完时返回旧值** —— 验证样式要等动画结束。
- **flex 容器里不要塞需要独占一行的兄弟节点** —— 直方图轴标签行塞进柱容器
  会被挤到 74px 宽，标签叠成「194019802020」。修法：柱子包进 `.hbars`，
  轴标签单独一行并用绝对定位百分比对齐到柱心。
- **快捷键键名要跟着 UI 改名同步** —— tab 改名后按 `1` 仍去点旧选择器，
  静默无反应。
- **hover 高亮的「恢复样式」必须与元素实际样式逐项一致** —— 否则 hover 一结束
  线宽回不到原值（肉眼可见的「跳一下」）。已统一为 `FAULT_MAIN_STYLE` 常量。
- **`.sw` 是 `height:0` 的横线，不能用 `box-shadow` 美化** —— 会被撑成
  有背景的方块（实测 `22×2` → `26×25`）。用 `height:4px` +
  `background-clip:content-box` + `padding-top:2px`。
- **图例不是装饰，必须与地图实现同源对齐** —— v3.6.0 查出 4 处不符
  （行政边界色/线型、断层三层叠绘、最大最近地震实心点 vs 虚线环、
  城市中心缺白边）。图例与地图是两套渲染，各写各的就会「说谎」。
- **窄屏浮层会互相压叠** —— 媒体查询查视口高度不靠谱，挤不挤取决于地图区
  高度，改用 JS 按 `stage.clientHeight` 判断；重叠检测脚本要排除父子容器。
- **开关状态别用 `className.indexOf('on')`** —— `'on'` 与 `'off'` 语义相反
  却只差一个字符，用显式 `!==`。
- **筛选器与目录表的联动必须靠 `data-i`** —— 漏写时 `parseInt(null)` 得 `NaN`，
  所有表格行显隐都会错，但地图 marker 仍正常，只看地图会以为功能没问题。
- **快捷筛选按钮必须先判断数据是否支持** —— 最大震级不足 6 级时按钮
  `disabled` 并在 `title` 说明原因，否则「筛选后命中 0 条」且无解释。
- **CSS 里有 `.fstat` 样式不等于 DOM 里有 `#fstat` 元素** —— 只写样式漏建
  元素时 `getElementById` 返回 null，页面不报错、统计条永不显示。
- **排序必须作用于全量索引** —— 不能对已渲染的 DOM 行排序；筛选后表格要
  **整体重渲染**。
- **dangling else** —— `for` 后紧跟 `if/else` 时 else 与内层 if 配对，
  统计会被最后一个未通过项覆盖。凡 `for` 后紧跟 `if/else` 一律加花括号。

### 工程与无缓存

- **v1.0.5 起整体移除磁盘缓存**（应用户要求「不要缓存」）：`cache_load` /
  `cache_save` / `cache_key_path` / `CACHE_SCHEMA` / `parse_faults_cached` /
  用户缓存目录全部删除，`--no-cache` / `--cache-dir` / `--cache-ttl` 三个
  开关随之移除。每次运行实时取数，大文件走一次性临时目录（`TMP_DIR`，
  atexit 清理）。
- **「字段恒为空」先怀疑取数链路，而不是怀疑源数据或解析逻辑** ——
  历史事故（v3.6.0）：belt 字段恒为空，根因是断库 pickle 缓存的 key
  只含源文件 `(size, mtime)`、不含解析逻辑版本，旧缓存静默遮蔽新字段。
  缓存移除后该类故障模式消失；`TestV150NoCacheAtAll` 守住不回归。
- **大文件下载用 16 路分块并发，且分块失败要重试一次** —— 实测
  （CN-faults.gmt 7.9 MB，ghproxy.net）：单连接基本拉不动；8 路 92 s；
  16 路 58 s（~133 KB/s，接近网关聚合上限）；32 路反而不稳（3/32 块超时）。
- **四路取数并行，墙钟取最大值** —— 地名解析完成后 USGS / CENC / 断库 /
  边界互不依赖，`ThreadPoolExecutor(4)` 并发；串行改并行后 USGS/CENC/边界
  被断库下载完全覆盖，不占墙钟。
- **提速不能只看单点计时，要看端到端** —— 合肥无缓存冷跑 83 s，
  其中断库下载 ~62–82 s、其余全部步骤合计 < 1 s。瓶颈在网关对
  GitHub 系主机的限速，不在代码。
- **静默的 `except Exception: return []` 会伪装成功** —— 任何静默兜底分支都要
  能被观测到（打印 `[warn]` 或计数），否则永远查不出「优化没生效」。
- **HTML 模板外置到 `scripts/template.html` 后，自检断言必须跟着改指向** ——
  仍查 `build_map.py` 的断言会变成永真断言，静默失效却报告「通过」。
  已加 `test_build_map_reads_external_template` 防再出现内嵌副本。
- **自检断言取源码片段时窗口不能太宽** —— 会一路扫到后面的 `__BOUNDARY__`，
  把行政边界的 `rings` 误当成另一类要素的 `rings` 而误报。
- **断言要防「永真」** —— 只查键名（值改成 None 键名仍在）、只查变量名
  （去掉拷贝后名字还在）、宽泛子串（模板另有同名串）三种典型形态。
  修正办法：正则精确定位 + **注入式变异验证**（定向改坏一份副本，
  确认每条断言都会失败）。
- **数据异常时先怀疑验证脚手架** —— 两个 parser 共用同一个计数 dict、
  测试替身签名与生产函数不同步，都会产出「被测代码有问题」的假象。
  给生产函数加参数时，替身必须逐参数对齐，否则抛 `TypeError` 绕过 `except`。
- **新增探针 key 必须同时加进断言列表**，否则用例只是 print、永远空转。

## 自检

```bash
python scripts/selftest.py
```

171 项测试，覆盖坐标转换、距离计算、负时间戳、GMT 解析与字段索引、断层裁剪
（含网格 vs 逐边扫描等价性、bbox 剪枝等价性）、目录完整性判定、HTML 模板渲染
完整性、行政边界匹配（打分、竖线省级名、行政级别、射线法）、DataV 边界
（双向精确匹配、GCJ-02 声明、按 `crs` 决定偏移、共边界接缝重合率）、
统计范围口径（默认 city、市域内 0 条才回退、带洞多边形、范围说明渲染进页面）、
界面结构（三 tab、右侧无 tab 三段常驻、CSS `.on` 限定、直方图结构分离、
图例色标与地图样式对齐、图例无带类项）、筛选器三处联动、导出与坐标一致性、
缓存 schema 版本化、技能目录零缓存、地名第二数据源与逆变换往返误差、
`L.control.layers` 不得实际调用等。

**断言有效性用注入式变异验证**：定向变异逐一注入副本目录，确认每一条都会让
自检失败，防止「永真断言」。

## 变更日志

| 版本 | 变更 |
|---|---|
| **1.4.0** | **代码结构优化（测量优先，输出逐字节不变）**。先建基线再动手：AST 统计出两个巨型函数 `build_html`（**281 行 / 分支复杂度 62**）与 `main`（**286 行 / 复杂度 33**）；基准测出 CPU 侧总耗时仅 **0.36 s**，而单次运行 90 s 里约 80 s 是断库下载 —— 说明**瓶颈不在计算，瓶颈在结构与可维护性**。① **`build_html` 拆分**：五段互不相干的渲染逻辑（震级分档条 / 断裂清单 / 年代直方图 / 地震目录行 / 概览文案）拆为 `mag_rows_html` / `group_faults` / `fault_rows_html` / `year_histogram_html` / `quake_rows_html` / `scope_text` / `src_note_text` / `magscale_text` / `maxintensity_row_html` / `yearscale_warn_html` 等**纯函数**（不读全局、不依赖调用顺序、可单独单测），`build_html` 自身降到 74 行只做占位符组装、复杂度 62→9。② **`main` 拆分**为 `parse_args` / `resolve_center` / `fetch_all` / `prepare_boundary` / `clip_and_mark` / `compose_scope` / `slim_products` / `write_outputs`，自身 286→81 行、复杂度 33→9。③ **去重**：`datetime(1970,1,1)+timedelta(ms)` 在 3 处各写一遍（其中 2 处还各自新建 EPOCH 对象）→ 统一为 `ms_to_dt` / `ms_to_year` + 模块级 `EPOCH_UTC`；几何压缩的 before/after 表达式重复两遍 → 抽 `_payload_bytes`；`fetch_earthquakes` 的分批规划抽为纯函数 `plan_batches`、去重指纹抽为 `feat_fingerprint`（顺带修掉**主循环体整体多缩进 4 空格**的非 PEP8 写法）；局部常量 `PAGE` 提为 `USGS_PAGE`（分批与自适应细分两处要共用）。④ **删除无效优化**：实测 `BND_DP_EPS`（边界抽稀容差）在真实市域边界上**收益恒为 0**（合肥 1080→1080、杂多 1221→1221），而压得动的 eps 会带来 55~111 m 形变（放大到 z15 是 14~28 px 肉眼可见）；曾怀疑是「闭环首尾重合导致 DP 顶层弦退化为 0」，实测开环（对 `ring[:-1]` 做 DP）结果几乎一致（915 vs 918），**排除算法原因，是数据形状本身决定的**，故删除该常量与 `slim_rings` 里的 DP 调用（保留降精度；断裂侧的 `FAULT_DP_EPS` 实测有效 47% 压缩 / 11 m 偏移，不受影响）。⑤ **拆分暴露的死代码**：`yearscale_warn_html` 有 `stats` 形参且内部 `byy = stats.get("by_year")` 算了却没人读 → 形参一并删除。⑥ **回归保障**：拆分全程用 v1.3.0 真实产物做**逐字节 golden 对比**（合肥 125399 字节 / 杂多 171128 字节，diff 为 0）证明行为未变。⑦ **修正 4 条「跟着实现细节跑」的断言**（关键教训）：`test_quake_table_is_paged` 守 `quakes[:QTABLE_FIRST_PAGE]` 字面量、`test_empty_quake_placeholder_colspan_matches_columns` 守「同时含 `__QROWS__` 与 colspan 的同一行」、`test_warn_logic_present_and_guarded` 守 `span_days < 90` 表达式写法、`test_quake_dedup_handles_missing_id` 守 `fid = "fp:%s..."` 字面量 —— 代码一挪/一改写就失效而**产品行为毫无变化**，全部改为**行为断言**（直接调用拆出的函数验证语义）。⑧ 新增 `TestV140CodeStructure`（9 项：两函数规模上限守卫 / 渲染函数存在且可单独调用 / 负时间戳换算 / 完整性年份自洽 / 分批区间连续无重叠全覆盖 / 边界抽稀已删 / 环闭合仍有效 / `quake_stats` 复用 `completeness_of`），**214 项全过**。性能：CPU 侧 0.359→0.343 s（n=5000 时 `build_html` −17%、市域过滤 −15%），但绝对值极小，本轮收益在结构不在耗时。 |
| **1.4.0（维护）** | **QA 脚本「开关可还原」判定校准 + 黔东南州新城市验证**。① 浏览器验收（`qa_browser.py`，42 项）在黔东南州实跑发现 1 项 FAIL：`开关可还原 [sat, label]`。定位为**非产品 bug**——`afterOn` 检查本就 PASS（卫星底图/注记两个开关两次点击后 UI 的 `on` 类确实回到初始，证明开关逻辑对称、可还原），而原 `restored` 判定用「点击前/还原后的**瓦片 img 数量相等**」；sat/label 是 Leaflet 瓦片图层，关闭再打开后瓦片**异步加载**，两次 click 之间 img 尚未渲染，导致误判 FAIL。改为基于 UI 开关状态（`on` 类是否回到初始）判定后重跑 **42/42 全过**；bnd/flt 为矢量 path（同步增删），新旧判定对其等价。② 数据真实性抽查：黔东南州 1900 年以来 M≥3 共 6 次（最早 2007、最大 M5.4），其中 M5.4 坐标 (108.81, 26.68) 对应 **2015-03-30 贵州剑河 5.5 级地震**（USGS 记 M5.4），落在州境内；断层 12 段含叙浦—洪江断裂、石阡—余庆断裂、江口—黄平断裂等黔东断裂体系，真实存在。③ 已知限制（沿用 v1.0.4 的精确匹配设计）：地名归一化只做双向精确匹配以防「西安」误命中「西安市长安区」，故「黔东南州」「黔东南」都匹配失败，必须用 **完整名「黔东南苗族侗族自治州」** 才能解析（DataV 索引名即如此）。 |
| **1.3.0** | **第三轮深度测试（浏览器体验式 QA + CLI 边界）后修复的图表误导问题**。**① 年代直方图量纲混用（本轮唯一真实缺陷）**：实测那曲杂多，CENC 只覆盖 **1~2 天**（2026-10-22 巴青 M7.0 余震序列）却贡献 38 条，而历史各年每年仅 1~8 条（那是**全年**完整目录）。两者同轴绘制后 2026 年柱子异常高耸，读起来像「地震活动剧增」——**图照出、不报错**，属最典型的静默误导。修复：当台网数据集中在 90 天内且 ≥3 条时，直方图下方渲染琥珀色警示条，写明「台网近期 N 条仅覆盖 X 天，与历史各年的全年计数不是同一量纲，不代表地震活动增强（该年另有 USGS 记录 M 条）」。**② 测试方法与结论**：本轮用 Playwright（系统 Chrome）对产物做 DOM 探测 / 交互测试 / 控制台捕获，共 **41 项 UI 断言全过**（骨架、概览无 undefined、目录表 6 列、空态 colspan=6、三 tab 切换、4 个图层开关逐个切换与还原、地震点/断层点击弹窗、搜索与清空、导出按钮、零 JS 错误、零 4xx）；另跑 **11 组 CLI 边界输入**（极小/极大半径、极高/极低震级、年份倒置、无边界、省消歧、重名无消歧、境外坐标、不存在城市）——全部出图无 traceback，不存在城市优雅报错不出图。**③ 测试脚手架自身的 7 处误报已逐一证实并修正**（记录以免重犯）：`.hist`→实为 `.hbars`；`.legend`→实为 `.lg`；图层开关状态在父 `label.on` 且 `.sw` 与图例色块重名；底图开关切换的是瓦片 img 不是 path；`undefined`/`NaN` 全文匹配会命中 JS 源码里的 `typeof x !== 'undefined'` 与 `isNaN()`；`<th` 正则会匹配到 `<thead` 导致列数多算 1；中文文件名需 `urllib.parse.quote` 编码否则 404。**④ 回归**：新增 `TestV130HistogramScaleWarning`（3 项），**205 项全过**。 |
| **1.2.0** | **双源合并顺序与目录完整性结论修复 + 死数据清理**（第二轮独立审计，全部改动均有实测复现）。**① 合并后不重排序（最高危，结论被颠倒）**：`_merge_cenc_usgs` 返回 `cenc_in + kept`，把「台网近期事件」整块拼到「USGS 历史事件」之前，结果是 2026 年排在 1940 年之前。列表顺序不是 cosmetic —— `quake_stats` 依赖 `by_year` 的**插入顺序**取首尾，于是算出 `earliest=2026 / latest=2025`（最早晚于最晚），页面顶部的**目录完整性徽章结论随之颠倒**。实测那曲杂多：本应判为 `sparse`（最早 1940 年，中小地震系统性缺失），却显示 `modern`（「属仪器记录时代，完整性较好」）。修复：合并结果按时间升序返回（前端目录表/直方图顺序同时修正），且 `quake_stats` 改为 `sorted()` + min/max（**不依赖输入顺序**）。**② 完整性文案与实际年份自相矛盾**：note 是写死的三句话，`sparse` 那句写「最早记录晚于 1960 年」，但 sparse 的判定区间是 [1900,1960) —— 合肥 earliest=1917 落在该区间，页面却说它「晚于 1960 年」，结论与数据直接打架。改为按实际年份动态生成。**③ `time=0` 事件被静默丢弃**：`if q["time"]:` 用真值判断，恰好落在 1970-01-01T00:00:00.000 的事件（时间戳 0 是 falsy）被排除在 `by_year`/`earliest` 之外，属静默丢数据；改为显式 `is not None` 判空。**④ 烈度显示为「9.0 度」**：CENC 烈度是中国烈度表整数（I–XII），接口给 `"9"`，转 float 后落盘 9.0，页面显示「9.0 度」不专业；整数值改存 `int`。**⑤ 死数据清理**：`max_intensity` 算了、存了 data.json、页面从不渲染 → 概览新增「最大烈度」行（仅台网数据时渲染）；`report_time` 落盘无人读 → 删除。**⑥ 可观测性**：双源合并此前完全静默，现在打印「台网新增 N 条 / 去重剔除 USGS 重复 M 条 / 合计 K 条」（实测杂多：新增 36、剔除 10、合计 94）—— 不打印就无法验证双源是否真在工作。**⑦ 测试**：新增 `TestV120MergeOrderAndCompleteness`（8 项：合并升序/去重计数回传/earliest 与输入顺序无关/time=0 计入/文案年份一致/no-data 分支/烈度为 int/最大烈度渲染），同步 2 处因返回类型变为 tuple 的旧调用，并修正 1 条守旧文案字面量的过粗断言（`test_sparse_before_1960` 原断言 `assertIn("1960", note)`），**202 项全过**。 |
| **1.1.0** | **数据真实性与字段补全 + 目录/版本错配修复 + 死代码清理**（本轮为「高级测试 + 研发」审计）。**① 目录与版本错配（最高危）**：审计发现最新源码误落在导出副本目录 `cn-active-fault-earthquake-map_v1.0.0/`（v1.0.5），而 WorkBuddy 实际加载的无后缀主目录仍停留在 **v1.0.1**（含 10 处缓存代码）——即「改了代码但线上跑旧版」；且三个同名目录导致技能被重复加载三份。已将最新代码合并回主目录（逐文件 md5 校验）并清理两个导出残留目录（zip 备份保留）。根因是 `pack.py` 的 `SKILL_NAME` 直接取目录名，在导出目录内再打包会产出 `xxx_v1.0.0_v1.0.5` 套娃，已改为剥离 `_vX.Y.Z` 后缀。**② 地震数据**：实测合肥 22 条含 5 种量规（mb 11/mw 5/ml 4/ms 1/mb_Lg 1），新增 `mag_type` + `mag_type_cn` 逐条标注、弹窗显示「震级口径」、概览新增「震级口径」行（中文口径由 Python 落盘，不在 JS 再抄映射表）；CENC 的 `type` 语义是**审定状态**而非事件类型，与 USGS 冲突，统一到 `status`（消除双源合并歧义）；补全 CENC 独有的**震中烈度** `intensity`（`report_time` 于 v1.2.0 作为死字段移除）；新增非天然事件（爆破/矿震）过滤，**剔除量经 `drop_counts` 回收并打印**（静默剔除=静默丢数据）；`quake_stats` 增加 `mag_types`/`n_auto`/`max_intensity`。**③ 断层数据**：恢复 `ref`（参考文献，实测 100% 有值，此前被丢弃）与 `zone`（所属断裂带，18% 有值）；修 `clip_faults` 逐字段重建 dict 导致新属性裁剪后静默消失的缺陷。**④ 真实 bug**：目录表实为 6 列而 Python 空态占位写 `colspan="4"`（与模板 JS 的 6 不一致）；`fetch_earthquakes` 分批日志三个槽位里两个都填 `len(quakes)` 看不出分段取回量。**⑤ 冗余**：三处重复弹窗构造统一为 `quakePopupHtml()`；删死代码 `_PROV_SUFFIX`、`pack.read_version()`、`boundary_datav` 未使用的 `import os` 及两个死占位符 `__BLEVEL__`/`__NFAULTNAMES__`。**⑥ 测试**：新增 `TestV110DataFidelity`（14 项：量规映射/口径分布/非天然过滤与计数/CENC 语义对齐/断库字段/裁剪保字段/colspan/弹窗统一/死代码），并修正 3 条**过粗的历史断言**（`test_no_belt_feature_anywhere` 等按关键字全文扫 belt，与新增断库术语撞车 → 字段改名 `zone`；`test_fault_name_not_backfilled_from_belt` 用「赋值处后 300 字符」做窗口会误判 → 改为只看 name 赋值表达式且限定在 `parse_faults` 内），**194 项全过**。实测合肥：9 段断层中 6 段属「郯庐断裂带」（此前因无断层名只显示「未命名断裂」）。 |
| **1.0.5** | **全面优化：移除磁盘缓存 + 四路并行 + 16 路分块下载**。用户要求「不要缓存、提高速度」。① **缓存整体移除**：删除 `cache_load`/`cache_save`/`cache_key_path`/`CACHE_SCHEMA`/`parse_faults_cached`/用户缓存目录（`default_cache_dir`/`set_cache_dir`/`cpath`）与 `--no-cache`/`--cache-dir`/`--cache-ttl` 三个开关；每次运行实时取数，大文件（断库/GADM/leaflet）落一次性临时目录 `TMP_DIR`、atexit 清理；`boundary_datav` 同样去缓存，仅保留进程内索引单次复用（`_INDEX_MEMO`，不落盘）。历史故障模式（旧 pickle 静默遮蔽新字段、TTL 内看不到新地震、geocode 缓存遮蔽 DataV 更新）随之消失。② **提速**：主流程地名解析后 USGS/CENC/断库/边界四路 `ThreadPoolExecutor(4)` 并行，墙钟取最大值；断库下载 8 路 → **16 路分块并发**（实测 92 s → 58 s，32 路反而不稳故不取），单块失败重试一次，FAULT_URLS 补 gh-proxy.com 作第二镜像；修掉 `[batch]` 行重复打印。实测合肥无缓存冷跑 **83 s**（断库下载 ~62–82 s 为网络上限，其余步骤合计 < 1 s）。③ **测试**：`test_fault_parsed_cache_reuses_source_key`/`test_cache_keys_carry_schema_version`/`TestV330NoCacheInSkill` 等缓存断言全部改写为无缓存口径，新增 `test_no_disk_cache_layer`、`test_parse_faults_reads_fresh_each_time`、`test_main_flow_fetches_in_parallel`、`test_fault_download_uses_16_workers`、`test_no_cache_means_logic_never_masked` 等，删除死代码 `fault_db_path`，**180 项全过**。④ 文档：README/SKILL 数据源表、参数表、执行步骤、工程要点、性能表全部同步为无缓存口径。 |
| **1.0.4** | **地名解析改为 DataV 单一数据源**。用户要求"不使用 GeoNames，直接用阿里 DataV 对应行政区划"。`geocode_city` 重写为直接调用 `_geocode_datav`（DataV.GeoAtlas 行政区划索引，覆盖全国 3238 条省/市/区县、中文名齐全、与行政边界同源）；删除 GeoNames/Open-Meteo 主路径、`GEOCODE` 端点常量、`_geocode_raw` 与多变体并发查询（52 城抽样 9.6% 中文名零命中问题的根因消除）。展示名做 norm_prov 归一化（「安徽省·合肥市」→「安徽·合肥」），与文件名/标题一致；第四返回值保留原始省级名供边界消歧。`GeocodeError` 提示改为 DataV 口径。同步更新 README/SKILL 数据源表与执行步骤说明、`_gadm_prov` 注释中已下线的 Open-Meteo 引用；自检改写为 `test_geocode_city_uses_datav_primary`（断言 DataV 为唯一源且 GeoNames 痕迹清零）并新增函数级 `test_geocode_city_returns_wgs84_and_normalized_name`（mock DataV 喂 GCJ-02 点，验证逆变换 + 归一化 + 省级名保留），180 项全过。 |
| **1.0.3** | **地震数据源切换为「国家地震台网 CENC ＋ USGS 历史兜底」（混合方案）**。用户指出 USGS 对中国目录不完整、且英文地名不友好，经实测确认：CEIC 官网接口已迁 OSS+React、旧 `ajax/speedsearch` 全球下线、新接口未公开；可达的权威 CENC 源为 Wolfx 镜像 `cenc_eqlist.json`（直接转发中国地震台网中心「正式测定」，带中文地名）。新增 `fetch_cenc_recent()` 取 CENC 近期目录（北京时间→UTC、CGCS2000 按 WGS84 处理做 GCJ-02 偏移），主流程 `_merge_cenc_usgs()` 将其按半径裁剪并与 USGS 去重（同事件时间±120 s 且位置<50 km，优先保留 CENC），网络失败优雅降级到 USGS 并打印 `[warn]`（不静默）。地震目录新增「来源」列（台网红标/USGS 灰标、可排序），弹窗与概览/页脚标注数据来源；`quake_stats` 增加 `n_cenc/n_usgs`。新增自检 `TestCencSource`/`TestMergeCencUsgs`（用录制样本验证解析与去重），179 项全过。合肥无近期台网事件故仍显示 USGS（设计如此），那曲半径内成功落进 6 条 CENC 台网事件（含 2026-10-22 巴青 M7.0 震群，中文地名）。 |
| **1.0.2** | **地震目录增加「范围」列**。用户要求列全部地震清单后，在「地震目录」表新增「范围」列（可点击表头排序），逐条标注每条地震属「市域内」还是「半径内」，市域内醒目红、半径内弱化灰；红边界地图（`--scope radius`）现已列出研究区全部 M≥3 事件并区分内外。配套改动：`build_map.py` 给每条地震打 `in_boundary` 标记（边界 GCJ-02 同系判定）、模板表头/行/排序/占位 colspan 同步、新增自检 `test_scope_column_present`（172 项全过）。 |
| **1.0.1** | **行政边界可视化优化 + 合肥计数澄清**。**边界改为清晰红色**：原为刻意弱化的淡蓝白细虚线（用户曾要求「行政区划不明显」），现改为「白色底衬（w4/op.9）+ 红色主线 `#ff2d2d`（w2.4/op1）+ 浅红填充（op.07）」三层叠绘，保证在卫星底图上醒目可辨；图例色标与图层开关色标同步改为红色实线。同步更新 README 边界说明、模板注释与两条自检断言（`test_boundary_uses_red_stroke` / `test_legend_swatches_match_map_styles`）。**合肥 M≥3 仅 2 次澄清**：经核查非 bug，`--scope=city` 仅保留市域内事件，合肥市域（≈11477 km²，地级市全境）内 USGS/ISC-GEM 仪器记录时代确仅 2 次（2024 肥东 M4.2/M4.9）；半径 200 km 内 20 次中 18 次落在邻市（六安/安庆/蚌埠/阜阳等），坐标 WGS84→GCJ-02 与边界同系、多边形判定无误。需看区域全貌用 `--scope radius`（已生成 `hefei_radius/` 样例，最大 M6.4 为 131 km 外六安霍山 1917 年地震）。 |
| **1.0.0** | **技能重建并改名**。中文名「城市活动断层与历史地震可视化」，
英文名 CN City Active Fault & Earthquake Map，物理 ID 换为
`cn-active-fault-earthquake-map`（缓存目录与环境变量同步改名，
`CN_FAULT_SEISMIC_CACHE` 保留为兼容别名）。**文档精简**：删除逐版本演进流水账
与已下线功能的考古段落，只保留当前行为说明、数据源精度、仍然有效的实现要点
与自检清单。功能与产出格式不变（沿用原 `cn-fault-seismic-map` v3.6.0 的全部
能力：市域内统计与裁剪、三 tab 布局、右侧常驻面板、最大/最近地震标注、
导出与自检 171 项）。 |

> 沿革：原技能 `cn-fault-seismic-map`（v1.x–v3.6.0）已在此版本合并重建。
> 旧技能目录与历史包已清理，仅保留本技能。
