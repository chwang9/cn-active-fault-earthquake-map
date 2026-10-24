#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-active-fault-earthquake-map —— 城市活动断层 + 历史地震可视化

数据源（均为免 Key 公开源）：
  地震目录 : USGS FDSNWS event web service (ISC-GEM 合并目录，含中国历史地震)
              ＋ 国家地震台网 CENC 近期目录（中文地名，双源融合）
  活动断层 : 中国活断层数据库 CAFDv2023 (CAFD2023)，
             经 GMT 中国社区 china-geospatial-data 分发的 CN-faults.gmt
  底图     : 高德卫星影像 + 影像注记（GCJ-02 火星坐标）
  地名解析 : 阿里 DataV.GeoAtlas 行政区划索引（单一数据源，免 Key）

设计约定（v1.0.5 起）：
  **不落任何磁盘缓存**：每次运行全部实时取数（地震/断层/边界/索引）。
  代价是每次都要拉 7.9 MB 断库；收益是数据永远最新、无缓存目录、
  无「旧缓存静默遮蔽新逻辑」这类故障模式。速度靠两点保证：
  ① 断库 16 路分块并发下载（实测 8 路 92 s → 16 路 58 s，网关对
     GitHub 系主机聚合限速约 140 KB/s，分块并发可逼近该上限）；
  ② 地名解析完成后，USGS/CENC/断库/边界四路取数并行，墙钟取最大值。

坐标系约定：
  地震与断层数据为 WGS84；高德瓦片为 GCJ-02。
  因此所有矢量要素在写入前统一做 WGS84 -> GCJ-02 偏移，
  否则在 30-42°N / 105-125°E 区域会整体错位 400-600 m。

用法：
  python build_map.py 西安 --out xi_an
  python build_map.py 西安 --out xi_an --radius 300 --min-mag 3.0
  python build_map.py 西安 --out xi_an --province 陕西
"""
import argparse
import atexit
import datetime
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

# 不要在技能目录里生成 __pycache__（那也是缓存文件）。
# 必须在 import 本地模块**之前**设置才生效。
sys.dont_write_bytecode = True

import boundary_datav  # noqa: E402  阿里 DataV 三级行政边界（与本脚本同目录）

UA = "cn-active-fault-earthquake-map/1.0 (WorkBuddy skill; public data only)"
CTX = ssl.create_default_context()

USGS = "https://earthquake.usgs.gov/fdsnws/event/1"

# USGS 单次查询返回条数上限。原为 fetch_earthquakes 里的局部常量 `PAGE`，
# v1.4.0 提到模块级并改名：分批规划（plan_batches）与自适应细分
# （_fetch_range）两处要用同一个值，局部常量会让纯函数没法单测。
USGS_PAGE = 20000

# 断库镜像。实测（2026-10，本机网关）：ghproxy.net 16 路分块并发可达
# ~133 KB/s（7.9 MB / 58 s）；gh-proxy.com 单连接约 22 KB/s 作第二顺位；
# 直连 raw.githubusercontent.com 经本网关基本不可达，仅作最后兜底。
FAULT_URLS = [
    "https://ghproxy.net/https://raw.githubusercontent.com/gmt-china/china-geospatial-data/main/CN-faults.gmt",
    "https://gh-proxy.com/https://raw.githubusercontent.com/gmt-china/china-geospatial-data/main/CN-faults.gmt",
    "https://raw.githubusercontent.com/gmt-china/china-geospatial-data/main/CN-faults.gmt",
]

# ---- 运行期临时目录（v1.0.5：不再使用任何磁盘缓存）----
#
# 设计决策（用户明确要求「不要缓存」）：
#   ① 不使用操作系统用户缓存目录，不写 geocode/usgs/断库 pickle 等任何
#      跨运行缓存 —— 每次运行全部实时取数，数据永远最新；
#   ② 下载中的大文件（断库 7.9 MB、GADM、leaflet）放**一次性临时目录**，
#      进程退出即删（atexit），不留下任何可被下次运行误用的残留；
#   ③ 技能目录内同样零缓存文件（含 __pycache__，见下方 dont_write_bytecode）。
#
# 为什么敢去掉缓存：v3.x 的缓存是为了把 19.6 s 的全流程压到 1.5 s，
# 但缓存引入了真实故障——旧 pickle 静默遮蔽新字段（zone 恒为空）、
# 地震目录 TTL 内看不到新事件、geocode 缓存让 DataV 更新不生效。
# 现在速度改由「16 路分块并发下载 + 四路取数并行」保证（见文件头）。
TMP_DIR = tempfile.mkdtemp(prefix="cn-fault-seismic-")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)


def tpath(name):
    """一次性临时目录下的文件路径（不创建目录）。"""
    return os.path.join(TMP_DIR, name)


LEAFLET_CDN = "https://unpkg.com/leaflet@1.9.4/dist/"

# GADM 4.1 行政边界（免 Key）。ADM2=地级市，ADM3=县级。仅 DataV 不可用时回退。
GADM_URLS = {
    2: "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_CHN_2.json.zip",
    3: "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_CHN_3.json.zip",
}


# ---------------------------------------------------------------- HTTP helper
def http_get(url, timeout=60, retries=3, binary=False):
    """GET 请求。POST 在本网关会被拦截，一律用 GET。"""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
                data = resp.read()
            if not binary:
                data = data.decode("utf-8", errors="replace")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt
            print("  [warn] 请求失败 (%d/%d) %s: %s -- %ds 后重试"
                  % (attempt + 1, retries, url[:70], exc, wait), file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(wait)
    raise RuntimeError("请求最终失败: %s (%s)" % (url[:90], last))


# ------------------------------------------------------- 大文件分块并发下载
def download_parallel(url, dest, min_size=1_000_000, workers=16, timeout=300):
    """分块并发下载大文件，返回字节数（失败返回 None）。

    为什么不用单连接：本机网关对 GitHub 系主机的**长连接限速**，
    且聚合带宽也被压在 ~140 KB/s 量级。实测（2026-10，CN-faults.gmt 7.9 MB，
    ghproxy.net）：
      单连接      > 300 s（基本拉不动）
      8 路并发     92 s（~85 KB/s）
      16 路并发    58 s（~133 KB/s）   ← 当前默认
      32 路并发    36 s 但有 3/32 块超时失败，反而拿不到完整文件
    原理：每条短连接各拉一段，带宽可叠加；但并发数过高会让单块
    排队变久、触发超时，16 路是实测的收益/稳定性平衡点。

    实现要点：
      1. 先 HEAD 拿 Content-Length（拿不到就放弃，交给调用方回退）；
      2. 切 N 块并发 curl -r start-end，**单块失败立即重试一次**；
      3. 逐块校验长度，合并后校验总长；
      4. 任一步失败 → 返回 None，调用方按镜像清单回退。
    """
    curl = shutil.which("curl")
    if not curl:
        return None
    # 分片临时目录放**一次性临时区**（进程退出即删，不做任何跨运行缓存）
    tmp_dir = tempfile.mkdtemp(prefix="cn-fault-seismic-dl-", dir=TMP_DIR)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        # ---- 1. 总长度
        total = None
        try:
            out = subprocess.run(
                [curl, "-sIL", "--max-time", "30", url],
                capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines():
                if line.lower().startswith("content-length:"):
                    total = int(line.split(":", 1)[1].strip())
        except Exception:  # noqa: BLE001
            total = None
        if not total or total < min_size:
            return None

        # ---- 2. 分块并发
        chunk = max(1, (total + workers - 1) // workers)
        ranges = []
        for i in range(workers):
            s0 = i * chunk
            e0 = min(total - 1, s0 + chunk - 1)
            if s0 > e0:
                break
            ranges.append((i, s0, e0))

        def _one(item):
            i, s0, e0 = item
            part = os.path.join(tmp_dir, "p%02d" % i)
            want = e0 - s0 + 1
            # 单块失败重试一次：网关对个别连接会直接 RST（实测 16 路里
            # 偶发 1 块失败），重试一次基本都能补上；整体失败交给调用方换镜像。
            for attempt in (0, 1):
                cmd = [curl, "-sS", "--max-time", str(timeout),
                       "-r", "%d-%d" % (s0, e0), "-o", part, url]
                try:
                    r = subprocess.run(cmd, capture_output=True,
                                       timeout=timeout + 30)
                    if r.returncode == 0 and os.path.exists(part) \
                            and os.path.getsize(part) == want:
                        return True
                except Exception:  # noqa: BLE001  超时等，进重试
                    pass
                if attempt == 0:
                    print("  [warn] 分块 %d 失败，重试" % i, file=sys.stderr)
            return False

        with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
            oks = list(ex.map(_one, ranges))
        if not all(oks):
            return None

        # ---- 3. 合并 + 校验
        got = 0
        with open(dest + ".part", "wb") as out:
            for i, _, _ in ranges:
                part = os.path.join(tmp_dir, "p%02d" % i)
                with open(part, "rb") as fh:
                    data = fh.read()
                out.write(data)
                got += len(data)
        if got != total:
            os.remove(dest + ".part")
            return None
        os.replace(dest + ".part", dest)
        return got
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------- WGS84 -> GCJ-02 偏移
def _t_lat(x, y):
    r = (-100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    r += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return r


def _t_lon(x, y):
    r = (300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    r += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return r


def wgs84_to_gcj02(lon, lat):
    """WGS84 经纬度 -> GCJ-02。高德瓦片坐标系，必须做这一步。"""
    a, ee = 6378245.0, 0.00669342162296594323
    d_lat = _t_lat(lon - 105.0, lat - 35.0)
    d_lon = _t_lon(lon - 105.0, lat - 35.0)
    rad = lat * math.pi / 180.0
    magic = 1 - ee * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sq) * math.pi)
    d_lon = (d_lon * 180.0) / (a / sq * math.cos(rad) * math.pi)
    return lon + d_lon, lat + d_lat


def out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def to_gcj02(lon, lat):
    if out_of_china(lon, lat):
        return lon, lat
    return wgs84_to_gcj02(lon, lat)


def gcj02_to_wgs84(lon, lat):
    """GCJ-02 -> WGS84（不动点迭代）。

    必须与 template.html 里的 JS 版 `gcj02ToWgs84` **逐项一致**：
    迭代 10 轮，且**每轮都从原始 GCJ 重算**偏移量
    （写成 `w -= f(w)` 累积形式会累积到公里级）。

    用途：阿里 DataV 的坐标本身就是 GCJ-02（数据源为高德开放平台），
    而 GeoNames / USGS 是 WGS84 —— 两边混用时必须有逆向变换。
    """
    if out_of_china(lon, lat):
        return lon, lat
    w_lon, w_lat = lon, lat
    for _ in range(10):
        g_lon, g_lat = wgs84_to_gcj02(w_lon, w_lat)
        w_lon = lon - (g_lon - w_lon)   # 每轮从原始 GCJ 重算，不累积
        w_lat = lat - (g_lat - w_lat)
    return w_lon, w_lat


# ------------------------------------------------------------- 地名 -> 坐标
class GeocodeError(Exception):
    """地名解析失败。携带可操作提示，而不是干巴巴一句「失败」。"""

    def __init__(self, city, province=None, candidates=None):
        self.city = city
        self.province = province
        self.candidates = candidates or []
        super().__init__(self._build())

    def _build(self):
        lines = []
        if self.candidates:
            lines.append("省份 '%s' 下没有名为 '%s' 的行政区划（DataV 索引）。候选：%s"
                         % (self.province, self.city,
                            "、".join("%s(%s)" % (r.get("name", "?"),
                                                  r.get("admin1") or r.get("level") or "")
                                      for r in self.candidates[:6])))
        else:
            lines.append("未在 DataV 行政区划索引中找到地名 '%s'。" % self.city)
        lines.append("请用「市/县/区」全称（如「合肥市」「海淀区」），"
                     "或直接指定坐标：")
        lines.append("  --lon 117.28 --lat 31.86")
        return "\n".join(lines)


def norm_prov(name):
    """归一化省级名称：'河北省' -> '河北'，'内蒙古自治区' -> '内蒙古'。"""
    if not name:
        return ""
    s = name
    for suf in ("特别行政区", "自治区", "维吾尔", "壮族", "回族"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if s.endswith("省") or s.endswith("市"):
        s = s[:-1]
    return s


def _geocode_datav(city, province=None):
    """用阿里 DataV.GeoAtlas 行政区划索引做地名解析（唯一数据源）。

    为什么只用 DataV 而不用 GeoNames / Open-Meteo Geocoding —— 实测
    （v3.3.0 审计）：GeoNames 把不少中国城市的主名字段存成了**拉丁转写**，
    中文查询直接 0 命中（淮南 -> "Huainan"、厦门 -> "Xiamen"、
    温州 -> "Wenzhou"、宿州 -> "Suzhou"、宣城 -> "Xuancheng"……）。
    DataV 索引覆盖全国 3238 条省/市/区县级，**中文名齐全**，且与行政边界
    同源，命中后边界匹配亦可直接复用。

    ⚠️ 坐标系：DataV 的 lng/lat 是 **GCJ-02**（实测其索引点与该行政区
    多边形顶点重合到 0.06 m，与边界几何同源），而 USGS 查询/展示用 WGS84。
    因此这里必须做 GCJ-02 -> WGS84 逆向变换，否则 USGS 半径查询会偏 400–600 m。
    """
    index = boundary_datav.ensure_index()
    cands = boundary_datav.resolve_adcode(city, province, None, None, index)
    if not cands:
        return None
    c = cands[0]
    lon_g, lat_g = c["center"]
    lon, lat = gcj02_to_wgs84(lon_g, lat_g)
    prov = boundary_datav._province_name(index, c["adcode"]) or province or ""
    return lon, lat, c["name"], prov


def geocode_city(city, province=None):
    """解析中国城市 -> (lon, lat, 展示名, 省级名)。

    单一数据源：阿里 DataV.GeoAtlas 行政区划索引（覆盖全国 3238 条
    省/市/区县级，中文名齐全、与行政边界同源）。不再使用 GeoNames /
    Open-Meteo Geocoding（其对中国城市主名常为拉丁转写，中文查询易 0 命中）。

    DataV 坐标为 GCJ-02，经逆变换得到 WGS84 供 USGS 半径查询；展示名做
    norm_prov 归一化（如「安徽省·合肥市」->「安徽·合肥」），与文件名/标题一致。
    """
    hit = _geocode_datav(city, province)
    if not hit:
        # DataV 索引未匹配：收集候选，给出可操作提示
        try:
            index = boundary_datav.ensure_index()
            cands = boundary_datav.resolve_adcode(
                city, province, None, None, index)
        except Exception:  # noqa: BLE001
            cands = []
        raise GeocodeError(city, norm_prov(province), cands)

    lon, lat, name, prov = hit
    disp = ("%s·%s" % (norm_prov(prov), norm_prov(name))) if prov else norm_prov(name)
    return lon, lat, disp, prov



# ----------------------------------------------------------- 地震目录查询
# USGS 全球目录里混有**非天然事件**（爆破 / 矿震 / 塌陷 / 声爆 …），
# type 字段是唯一判别依据。实测（2026-10，M>=3、半径 200 km）：
#   合肥 22 条 / 唐山 161 条 / 淮南 17 条 / 阜新 65 条 —— 全为天然地震
# 但这是**中国区域的当前取样**，目录本身是全球的且会随时间增补，
# 不能靠取样结论放行。默认只保留天然地震，**剔除数必须可观测**：
# 静默剔除与静默丢数据是同一类故障。
NATURAL_TYPES = {"earthquake"}

# 震级量规。USGS 同一份目录里 mb / mw / ml / ms / mb_lg 混用（实测合肥 22 条：
# mb 11、mw 5、ml 4、ms 1、mb_lg 1）——不同量规对同一次地震可差 0.3~0.7 级，
# 直接拿混合口径排「最大震级」会误导。因此逐条保留量规并在页面上标注。
MAG_SCALE_CN = {
    "mw": "矩震级 Mw", "mww": "W 相矩震级 Mww", "mwb": "体波矩震级 Mwb",
    "mwc": "矩震级 Mwc", "mwr": "区域矩震级 Mwr", "mwb_ms": "矩震级 Mwb",
    "mb": "体波震级 mb", "mb_lg": "短周期面波震级 mb_Lg",
    "ms": "面波震级 Ms", "ms_20": "面波震级 Ms20", "ml": "地方性震级 ML",
    "mlv": "垂直分量地方性震级 MLv", "md": "持续时间震级 Md",
    "mfa": "宏观感觉震级 Mfa", "mh": "手工测定震级 Mh",
}


def mag_scale_cn(code):
    """震级量规代码 -> 中文。未收录/unknown 返回空串（页面按「未标注」处理）。"""
    if not code:
        return ""
    return MAG_SCALE_CN.get(str(code).strip().lower(), str(code))


def plan_batches(start_year, end_year, total, page=USGS_PAGE):
    """按预估总量规划分批区间 —— 纯函数，可单测。

    返回 [(y0, y1), ...]；总量未知或未超单批上限时返回空表（= 不分批）。
    need 留 15% 余量是因为预估与实际常有出入，宁可多切一段也别再触发
    自适应细分（细分要多打一轮请求）。
    """
    if total is None or total <= page:
        return []
    span = max(1, end_year - start_year)
    need = int(math.ceil(total / float(page)) * 1.15)
    step = max(1, int(span / need))
    batches = []
    y = start_year
    while y <= end_year:
        batches.append((y, min(end_year, y + step - 1)))
        y += step
    return batches


def feat_fingerprint(feat):
    """USGS 要素的稳定去重键。

    v3.5.0 修：原先只用 feat.get("id") 去重，USGS 若某条记录缺 id，
    则所有无 id 的记录会**塌缩成同一条**（fid=None 全部命中 seen 里的
    None）→ 静默丢数据。现在 id 缺失时用「时间+经纬度+震级」构造指纹。
    """
    fid = feat.get("id")
    if fid:
        return fid
    props = feat.get("properties") or {}
    coords = (feat.get("geometry") or {}).get("coordinates") or []
    return "fp:%s|%s|%s|%s" % (props.get("time"),
                               coords[0] if len(coords) > 0 else "",
                               coords[1] if len(coords) > 1 else "",
                               props.get("mag"))


def fetch_earthquakes(lon, lat, radius_km, min_mag, start_year, end_year,
                      drop_counts=None):
    """以城市为中心取矩形范围，交给 USGS；再按球面距离严格筛出圆形范围。

    截断防护：先用 /count 端点预估总量。若超过单次 limit，
    自动分批按时间分段拉取，避免静默丢事件（实测全国 M3 约 1.4 万条，
    单次 limit=20000 已接近上限，超大范围必须分段）。

    drop_counts：可选 dict，用于**可观测地**回收各类剔除量
    （non_tectonic=非天然事件）。剔除而不上报 = 静默丢数据，与本技能
    「禁止静默失败」的约定冲突，所以调用方应当传入并打印。
    """
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    minlat, maxlat = lat - dlat, lat + dlat
    minlon, maxlon = lon - dlon, lon + dlon

    base = {
        "format": "geojson",
        "minmagnitude": min_mag,
        "minlatitude": minlat, "maxlatitude": maxlat,
        "minlongitude": minlon, "maxlongitude": maxlon,
        "orderby": "time-asc",
    }

    # ---- 1. 预估总量，判断是否需要分段
    cparams = dict(base)
    cparams["starttime"] = "%d-01-01" % start_year
    # v3.5.0 修：USGS 把裸日期解析为该日 T00:00:00，
    # 写成 "%d-12-31" 会把 12 月 31 日整天丢掉（实测 2019-12-31 查 0 条，
    # 改到 2020-01-01 才拿到 5 条）。上界改用当日最后一秒。
    cparams["endtime"] = "%d-12-31T23:59:59" % end_year
    cparams.pop("orderby", None)
    count_url = USGS + "/count?" + urllib.parse.urlencode(cparams)

    def _query(y0, y1, limit=USGS_PAGE):
        params = dict(base)
        params["starttime"] = "%d-01-01" % y0
        # v3.5.0 修：上界用当日最后一秒，避免丢掉 12 月 31 日整天
        params["endtime"] = "%d-12-31T23:59:59" % y1
        params["limit"] = limit
        url = USGS + "/query?" + urllib.parse.urlencode(params)
        raw = http_get(url, timeout=180, retries=4)
        return json.loads(raw).get("features") or []

    def _count():
        try:
            return int(json.loads(http_get(count_url, timeout=60, retries=3)
                                  ).get("count", 0))
        except Exception as exc:  # noqa: BLE001
            print("  [warn] count 端点不可用（%s），改用单批拉取" % exc,
                  file=sys.stderr)
            return None

    # count 与整段 query **并发**：两个请求各约 4~5 s，串行要 9 s，
    # 并发后只需等最慢的一个。count 只用于判断是否需要按年分段。
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_q = ex.submit(_query, start_year, end_year)
        fut_c = ex.submit(_count)
        feats_first = fut_q.result()
        total = fut_c.result()
    print("  [batch] %d–%d: %d 条" % (start_year, end_year, len(feats_first)))

    def _fetch_range(y0, y1, feats=None):
        """取 [y0,y1] 的**完整**事件列表；返回数达上限就再对半细分。

        v3.2.0 修掉的静默丢数据 bug：原先只按 count 预估分段，
        实测 1963–2025 段仍返回**满额 20000 条**（被截断），
        30542 条里丢了 8766 条且**毫无报错**。现在按「实际取回量」
        自适应细分：满额 → 对半再取，直到每段都不满额。
        """
        if feats is None:
            feats = _query(y0, y1)
        if len(feats) < USGS_PAGE:
            return feats
        mid = (y0 + y1) // 2
        if mid <= y0 or mid >= y1:
            print("  [warn] %d–%d 已是单年且取回 %d 条（达上限 %d），"
                  "该年数据可能被截断" % (y0, y1, len(feats), USGS_PAGE),
                  file=sys.stderr)
            return feats
        print("  [info] %d–%d 取回 %d 条（达上限），细分重取"
              % (y0, y1, len(feats)))
        return (_fetch_range(y0, mid) + _fetch_range(mid + 1, y1))

    batches = plan_batches(start_year, end_year, total)
    if batches:
        print("  [info] 预估 %d 条 > 单批上限 %d，按 %d 段分批拉取"
              % (total, USGS_PAGE, len(batches)))

    # ---- 2. 逐批拉取
    quakes = []
    seen = set()
    n_non_tectonic = 0
    # 首批 = 并发时已经取回的那一批（其条数已在上面打印，不重复打）；
    # 若需分段，改用自适应细分。
    if not batches:
        feats_list = _fetch_range(start_year, end_year, feats_first)
    else:
        feats_list = []
        for (y0, y1) in batches:
            fs = _fetch_range(y0, y1)
            print("  [batch] %d–%d: %d 条" % (y0, y1, len(fs)))
            feats_list.extend(fs)
    # v1.4.0：这一段原先整体**多缩进了一层**（4 空格），Python 允许但不合
    # PEP8，读起来像还嵌在某个 if 里。现已对齐。
    for feat in feats_list:
        fid = feat_fingerprint(feat)
        if fid in seen:
            continue
        seen.add(fid)
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        qlon, qlat = coords[0], coords[1]
        depth = coords[2] if len(coords) > 2 else None
        mag = props.get("mag")
        if mag is None or mag < min_mag:
            continue
        # 非天然事件（爆破/矿震/塌陷…）默认剔除，数量回收到 drop_counts
        etype = str(props.get("type") or "earthquake").strip().lower()
        if etype not in NATURAL_TYPES:
            n_non_tectonic += 1
            continue
        d = haversine_km(lon, lat, qlon, qlat)
        if d > radius_km:
            continue
        mtype = (props.get("magType") or "").strip()
        quakes.append({
            "id": fid,
            "lon": qlon, "lat": qlat, "depth": depth,
            "mag": round(float(mag), 2),
            # 量规：mb/mw/ml/ms…混用，逐条保留以便页面标注（见 MAG_SCALE_CN）
            "mag_type": mtype,
            # 中文口径名**由 Python 落盘**，不在 JS 里再抄一份映射表：
            # 两份映射迟早会不同步（改了 Python 忘了改模板 = 页面显示旧口径）。
            "mag_type_cn": mag_scale_cn(mtype),
            # 审定状态：reviewed=人工核定 / automatic=自动测定（精度较低）
            "status": (props.get("status") or "").strip(),
            "place": (props.get("place") or "").strip(),
            "time": props.get("time"),
            "url": props.get("url"),
            "dist_km": round(d, 1),
            "type": etype,
        })
    quakes.sort(key=lambda q: q["time"] if q["time"] is not None else 0)
    if drop_counts is not None:
        drop_counts["non_tectonic"] = n_non_tectonic
    if total is not None and total > USGS_PAGE:
        # v1.1.0 修：原先三个槽位里两个填的都是 len(quakes)，
        # 「分批共取回」实际打印的是筛选后条数，与「原始矩形范围」重复，
        # 看不出分段到底拉回多少。现在分别用 feats_list / total / quakes。
        print("  [info] 分批共取回 %d 条（原始矩形范围预估 %d 条，"
              "经圆形范围与天然事件筛选后 %d 条）"
              % (len(feats_list), total, len(quakes)))
    return quakes


# 国家地震台网（CENC / 中国地震台网中心）近期审定目录的镜像接口。
# 该接口直接转发 CENC 的「正式测定」结果，带中文地名（如「青海玉树州杂多县」），
# 比 USGS 的英文地名更适合中国地图。注意：这是**实时/近期**列表（通常最近约一周、
# 约 50 条），**不含历史目录**；历史的完整性仍由 USGS 兜底（见 _merge_cenc_usgs）。
CENC_URL = "https://api.wolfx.jp/cenc_eqlist.json"


def fetch_cenc_recent(min_mag=3.0, timeout=30, retries=3):
    """取国家地震台网（CENC）近期审定地震。

    返回字段与 USGS 字典对齐（lon/lat/ depth/mag/place/time[UTC 毫秒]/id/source）。
    时间：CENC 给的是北京时间（UTC+8），统一转 UTC 毫秒以与 USGS 同口径。
    坐标：CENC 发布 CGCS2000（与 WGS84 差 <1 m），显示前统一按 WGS84 处理做
    WGS84→GCJ-02 偏移，与高德底图同系。
    网络失败：直接抛异常，由调用方捕获并降级到 USGS，绝不静默吞掉。
    """
    raw = http_get(CENC_URL, timeout=timeout, retries=retries)
    data = json.loads(raw)
    quakes = []
    for key, ev in data.items():
        if not isinstance(ev, dict):
            continue  # 跳过 md5 等非事件键
        try:
            mag = float(ev.get("magnitude"))
        except (TypeError, ValueError):
            continue
        if mag < min_mag:
            continue
        try:
            lat = float(ev.get("latitude"))
            lon = float(ev.get("longitude"))
        except (TypeError, ValueError):
            continue
        dep_raw = ev.get("depth")
        try:
            depth = (float(dep_raw) if dep_raw not in (None, "", "null") else None)
        except (TypeError, ValueError):
            depth = None
        tstr = (ev.get("time") or "").strip()
        tms = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.datetime.strptime(tstr, fmt)
                tms = int((dt - datetime.timedelta(hours=8)
                           - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
                break
            except ValueError:
                continue
        place = (ev.get("location") or ev.get("placeName") or "").strip()
        # 烈度（中国台网独有，USGS 目录没有该字段）。实测字段形如 "5"/"4"，
        # 表示震中烈度（中国烈度表，罗马数字 I–XII 的阿拉伯数字等价）。
        inten = None
        try:
            raw_i = ev.get("intensity")
            if raw_i not in (None, "", "null"):
                inten = float(raw_i)
        except (TypeError, ValueError):
            inten = None
        quakes.append({
            "id": "cenc:" + str(ev.get("EventID")
                              or ("%s@%s" % (tstr, place))),
            "lon": lon, "lat": lat, "depth": depth,
            "mag": round(mag, 2), "place": place,
            # v1.1.0：中国台网**不随事件发布量规代码**，不能臆测成 Ms/ML，
            # 留空（页面显示「未标注」）——把未知标成已知是数据造假。
            "mag_type": "",
            "mag_type_cn": "",
            # CENC 的 type 字段语义是**审定状态**（reviewed/automatic），
            # 而 USGS 的 type 是**事件类型**（earthquake/quarry blast）。
            # 若直接存进 type，两源合并后同一个字段会有两种含义。
            # 统一：type 恒为事件类型，审定状态一律走 status。
            "status": (ev.get("type") or "").strip(),
            # 烈度：中国烈度表为整数（I–XII），接口实测给的是 "9"/"5" 这类
            # 字符串，转 float 会变成 9.0，页面上写「9.0 度」不专业。
            # v1.2.0：整数值一律存 int，模板直接拼「度」。
            "intensity": (int(inten) if inten is not None
                          and float(inten).is_integer() else inten),
            "time": tms, "url": "", "dist_km": None,
            "type": "earthquake",
            "source": "CENC",
            # EventID 是台网事件的权威编号，供 data.json 溯源核对
            # （模板不渲染，但它是「这条地震到底是哪条」的唯一凭据，
            #   与 id 的 "cenc:" 前缀部分重复，显式保留以免字符串切割出错）
            "event_id": ev.get("EventID"),
        })
    quakes.sort(key=lambda q: q["time"] or 0)
    return quakes


def _merge_cenc_usgs(usgs, cenc, lon, lat, radius_km):
    """把 CENC 近期目录并入 USGS 历史目录，并去重。

    - CENC 先按距离裁剪到半径内（接口本身是全国近期列表，无半径参数）；
    - 与 USGS 中「时间±120 s 且位置 <50 km」的事件判定为同一次，丢弃 USGS 副本
      （优先保留 CENC：中文地名 + 国家台网权威）；
    - 仅出现在 USGS 的历史事件保留。

    返回 (merged, info)。merged **按时间升序**；info 记录台网新增条数与
    被判定为重复而丢弃的 USGS 条数，供调用方打印（v1.2.0）。
    """
    for q in cenc:
        q["dist_km"] = round(haversine_km(lon, lat, q["lon"], q["lat"]), 1)
    cenc_in = [q for q in cenc if q["dist_km"] <= radius_km]
    kept = []
    n_dup = 0
    for u in usgs:
        dup = any(abs((u.get("time") or 0) - (c.get("time") or 0)) < 120000
                  and haversine_km(u["lon"], u["lat"], c["lon"], c["lat"]) < 50
                  for c in cenc_in)
        if dup:
            n_dup += 1
        else:
            kept.append(u)
    merged = cenc_in + kept
    # v1.2.0 修复：**必须重排序**。两个源各自内部有序，但
    # `cenc_in + kept` 把「近期台网事件」整体拼到「历史 USGS」前面，
    # 结果是 2026 年排在 1940 年之前。列表顺序不是 cosmetic —
    # quake_stats 曾依赖插入顺序取 earliest/latest，于是算出
    # earliest=2026 / latest=2025（最早晚于最晚），目录完整性结论随之颠倒。
    # 现在统计侧改为 min/max（不依赖顺序），这里排序是为了**目录表与
    # 直方图前端渲染顺序**正确。
    merged.sort(key=lambda q: q.get("time") or 0)
    # 去重/新增条数回传：不做就是静默改数据
    return merged, {"cenc_added": len(cenc_in), "usgs_dropped_dup": n_dup}


def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------ 断层数据加载
def fetch_fault_db():
    """下载 CN-faults.gmt（CAFDv2023，约 7.9 MB）到一次性临时目录。

    v1.0.5 起不做磁盘缓存：每次运行实时拉取，保证断库永远是最新版。
    速度策略（实测，本机网关）：
      ① 主镜像 ghproxy.net 走 16 路分块并发（~58 s / ~133 KB/s）；
      ② 并行失败（个别分块两次重试仍不成）→ 换下一个镜像重试；
      ③ 全部镜像的分块都失败 → 退回单连接 http_get 拉最后一个镜像。
    文件落在 TMP_DIR，进程退出即删，技能目录与用户缓存目录均零残留。
    """
    dest = tpath("CN-faults.gmt")
    for url in FAULT_URLS:
        try:
            print("  [fetch] 活动断层数据库 CN-faults.gmt … (%s)"
                  % url.split("/")[2])
            got = download_parallel(url, dest, min_size=1_000_000,
                                    workers=16, timeout=150)
            if got:
                print("  → 16 路分块并发下载完成：%.1f MB" % (got / 1048576.0))
                return dest
            print("  [warn] 分块并发失败，回退单连接：%s" % url.split("/")[2],
                  file=sys.stderr)
            data = http_get(url, timeout=600, retries=2)
            if len(data) < 1_000_000:
                raise RuntimeError("数据过小 (%d bytes)，疑似错误页" % len(data))
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(data)
            return dest
        except Exception as exc:  # noqa: BLE001
            print("  [warn] 源不可用 %s: %s" % (url.split("/")[2], exc),
                  file=sys.stderr)
    raise SystemExit("无法获取断层数据，请检查网络后重试")


def ensure_gadm_file(level):
    """下载 GADM 4.1 中国行政边界（ADM2=地级市 / ADM3=县级）到临时目录。

    GADM 以 zip 分发（约 1.4 MB / 2.7 MB）。仅 DataV 不可用时才会走到，
    同样不落跨运行缓存：解压出的 JSON 放 TMP_DIR，退出即删。
    """
    dest = tpath("gadm41_CHN_%d.json" % level)
    if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
        return dest

    url = GADM_URLS[level]
    print("  [fetch] GADM 4.1 行政边界 ADM%d …" % level)
    import io
    import zipfile
    raw = http_get(url, timeout=600, retries=3, binary=True)
    if len(raw) < 100_000:
        raise RuntimeError("下载内容过小 (%d bytes)" % len(raw))
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = zf.namelist()[0]
        data = zf.read(name)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("GADM zip 解析失败: %s" % exc)
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def _norm_cn_name(name):
    """归一化行政区名用于匹配：去空白、去括号内容。

    注意：**不剥离「市/县/区/省」后缀**。GADM 的 ADM2 是地级市
    （「西安市」）、ADM3 是区县（「新城区」），若把「市」剥掉，
    '西安' 会同时命中 '长安区'，导致城市边界匹配到市辖区而非整个市。
    层级区分改由 TYPE 字段与层级循环保证。
    """
    if not name:
        return ""
    s = str(name).strip()
    # GADM 部分条目是「繁体|简体」格式（如 '陝西|陕西'、'北京|北京'）
    if "|" in s:
        s = s.split("|")[-1].strip()
    if "（" in s:
        s = s.split("（")[0]
    if "(" in s:
        s = s.split("(")[0]
    return s.strip()


def _match_score(want, cand):
    """名称匹配打分。返回 0 表示不匹配。

    只接受「完全相等」或「双方去掉行政后缀后相等」，
    不接受前缀包含（'西安' vs '长安区' 会被误判）。

    早期版本误写成 `stripped == want`（恒真），导致任意候选都拿 90 分，
    结果按遍历顺序返回了「安康市」而不是「西安市」。
    """
    if not want or not cand:
        return 0
    if want == cand:
        return 100
    # 双方各去掉一个行政后缀后比较
    #   '唐山市' vs '唐山' -> '唐山' == '唐山'  命中
    #   '西安'   vs '西安市' -> '西安' == '西安' 命中
    def strip(s):
        for suf in ("特别行政区", "自治州", "自治区", "地区", "省", "市",
                    "县", "区", "盟"):
            if s.endswith(suf) and len(s) > len(suf):
                return s[: -len(suf)]
        return s
    sw, sc = strip(want), strip(cand)
    if sw and sc and sw == sc:
        return 90
    return 0


def _iter_rings(geom):
    """从 Polygon / MultiPolygon 提取**全部**环坐标列表（含内环/洞）。

    v3.5.0 修：原先只取 poly[0]（外环），把内环（洞）直接丢掉 ——
    于是「洞里」的点被 `_point_in_rings` 判为在边界内，
    落在洞里的断裂不会被裁掉（多算），边界内的地震统计也会偏多。
    现在与 boundary_datav._iter_rings 的语义统一：**全部环**。
    `_point_in_rings` 用「逐环翻转 XOR」，对「互不相交的多边形 + 洞」
    都成立 —— 点在洞里会多翻转一次回到外部。
    """
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        for ring in coords:
            if ring:
                yield ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                if ring:
                    yield ring


def _bbox_of(rings):
    lons = [p[0] for r in rings for p in r]
    lats = [p[1] for r in rings for p in r]
    return (min(lons), min(lats), max(lons), max(lats))


def _gadm_prov(name):
    """归一化 GADM 的省级名。

    GADM 的 NL_NAME_1 格式特殊：**繁体|简体** 用竖线分隔，
    例如 '陝西|陕西'、'江蘇|江苏'、'內蒙古自治區|内蒙古自治区'。
    取竖线后的简体部分再做后缀剥离，才能和归一化后的省名
    '陕西' / '河北' 对上。
    """
    if not name:
        return ""
    s = str(name).strip()
    if "|" in s:
        s = s.split("|")[-1].strip()
    return norm_prov(s)


def _is_district_type(t):
    """GADM 的 TYPE 是拼音：市辖区 = Shìxiáqū / District。

    用于剔除 ADM2 中误入的市辖区条目。
    """
    if not t:
        return False
    t = str(t).lower()
    return ("shìxiáqū" in t or "shixiaqu" in t or t.startswith("district")
            or "district" in t)


def find_admin_boundary(city, province, lon, lat, search_km=120.0):
    """查找城市行政边界。

    策略：先在 ADM2（地级市）按中文名匹配；未命中则退到 ADM3（县级）。
    名字匹配不上时退化为「包含目标坐标的最小行政区」兜底。

    返回 dict: {name, level, admin, source, rings:[[ [lon,lat], ... ], ...]}
    """
    want = _norm_cn_name(city)
    # 省级统一用 norm_prov（剥离「省/市/自治区」等后缀），
    # 以便和 GADM 的「河北」与 Open-Meteo 的「河北省」都能对上。
    prov = norm_prov(province)

    for level in (2, 3):
        try:
            path = ensure_gadm_file(level)
        except Exception as exc:  # noqa: BLE001
            print("  [warn] GADM ADM%d 获取失败: %s" % (level, exc), file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        feats = data.get("features") or []
        # --- 1) 名字匹配，取最高分
        best = None
        for f in feats:
            props = f.get("properties") or {}
            cand = _norm_cn_name(props.get("NL_NAME_%d" % level))
            if not cand:
                cand = _norm_cn_name(props.get("VARNAME_%d" % level))
            sc = _match_score(want, cand)
            if sc == 0:
                continue
            # 层级校验：ADM2 必须是地级市/地区/盟，不能是市辖区
            if level == 2 and _is_district_type(props.get("TYPE_2")):
                continue
            # 省级比较必须归一化：GADM 是「陝西|陕西」，Open-Meteo 是「陕西省」
            p1 = _gadm_prov(props.get("NL_NAME_1"))
            if prov and p1 and p1 != prov:
                continue
            if best is None or sc > best[0]:
                best = (sc, f, props)
        if best is None:
            continue
        _, f, props = best
        rings = [r for r in _iter_rings(f.get("geometry") or {}) if len(r) >= 3]
        if not rings:
            continue
        lvl_name = "地级市" if level == 2 else "县级"
        return {
            "name": _norm_cn_name(props.get("NL_NAME_%d" % level)
                                 or props.get("NAME_%d" % level)),
            "level": level,
            "level_name": lvl_name,
            "admin": _gadm_prov(props.get("NL_NAME_1")
                                or props.get("NAME_1")) or "",
            "source": "GADM 4.1",
            "source_short": "GADM 4.1",
            "rings": rings,
        }

    # --- 2) 名字都匹配不上：按「包含目标点的最小行政区」兜底
    return _boundary_by_containment(city, lon, lat)


def _boundary_by_containment(city, lon, lat):
    """兜底：找到包含目标坐标的行政单元（取面积最小者，即最贴近的那一级）。"""
    best = None
    for level in (3, 2):
        try:
            path = ensure_gadm_file(level)
        except Exception as exc:  # noqa: BLE001
            # v3.5.0：原先 except Exception: continue —— GADM 下载失败被
            # **完全静默**吞掉，最终只报「找不到边界」，用户无从判断
            # 是网络问题还是真的没有该行政区。现在打印告警。
            print("  [warn] GADM level=%d 获取失败：%s" % (level, exc),
                  file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for f in data.get("features") or []:
            props = f.get("properties") or {}
            rings = [r for r in _iter_rings(f.get("geometry") or {}) if len(r) >= 3]
            if not rings:
                continue
            minx, miny, maxx, maxy = _bbox_of(rings)
            if not (minx <= lon <= maxx and miny <= lat <= maxy):
                continue
            if not _point_in_rings(lon, lat, rings, _ring_bboxes(rings)):
                continue
            area = (maxx - minx) * (maxy - miny)
            if best is None or area < best[0]:
                best = (area, level, props, rings)
        if best:
            break
    if not best:
        return None
    _, level, props, rings = best
    return {
        "name": _norm_cn_name(props.get("NL_NAME_%d" % level)
                             or props.get("NAME_%d" % level)),
        "level": level,
        "level_name": "地级市" if level == 2 else "县级",
        "admin": _gadm_prov(props.get("NL_NAME_1") or props.get("NAME_1")) or "",
        "source": "GADM 4.1",
        "source_short": "GADM 4.1",
        "rings": rings,
    }


def _point_in_rings(lon, lat, rings, ring_bboxes=None):
    """射线法判断点是否在任一外环内。

    逐环翻转并累积 XOR 是标准做法，对「多个互不相交的外环」（MultiPolygon）
    与「带内环的 Polygon」都成立：点在洞里会再翻转一次回到外部。

    v3.6.0 性能：加**每环 bbox 快速排除**。实测这是本地裁剪的
    第一热点（cProfile：`_point_in_rings` tottime 0.100 s / 3110 次调用，
    占本地裁剪总耗时 0.214 s 的 **47%**）—— 因为射线法要对**每个环的所有边**
    逐边判断，而市域边界的环往往有几百上千个顶点，其中绝大多数边
    根本不可能与该点的水平射线相交。
    先用「点的 lon/lat 是否落在该环的 bbox 内」排除掉完全不可能命中的环，
    命中候选环才进射线法。**这是纯粹的剪枝，不改变判定结果**。
    """
    inside = False
    for ri, ring in enumerate(rings):
        n = len(ring)
        if n < 3:
            continue
        if ring_bboxes is not None:
            bx0, by0, bx1, by1 = ring_bboxes[ri]
            if lon < bx0 or lon > bx1 or lat < by0 or lat > by1:
                continue
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat):
                xint = (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
                if lon < xint:
                    inside = not inside
            j = i
    return inside


def _ring_bboxes(rings):
    """逐环的 (minx, miny, maxx, maxy)，供 _point_in_rings 剪枝用。"""
    out = []
    for ring in rings:
        if len(ring) < 3:
            out.append((0.0, 0.0, -1.0, -1.0))  # 空盒 → 恒不相交
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


# 市域过滤的退化阈值：边界内记录少于这个数就退回半径统计。
#
# v1.8.0 实测把这个值从 8 改成 1 —— 8 是我凭空定的，实测证明它错误地
# 掩盖了正确行为：
#   合肥：半径 200 km 内 20 次，市域内只有 2 次（2024-09 肥东 M4.2 / M4.9）。
#   若按 8 的阈值回退，页面会报「最大震级 6.4」，而那其实是 1917 年
#   六安霍山地震（距合肥 131 km）—— 把邻市的大震说成本市的，正是
#   这一版要修的问题。2 次才是「合肥发生过什么地震」的正确答案。
#
# 现在的语义：**只有市域内 0 条才回退**。0 条大概率是范围过小或边界
# 匹配错误，此时宁可多画（给半径结果）也别给一张空图 —— 多画会在
# 概览里写明口径，而空图会让人误判「这个城市没有地震」。
CITY_SCOPE_MIN_KEEP = 1

# ---- v2.0.0 产物体积：坐标精度与抽稀（实测扫描定参） ----
#
# 实测（唐山/西安/汶川三份产物）：faults 占产物 43%~63%，其中 coords 里
# 6 位小数占 90% —— 0.000001° ≈ 0.1 m，远超 30 m 级断库本身的定位精度，
# 也远超屏幕分辨率（z9 下 1 px ≈ 280 m）。
#
# 参数由扫描表选定（slim_scan.py，体积收益 vs 几何保真）：
#   prec=4 eps=0.0001 -> 唐山 92.5→40.7 KB(-56%)、西安 108→33 KB(-69%)、
#                        汶川 221→101 KB(-54%)，形状改变 ≤ 27 m（<0.1 px）
#   prec=4 eps=0.0002 -> 再多 4~6%，但汶川出现 57 m 变形（仍 <0.3 px）
# 取保守的 0.0001：收益已过半，变形小到不可见。
FAULT_PREC = 4           # 断裂坐标小数位（≈11 m）
FAULT_DP_EPS = 0.0001    # 断裂抽稀容差（度，≈11 m）
BND_PREC = 4             # 行政边界小数位（≈11 m）
QUAKE_PREC = 4           # 震中坐标小数位（≈11 m，远小于震中定位误差）
#
# v1.4.0：**边界不做抽稀**（原 BND_DP_EPS 已删除）。
# 实测（合肥 1080 点 / 杂多 1221 点，Hausdorff 偏移为点到简化折线的垂距）：
#     eps=0.0001 → 1080→1080、1221→1221   收益 **0%**，偏移 0 m
#     eps=0.0005 →  918 / 1216             收益 7.3%，偏移 55 m
#     eps=0.001  →  653 / 1084             收益 24.5%，偏移 111 m
# 也就是说：容差小到不失真时**一个点都压不掉**（行政边界沿路网/水系勾绘，
# 点间距约 90 m 且形状锯齿，局部垂距普遍 >50 m）；而压得动的容差（≥55 m）
# 在放大到 z15（≈4 m/px）时是 14~28 px 的**肉眼可见偏移** —— 边界是图上
# 特意加粗标红的要素，用它换 5 KB 不划算。
# 顺带排除了一个猜想：曾怀疑是「闭环首尾重合导致 DP 顶层的弦退化为 0」，
# 实测开环（对 ring[:-1] 做 DP）与闭环结果几乎一致（915 vs 918），
# 所以不是算法退化，是数据本身的形状决定的。
#
# 断裂侧的 FAULT_DP_EPS 则**确实有效**（实测合肥 200 km 内 1871→992 点，
# 压缩 47%，最大偏移 11.1 m），两者不能相提并论。

# 这两个字段不落盘（实测汶川 1268 次：url 76.8 KB、type 14.9 KB）：
#   url  —— 完全可由 id 拼出 https://earthquake.usgs.gov/earthquakes/eventpage/<id>
#   type —— 全量取样恒为 "earthquake"，且模板从未读取
QUAKE_DROP_FIELDS = ("url", "type")

# 地震目录表首屏行数（v2.0.0）。汶川 1268 行若全量渲染要 7600 个 DOM 节点，
# 首屏只给 120 行，其余由前端「显示更多」按页追加。
QTABLE_FIRST_PAGE = 120


def _douglas_peucker(pts, eps):
    """Douglas-Peucker 折线抽稀，eps 单位为度（仅用于压缩产物体积）。

    该函数原属已下线的「GB 18306 走廊」模块，随模块退役时就地保留，
    以免破坏 slim_faults / slim_rings。
    """
    if len(pts) < 3:
        return list(pts)
    dmax, idx = 0.0, 0
    x1, y1 = pts[0]
    x2, y2 = pts[-1]
    denom = math.hypot(x2 - x1, y2 - y1)
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if denom < 1e-12:
            d = math.hypot(px - x1, py - y1)
        else:
            d = abs((y2 - y1) * px - (x2 - x1) * py + x2 * y1 - y2 * x1) / denom
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        left = _douglas_peucker(pts[:idx + 1], eps)
        right = _douglas_peucker(pts[idx:], eps)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def slim_faults(faults):
    """降精度 + Douglas-Peucker 抽稀，只影响**产物体积**，不改语义。"""
    out = []
    for f in faults:
        c = [[round(float(x), FAULT_PREC), round(float(y), FAULT_PREC)]
             for x, y in f.get("coords") or []]
        if len(c) > 2:
            c = _douglas_peucker(c, FAULT_DP_EPS)
        if len(c) < 2:                      # 退化保护：至少留两点
            c = (c * 2)[:2] if c else []
        g = dict(f)
        g["coords"] = c
        out.append(g)
    return out


def slim_quakes(quakes):
    """震中降精度 + 去掉可派生/未使用的字段。

    url 不落盘是最大的一项（汶川 76.8 KB），代价是前端要自己拼——
    见模板里的 quakeUrl()。
    type 在**过滤阶段**是关键字段（剔除非天然事件，见 fetch_earthquakes
    的 drop_counts），过滤完成后它的使命就结束了，模板不读，故一并丢弃。
    """
    out = []
    for q in quakes:
        g = dict(q)
        try:
            g["lon"] = round(float(q["lon"]), QUAKE_PREC)
            g["lat"] = round(float(q["lat"]), QUAKE_PREC)
        except (TypeError, ValueError):
            pass
        for k in QUAKE_DROP_FIELDS:
            g.pop(k, None)
        out.append(g)
    return out


def slim_rings(rings):
    """行政边界环降精度，**必须保持环闭合**。

    v1.4.0：这里**不做抽稀** —— 实测抽稀对真实市域边界收益为 0
    （见 BND_PREC 上方的扫描表），属于无效优化，已删除。
    只保留降精度（6 位 -> 4 位小数，≈11 m，远小于边界本身的勾绘精度）。

    注意：过滤地震点发生在 slim **之前**（见 main 的步骤顺序），
    所以这里只影响产物体积与画面，不影响任何统计结果。
    """
    out = []
    for r in rings or []:
        c = [[round(float(x), BND_PREC), round(float(y), BND_PREC)]
             for x, y in r]
        if c and c[0] != c[-1]:
            c.append(list(c[0]))            # 环必须闭合，否则填充会漏
        if len(c) >= 3:
            out.append(c)
    return out


def filter_quakes_in_boundary(quakes, boundary, min_keep=CITY_SCOPE_MIN_KEEP):
    """只保留落在行政边界**内**的地震点。

    为什么要这一步：USGS 只支持矩形/圆形查询，半径搜索只是**取数手段**，
    但页面标题写的是「某城市的历史地震」——半径 200 km 的结果里
    大量事件发生在邻市邻省，直接全画是答非所问。

    坐标系：调用时 quakes 与 boundary["rings"] **都已转成 GCJ-02**，
    两侧同坐标系才能直接判包含。若边界来自 WGS84 源（GADM 回退），
    调用方必须先 to_gcj_list，否则会整体偏移 400–600 m，
    边缘的点判定结果随即反转。

    退化保护（三种情况都返回半径结果，并在 info 里写明原因）：
      1. 无边界（`--no-boundary` 或下载失败）；
      2. 边界内记录数 < min_keep；
      3. rings 为空。

    返回 (kept_quakes, info)。
    """
    info = {"mode": "radius", "radius_total": len(quakes), "inside": None,
            "kept": len(quakes), "reason": "", "min_keep": min_keep}
    if not boundary or not boundary.get("rings"):
        info["reason"] = "未加载行政边界，按圆形半径统计"
        return list(quakes), info

    rings = boundary["rings"]
    # v3.6.0：预计算逐环 bbox，供 _point_in_rings 剪枝（见该函数说明）
    rbbs = _ring_bboxes(rings)
    kept = [q for q in quakes
            if _point_in_rings(q["lon"], q["lat"], rings, rbbs)]
    info["inside"] = len(kept)

    if len(kept) == 0:
        info["reason"] = "市域内无目录记录，退回圆形半径统计"
        return list(quakes), info
    if len(kept) < min_keep:
        info["reason"] = ("市域内仅 %d 次记录（< %d），退回圆形半径统计"
                          % (len(kept), min_keep))
        return list(quakes), info

    info["mode"] = "city"
    info["kept"] = len(kept)
    info["reason"] = ""
    return kept, info


def inline_leaflet(html):
    """把 leaflet 的 CSS/JS 内联进 HTML，得到**单文件自包含**页面。

    v3.1.0 新增 --standalone。--offline 只是「附本地副本 + CDN 失败回退」，
    页面仍要连着 leaflet.js/css 三个文件；而单文件内联后，一个 HTML
    发给谁都能直接打开（断网也行），更适合对外交付。

    实现上把模板里的 <link href=CDN ...> 与 <script src=CDN ...></script>
    换成 <style>…</style> 与 <script>…</script>（连 onerror 回退一起去掉，
    内联后没有"加载失败"可言）。
    """
    ljs, lcss = ensure_leaflet_assets()
    if not ljs or not lcss:
        return html, False
    # ⚠️ replacement 必须用**函数**而不是字符串：CSS/JS 里含大量
    # 反斜杠（正则字面量 \s、\d 等），字符串形式会被 re 当成替换模板
    # 转义，直接抛 "bad escape \s"。函数形式不做转义。
    # CSS：<link rel="stylesheet" href="...leaflet.css" onerror="...">
    html = re.sub(
        r'<link[^>]*leaflet\.css[^>]*>',
        lambda m: '<style>\n/* leaflet 1.9.4 (inlined) */\n%s\n</style>' % lcss,
        html, count=1, flags=re.S)
    # JS：<script src="...leaflet.js" onerror="..."></script>
    html = re.sub(
        r'<script[^>]*leaflet\.js[^>]*></script>',
        lambda m: '<script>\n%s\n</script>' % ljs,
        html, count=1, flags=re.S)
    inlined = "/* leaflet 1.9.4 (inlined) */" in html
    return html, inlined


def ensure_leaflet_assets():
    """下载 Leaflet 1.9.4 的 js/css，供离线降级使用。

    背景：HTML 走 CDN 引用 Leaflet，若 unpkg 不可达，地图会整个白屏且
    无任何提示。--offline/--standalone 时下载资源并随产物一起写出
    （leaflet.min 副本放输出目录，页面用 onerror 自动切换）。
    v1.0.5 起同样不落跨运行缓存：每次用到才下载，临时文件退出即删。
    返回 (js_text_or_None, css_text_or_None)。
    """
    out = []
    for name in ("leaflet.js", "leaflet.css"):
        try:
            data = http_get(LEAFLET_CDN + name, timeout=60, retries=2)
            if len(data) < 10_000:
                raise RuntimeError("内容过小")
            out.append(data)
        except Exception as exc:  # noqa: BLE001
            print("  [warn] Leaflet %s 下载失败: %s" % (name, exc),
                  file=sys.stderr)
            out.append(None)
    return out[0], out[1]


def parse_faults(path):
    """解析 GMT 段式文本，产出 [{name, en_name, kind, age, coords:[[lon,lat],..]}]。"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    try:
        start = lines.index("# FEATURE_DATA")
    except ValueError:
        start = 0

    feats = []
    cur = None
    for line in lines[start:]:
        if line.startswith(">"):
            cur = {"attrs": None, "coords": []}
            feats.append(cur)
        elif line.startswith("# @D"):
            if cur is not None:
                cur["attrs"] = line[4:].split("|")
        elif cur is not None and line and not line.startswith("#"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    cur["coords"].append([float(parts[0]), float(parts[1])])
                except ValueError:
                    pass

    out = []
    for f in feats:
        if len(f["coords"]) < 2:
            continue
        a = f["attrs"] or [""] * 11
        # 字段序（断库头部 # @N 行原文）：
        #   FZN_Ch|FZN_En|FN_Ch|FN_En|FSN_Ch|FSN_En|Fea_Ch|Fea_En|AGE|AGE_Des|RefE
        # 实测（v1.1.0，断库前 541 段取样）各字段填充率：
        #   idx0  断裂带名 FZN_Ch   18%   idx2 断裂名 FN_Ch      69%
        #   idx6  性质     Fea_Ch   80%   idx8 年代 AGE        100%
        #   idx9  年代描述 AGE_Des 100%   idx10 参考文献 RefE  100%
        #
        # v1.1.0 起重新落盘 idx0（zone）与 idx10（ref）：
        #   · ref  —— 100% 有值（如 "UAFS:Qingdao"、"SMCAR; Xu et al., 2016"），
        #              是断层可溯源性的唯一依据，此前被直接丢弃；
        #   · zone —— 早期版本曾因「缓存里恒为空」被判定为无用而剔除，
        #              实测非空率 18%，是真实分类信息（断裂带 > 断裂），
        #              非空时在弹窗显示即可，无需恢复「带」图层。
        name = (a[2] if len(a) > 2 else "") or ""
        en = (a[3] if len(a) > 3 else "") or ""
        kind = (a[6] if len(a) > 6 else "") or ""
        age = (a[8] if len(a) > 8 else "") or ""
        zone = (a[0] if len(a) > 0 else "") or ""
        ref = (a[10] if len(a) > 10 else "") or ""
        out.append({
            "name": name.strip().strip('"'),
            "en_name": en.strip().strip('"'),
            "kind": kind.strip().strip('"'),
            "age": age.strip().strip('"'),
            "zone": zone.strip().strip('"'),
            "ref": ref.strip().strip('"'),
            "coords": f["coords"],
        })
    return out


def clip_faults(faults, lon, lat, radius_km, min_pts=2):
    """按圆形范围裁剪断裂：保留与圆相交的断裂段并裁到边界附近。

    v3.5.0 修：原先只按点做「在 bbox 内」的过滤，再把留下的点**连成一条**
    折线 —— 落在范围外的中间段被抹掉后，前后两段被硬连起来，
    会画出一条不存在的断裂（与 _clip_polyline 的 S1 同类缺陷）。
    现在按**连续性**切分：范围外的点会断开折线，
    每一段连续子折线单独输出。
    """
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
    # 裁剪到 bbox 外扩 20%，保留连续走向
    ex = dlon * 0.2
    ey = dlat * 0.2
    kept = []
    for f in faults:
        pts = f["coords"]
        if not any(bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]
                   for p in pts):
            continue
        # 按连续性切分：范围内的点连成一段，范围外即断开
        runs = []
        cur = []
        for p in pts:
            inside = ((bbox[0] - ex) <= p[0] <= (bbox[2] + ex)
                      and (bbox[1] - ey) <= p[1] <= (bbox[3] + ey))
            if inside:
                cur.append(p)
            else:
                if len(cur) >= min_pts:
                    runs.append(cur)
                cur = []
        if len(cur) >= min_pts:
            runs.append(cur)
        for cl in runs:
            # v1.1.0 修：原先**逐字段**重建 dict，parse_faults 每新增一个
            # 属性（zone / ref）这里就会漏掉一次 —— 属性在「半径裁剪」后
            # 静默消失，而图照出、不报错。改成整字典复制 + 覆写 coords，
            # 从结构上杜绝这类「新增字段被中间步骤吃掉」的缺陷。
            g = dict(f)
            g["coords"] = cl
            kept.append(g)
    return kept


class _EdgeGrid:
    """行政边界边的**均匀网格空间索引**，用于把折线裁剪从 O(段×边) 降到近似 O(段)。

    v3.4.0 性能：原先裁剪要对「每条断裂段 × 每条边界边」逐一求交
    （合肥实测 390 段 × 1079 边 = 42 万次 `_seg_t`，占构建耗时 0.33 s）。
    建一张网格后，每段只需与**同格附近的边**求交，调用量降到千分之一量级。

    正确性：边按**自身 bbox** 覆盖的格插入；查询按**查询 bbox** 覆盖的格取并集。
    两者 bbox 若有重叠，则必存在公共点 p，p 所在格同时在插入集与查询集里，
    因此不会漏掉任何可能相交的边（无漏检）。
    """

    def __init__(self, edges, bbox, target_per_cell=4.0):
        self.edges = edges
        self.xmin, self.ymin, self.xmax, self.ymax = bbox
        n = len(edges)
        # 格子边长按「每格约 target_per_cell 条边」推算，最少 2×2
        dim = max(2, int((n / max(target_per_cell, 1.0)) ** 0.5))
        self.dim = dim
        self.cw = max((self.xmax - self.xmin) / dim, 1e-9)
        self.ch = max((self.ymax - self.ymin) / dim, 1e-9)
        self.cells = {}
        self.big = []          # 跨度极大的边（>64 格），每次查询都带上
        for idx, (a, b) in enumerate(edges):
            cells = self._cells_for(a, b)
            if len(cells) > 64:
                self.big.append(idx)
                continue
            for c in cells:
                self.cells.setdefault(c, []).append(idx)

    def _cell_range(self, x0, y0, x1, y1):
        i0 = int((min(x0, x1) - self.xmin) / self.cw)
        i1 = int((max(x0, x1) - self.xmin) / self.cw)
        j0 = int((min(y0, y1) - self.ymin) / self.ch)
        j1 = int((max(y0, y1) - self.ymin) / self.ch)
        i0 = max(0, min(i0, self.dim - 1))
        i1 = max(0, min(i1, self.dim - 1))
        j0 = max(0, min(j0, self.dim - 1))
        j1 = max(0, min(j1, self.dim - 1))
        return i0, i1, j0, j1

    def _cells_for(self, a, b):
        i0, i1, j0, j1 = self._cell_range(a[0], a[1], b[0], b[1])
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    def query(self, x0, y0, x1, y1):
        """返回与给定 bbox 可能相交的边的下标列表（去重）。"""
        i0, i1, j0, j1 = self._cell_range(x0, y0, x1, y1)
        hit = set(self.big)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                lst = self.cells.get((i, j))
                if lst:
                    hit.update(lst)
        return hit


def _seg_t(a, b, c, d):
    """线段 a→b 与 c→d 的交点参数 t（沿 a→b，落在 [0,1] 内才有效）。

    返回归一化后的 t，或 None（平行 / 不相交）。用于把断裂折线裁到行政边界内。
    """
    x1, y1 = a; x2, y2 = b; x3, y3 = c; x4, y4 = d
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-15:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denom
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return max(0.0, min(1.0, t))
    return None


def _clip_polyline(coords, edges, rings, grid=None, ring_bboxes=None):
    """把一条折线裁到多边形（含空洞）内部，返回若干内部子折线。

    做法：对每一段求它与多边形各边的交点参数 t，按 t 排序后用「中点是否在
    多边形内」判定每一段区间的归属（简单多边形 + 空洞都能正确处理），把内部
    区间拼接成连续子折线。这样断裂即使只是擦过边界（有交集）也会被保留，
    且位于边界外的部分被精确裁掉。

    v3.4.0 性能：传入 grid（_EdgeGrid）时，每段只与**同格附近的边**求交，
    不再对全部边界边做笛卡尔积 —— 合肥实测 42 万次 `_seg_t` 降到约 1 万次。
    不传 grid 时退回原逐边扫描（行为一致，仅慢）。

    v3.5.0 修：原先 `cur` 在段循环外初始化、只在循环末 flush 一次，
    于是「某一段完全在边界外」时，`cur` 仍握着上一段的末点，下一段的
    内部区间会 `cur.append(pa)` 把两段**硬连**起来，在 MultiPolygon
    的空隙上画出一条不存在的断裂（实测：两个不相交方块 + 一条横穿
    间隙的断裂，输出里多出 (10,5)→(20,5) 这条完全在边界外的连线）。
    现在按「连通性」判定：只有真正首尾相接的内部区间才续接，
    其余一律断开成新的子折线。
    """
    if len(coords) < 2:
        return []
    runs = []
    cur = []
    prev_seg = None      # 上一段内区间的所在段索引
    prev_t = None        # 上一段内区间的结束参数 t
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        ts = []
        if grid is not None:
            cand = grid.query(a[0], a[1], b[0], b[1])
            edge_iter = ((edges[k]) for k in cand)
        else:
            edge_iter = iter(edges)
        for (c, d) in edge_iter:
            t = _seg_t(a, b, c, d)
            if t is not None:
                ts.append(t)
        ts = sorted(set(round(t, 9) for t in ts))
        pts = [0.0] + ts + [1.0]
        seg_has_inside = False
        for k in range(len(pts) - 1):
            t0, t1 = pts[k], pts[k + 1]
            if t1 - t0 < 1e-9:
                continue
            tm = (t0 + t1) / 2.0
            xm = a[0] + (b[0] - a[0]) * tm
            ym = a[1] + (b[1] - a[1]) * tm
            if not _point_in_rings(xm, ym, rings, ring_bboxes):
                continue
            pa = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
            pb = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
            # 连通性：同一段内相邻区间（共享端点），或跨段且上一段
            # 一直保持内部直到段末（t1 == 1）并且本段从段首（t0 == 0）开始。
            contiguous = False
            if cur:
                if prev_seg == i and abs(prev_t - t0) < 1e-9:
                    contiguous = True
                elif (prev_seg == i - 1 and prev_t is not None
                      and prev_t >= 1.0 - 1e-9 and t0 <= 1e-9):
                    contiguous = True
            if not contiguous:
                if cur:
                    runs.append(cur)
                cur = [pa, pb]
            else:
                if cur[-1] != pa:
                    cur.append(pa)
                cur.append(pb)
            seg_has_inside = True
            prev_seg = i
            prev_t = t1
        if not seg_has_inside:
            # 本段完全在边界外：断开，下一段必须另起一条子折线
            if cur:
                runs.append(cur)
                cur = []
            prev_seg = None
            prev_t = None
    if cur:
        runs.append(cur)
    return runs


def clip_faults_to_boundary(faults, rings):
    """按行政边界多边形裁剪断裂：只保留与边界有交集的断裂，并裁掉边界外部分。

    v3.4.0 新增 —— 此前断裂按半径 bbox 裁剪（作为区域构造背景），
    会把大量邻市邻省的断裂画进来。现在默认只展示**本市行政边界内**的断裂
    （擦边、跨界同样保留），与地震的「市域内统计」口径一致。

    坐标系：调用时 faults 与 rings 都已是 GCJ-02（与地图底图同坐标系）。
    无边界（--no-boundary）时调用方应回退到 clip_faults 按半径裁剪。
    """
    if not rings:
        return list(faults)
    # 多边形 bbox（快速排斥：整条断裂 bbox 与多边形 bbox 不相交则直接丢弃）
    pbbox = [min(p[0] for p in r) for r in rings] + [max(p[0] for p in r) for r in rings]
    xmin = min(pbbox[: len(rings)])
    xmax = max(pbbox[len(rings):])
    ymin = min(min(p[1] for p in r) for r in rings)
    ymax = max(max(p[1] for p in r) for r in rings)
    edges = []
    for ring in rings:
        if len(ring) < 2:
            continue
        for i in range(len(ring) - 1):
            edges.append((ring[i], ring[i + 1]))
    if not edges:
        return list(faults)
    # v3.4.0 性能：建网格索引，避免「段 × 边」笛卡尔积求交
    grid = _EdgeGrid(edges, (xmin, ymin, xmax, ymax))
    # v3.6.0 性能：逐环 bbox，供 _point_in_rings 剪枝（不改变判定结果）
    ring_bboxes = _ring_bboxes(rings)
    out = []
    for f in faults:
        coords = f.get("coords") or []
        if len(coords) < 2:
            continue
        fxs = [p[0] for p in coords]
        fys = [p[1] for p in coords]
        if (max(fxs) < xmin or min(fxs) > xmax
                or max(fys) < ymin or min(fys) > ymax):
            continue
        for run in _clip_polyline(coords, edges, rings, grid, ring_bboxes):
            if len(run) >= 2:
                g = dict(f)
                g["coords"] = [[round(float(x), FAULT_PREC), round(float(y), FAULT_PREC)]
                               for x, y in run]
                out.append(g)
    return out


# ------------------------------------------------------------------ 统计
def completeness_of(earliest):
    """按「最早有记录的年份」评估目录完整性 —— 纯函数，可单测。

    基于实测规律，非硬编码：ISC-GEM 合并目录对中国的仪器记录约始于
    1960 年代，1970 年后 M>=3 基本完整；此前只有大震级事件被系统编目。
    因此用「首个有记录的年份」判断本图是否落在稀疏区。

    返回 (code, note)。note **按实际年份动态生成** ——
    v1.2.0 修：原来是写死的三句话，与判定区间对不上：合肥 earliest=1917
    落在 [1900,1960) 判为 sparse，却显示「最早记录**晚于 1960 年**」，
    而 1917 明显不晚于 1960，页面上「结论」与「数据」直接打架。
    判据：能算出来的结论就不要写死，写死的文案迟早和判定条件脱节。
    """
    if earliest is None:
        return "no-data", "该范围内无目录记录"
    if earliest < 1900:
        return "full", ("最早记录为 %d 年（早于 1900 年），目录回溯期最长，"
                        "但那时仅有大震被系统编目" % earliest)
    if earliest < 1960:
        return "sparse", ("最早记录为 %d 年（1900–1959 区间），该时期仅较大地震"
                          "被系统编目，中小地震在目录中系统性缺失" % earliest)
    return "modern", ("最早记录为 %d 年（1960 年后），属仪器记录时代，"
                      "完整性较好" % earliest)


def quake_stats(quakes):
    by_mag = OrderedDict()
    for q in quakes:
        b = math.floor(q["mag"])
        by_mag[b] = by_mag.get(b, 0) + 1
    by_year = OrderedDict()
    for q in quakes:
        # v1.2.0 修复：原写法 `if q["time"]:` 用的是**真值判断**，
        # 而 time 是毫秒时间戳 —— 恰好落在 1970-01-01T00:00:00.000 的事件
        # time == 0，是 falsy，会被静默跳过、不计入 by_year 与 earliest。
        # 概率极低但属于「静默丢数据」，与本技能约定冲突。改成显式判 None。
        t = q.get("time")
        if t is None:
            continue
        # Windows 的 time.gmtime 不支持负时间戳（1900 年前），必须用 datetime
        y = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + \
            datetime.timedelta(milliseconds=t)
        y = y.year
        by_year[y] = by_year.get(y, 0) + 1

    # v1.2.0 修复：earliest/latest 原本取 by_year 的**插入顺序**首尾。
    # 插入顺序 = 输入列表顺序，一旦上游合并没重排序（台网近期事件被拼到
    # 历史事件前面），就会算出 earliest > latest 这种自相矛盾的结果，
    # 目录完整性结论随之颠倒。统计语义上「最早」就该是 min，不依赖顺序。
    years = sorted(by_year.keys())
    earliest = years[0] if years else None
    latest = years[-1] if years else None
    completeness, comp_note = completeness_of(earliest)

    # 震级量规分布（v1.1.0）：同一份目录里 mb/mw/ml/ms 混用，
    # 页面必须告诉使用者「这些 M 不是同一把尺子量出来的」。
    mag_types = {}
    for q in quakes:
        mt = (q.get("mag_type") or "").strip()
        mag_types[mt] = mag_types.get(mt, 0) + 1
    mag_types = OrderedDict(sorted(mag_types.items(),
                                   key=lambda kv: (-kv[1], kv[0])))

    return {
        "total": len(quakes),
        "n_cenc": sum(1 for q in quakes if q.get("source") == "CENC"),
        "n_usgs": sum(1 for q in quakes if q.get("source") == "USGS"),
        # 自动测定（未经人工核定）条数：定位与震级精度明显低于 reviewed
        "n_auto": sum(1 for q in quakes
                      if str(q.get("status") or "").lower() == "automatic"),
        # 烈度（仅中国台网 CENC 提供）：取区间内的最大烈度
        "max_intensity": max((q["intensity"] for q in quakes
                              if q.get("intensity") is not None), default=None),
        "mag_types": dict(mag_types),
        "max_mag": max((q["mag"] for q in quakes), default=None),
        "min_mag": min((q["mag"] for q in quakes), default=None),
        "by_mag": dict(sorted(by_mag.items(), reverse=True)),
        "by_year": dict(sorted(by_year.items())),
        "deepest": max((q["depth"] for q in quakes
                        if q["depth"] is not None), default=None),
        "earliest_year": earliest,
        "latest_year": latest,
        "completeness": completeness,
        "completeness_note": comp_note,
    }


def to_gcj_list(coords):
    """整条折线做 WGS84 -> GCJ-02 偏移。

    注意：每个点只能调用一次 to_gcj02。早先写成
    [[round(to_gcj02(...)[0],6), round(to_gcj02(...)[1],6)] ...]
    会导致每个点算两遍（且两次结果必须完全一致才不出错），属隐性冗余。
    """
    out = []
    for p in coords:
        gx, gy = to_gcj02(p[0], p[1])
        out.append([round(gx, 6), round(gy, 6)])
    return out


# ------------------------------------------------ 断裂「性质 / 时代」通俗化
# CAFDv2023 的 AGE 字段是国际地质年代代码（Qh=全新世、Qp*=更新世…），
# 非专业用户看不懂。这里统一翻译成「距今多少年」的直白说法。
# 顺序即新->老，_age_rank 用它判断哪个年代更新。
AGE_TEXT = {
    "Qh": "全新世（约 1 万年前至今）",
    "Qh2": "全新世早段（约 1 万–8 千年前）",
    "Qh3": "全新世晚段（约 8 千–1 万年前）",
    "Qp31": "晚更新世早段（约 8 万–13 万年前）",
    "Qp32": "晚更新世晚段（约 1.3 万–8 万年前）",
    "Qp3": "晚更新世（约 1.3 万–21 万年前）",
    "Qp2": "中更新世（约 21 万–130 万年前）",
    "Qp1+2": "早—中更新世（约 13 万–260 万年前）",
    "Qp1": "早更新世（约 130 万–260 万年前）",
    "Qp": "更新世（约 1.2 万–260 万年前）",
    "Q": "第四纪（约 260 万年前至今）",
    "Pre-Q": "前第四纪（260 万年前，属古老基岩断层）",
    "": "未标注",
}
# 越靠前 = 越新（越晚发生活动）
AGE_ORDER = ["Qh3", "Qh2", "Qh", "Qp32", "Qp31", "Qp3", "Qp2", "Qp1+2",
              "Qp1", "Qp", "Q", "Pre-Q"]

# 断裂「性质」是地质力学分类术语，翻译成「怎么动」的直白描述
KIND_TEXT = {
    "正断": "拉张型（地层被拉长，多为伸展构造）",
    "逆断": "挤压型（地层被压缩并抬升）",
    "走滑": "水平错动型（两侧地块横向相对滑动）",
    "左旋": "水平左旋错动",
    "右旋": "水平右旋错动",
    "左旋逆断": "左旋 + 挤压",
    "右旋逆断": "右旋 + 挤压",
    "左旋正断": "左旋 + 拉张",
    "右旋正断": "右旋 + 拉张",
    "走滑逆断": "水平错动 + 挤压",
    "隐伏正断": "隐伏拉张型（地下未出露）",
    "隐伏性质未知": "隐伏断层，性质不明",
    "推测性质未知": "推测存在，性质不明",
    "出露性质未知": "地表出露，性质不明",
}


def _age_cn(code):
    """地质年代代码 -> 通俗中文。未收录的代码原样返回。"""
    if not code:
        return AGE_TEXT[""]
    return AGE_TEXT.get(code, "%s（地质年代代码）" % code)


def _kind_cn(code):
    """断裂性质 -> 通俗中文。未收录的原样返回。"""
    if not code:
        return "未标注"
    return KIND_TEXT.get(code, code)


def _age_rank(code):
    """年代排序权重：数字越大 = 越新。未收录的排到最后。"""
    try:
        return len(AGE_ORDER) - AGE_ORDER.index(code)
    except ValueError:
        return 0


# --------------------------------------------------------------- HTML 生成
# HTML 模板外置到 scripts/template.html，便于单独调整版式与样式。
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "template.html")


def _load_template():
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        return fh.read()


HTML_TMPL = _load_template()


# ------------------------------------------------ HTML 片段构造（v1.4.0 拆分）
#
# build_html 原本 **281 行、分支复杂度 62** —— 五段互不相干的渲染逻辑
# （震级分档条 / 断裂清单 / 年代直方图 / 地震目录行 / 概览文案）全挤在一个
# 函数体里：改任何一块都要在 281 行里定位，且无法对单块单独断言。
#
# 现在拆成「入参 -> HTML 片段」的**纯函数**：不读全局、不写文件、不依赖
# 调用顺序，每一块都能单独单测。拆分严格保持输出逐字节不变 ——
# 已与 v1.3.0 的产物做过 diff 回归（见 selftest 的 TestV140HtmlRefactor）。
#
# 时间转换也在这里统一：原先 `datetime(1970,1,1,tzinfo=utc) + timedelta(ms=t)`
# 在 quake_stats / build_html / 量纲提示三处各写了一遍，其中两处还各自
# 新建了 EPOCH 常量对象。

EPOCH_UTC = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def ms_to_dt(ms):
    """毫秒时间戳 -> UTC datetime。

    Windows 的 time.gmtime **不支持负时间戳**，1900 年前的目录事件
    （如 1917 年霍山 M6.4，时间戳为负）用它转换会直接抛异常，
    所以全脚本统一走 datetime 加减。
    """
    return EPOCH_UTC + datetime.timedelta(milliseconds=ms)


def ms_to_year(ms):
    """毫秒时间戳 -> 年份（负时间戳也正确）。"""
    return ms_to_dt(ms).year


def mag_rows_html(stats):
    """震级分档条：M3+ / M4+ … 各多少条（条宽按最大档归一化）。"""
    rows = []
    by_mag = stats.get("by_mag") or {}
    maxc = max(by_mag.values()) if by_mag else 1
    for mag, cnt in by_mag.items():
        rows.append(
            '<tr><td>M %d+</td><td class="n">%d</td>'
            '<td style="width:64px"><div class="bar"><i style="width:%d%%"></i></div></td></tr>'
            % (mag, cnt, int(100 * cnt / maxc)))
    return "".join(rows)


def group_faults(faults):
    """按断裂名聚合测绘分段。

    同一条断裂常被拆成多段记录（不同测绘项目各自提交），逐条列会出现
    大量重名。返回 OrderedDict：
        {名称: {"n": 段数, "kind": {性质代码}, "age": {时代代码}}}
    """
    grouped = OrderedDict()
    for f in faults:
        key = f["name"] or "未命名断裂"
        g = grouped.setdefault(key, {"n": 0, "kind": set(), "age": set()})
        g["n"] += 1
        if f["kind"]:
            g["kind"].add(f["kind"])
        if f["age"]:
            g["age"].add(f["age"])
    return grouped


def fault_rows_html(grouped):
    """断裂清单行（段数多的排前面，性质/时代转成通俗中文）。"""
    rows = []
    for name, g in sorted(grouped.items(), key=lambda kv: -kv[1]["n"]):
        kinds = sorted(g["kind"])
        kind = "；".join(_kind_cn(k) for k in kinds) if kinds else "未标注"
        # 同一条断裂的多段可能有不同年代码，取**最新的**（年代数值最大者）
        ages = sorted(g["age"], key=_age_rank)
        age = _age_cn(ages[-1]) if ages else "未标注"
        if len(ages) > 1:
            age += "（%s）" % "、".join(ages)
        seg = ' <span style="color:#6b7784">×%d段</span>' % g["n"] if g["n"] > 1 else ""
        # data-name 供前端「hover 高亮地图上该断裂」（v2.1.0 交互联动）
        rows.append(
            '<tr data-name="%s" title="%s"><td>%s%s</td><td>%s</td><td>%s</td></tr>'
            % (name.replace('"', "&quot;").replace("&", "&amp;"),
               name.replace('"', "&quot;"), name, seg, kind, age))
    return "".join(rows)


def year_histogram_html(by_year):
    """年代直方图（每 10 年一柱）+ X 轴首/中/末三个标签。

    X 轴标签的定位方式（v3.4.0 修复）：原实现用「每桶一个 span，flex:1」，
    但 .hist 本身是 display:flex 的**柱状图容器**，而 .histax 被写在它
    **内部**，于是 histax 成了柱子的同级 flex 项，被挤到只剩 74px 宽，
    三个标签叠成「194019802020」。现在柱子在 .hbars（行 flex），
    轴标签单独一行，用绝对定位的百分比坐标对齐到对应柱子中心。
    """
    years = sorted(by_year or {})
    if not years:
        return ""
    y0, y1 = years[0], years[-1]
    buckets = OrderedDict()
    y = (y0 // 10) * 10
    while y <= y1:
        buckets[y] = 0
        y += 10
    for yr, cnt in (by_year or {}).items():
        buckets[(yr // 10) * 10] += cnt
    mx = max(buckets.values()) or 1
    bars = "".join(
        '<div class="b" style="height:%dpx" title="%d–%d 年：%d 次"></div>'
        % (max(1, int(30 * cnt / mx)), bk, bk + 9, cnt)
        for bk, cnt in buckets.items())
    keys = list(buckets.keys())
    n = len(keys)
    mid = keys[n // 2] if n else None
    axhtml = []
    for i, bk in enumerate(keys):
        if not (i == 0 or bk == mid or i == n - 1):
            continue
        if i == 0:
            style = "left:0;text-align:left;transform:none"
        elif i == n - 1:
            style = "left:100%;text-align:right;transform:translateX(-100%)"
        else:
            style = ("left:%.2f%%;text-align:center;transform:translateX(-50%%)"
                     % ((i + 0.5) * 100.0 / n))
        axhtml.append('<span style="position:absolute;%s">%d</span>' % (style, bk))
    return ('<div class="hbars">%s</div>' % bars
            + '<div class="histax">%s</div>' % "".join(axhtml)
            + '<div style="font-size:10px;color:#6e7681;margin-top:3px;'
              'text-align:right">峰值 %d 次/10年</div>' % mx)


def quake_rows_html(quakes, limit=QTABLE_FIRST_PAGE):
    """地震目录表行 —— 只渲染首页，其余由前端按需生成（v2.0.0）。

    实测汶川 1268 次地震 = 1268 行 × 6 列 ≈ 7600 个 DOM 节点，是整页
    14412 节点里最大的一块，而绝大部分用户根本不会滚到。
    注意：**排序仍在全量数据上进行**，分页只影响渲染条数。
    """
    rows = []
    for qi, q in enumerate(quakes[:limit]):
        t = q.get("time")
        date_s = ms_to_dt(t).strftime("%Y-%m-%d") if t is not None else "—"
        mag = q["mag"]
        mcls = "md6" if mag >= 6 else "md"
        depth = q.get("depth")
        scope_tag = ("市域内" if q.get("in_boundary") is True
                     else "半径内" if q.get("in_boundary") is False else "—")
        src = q.get("source", "USGS")
        src_tag = "台网" if src == "CENC" else "USGS"
        src_cls = "src cenc" if src == "CENC" else "src usgs"
        rows.append(
            '<tr data-i="%d" data-date="%s" data-mag="%s" data-depth="%s" '
            'data-dist="%s" data-scope="%s" data-src="%s" data-lat="%s" '
            'data-lon="%s" title="%s">'
            '<td>%s</td><td class="n %s">%.1f</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="sc">%s</td>'
            '<td class="%s">%s</td></tr>'
            % (qi, date_s, mag, depth if depth is not None else 999,
               q["dist_km"], scope_tag, src_tag, q["lat"], q["lon"],
               (q["place"] or "").replace('"', "'"),
               date_s, mcls, mag,
               ("%.0f" % depth) if depth is not None else "—",
               "%.0f" % q["dist_km"], scope_tag, src_cls, src_tag))
    return "".join(rows)


def scope_text(scope, boundary, radius):
    """统计范围说明（v1.8.0）：返回 (范围说明, 角标短文案)。

    必须**显式**告诉使用者「这张图的数字是哪个范围的」——否则
    「历史地震 12 次」会被误读成「这个城市只发生过 12 次地震」，
    而真实含义是「目录里、市域内、M≥3 的记录有 12 次」。
    """
    scope = scope or {"mode": "radius"}
    if scope.get("mode") == "city":
        note = "%s 行政区域内" % (boundary["name"] if boundary else "本市")
        dropped = (scope.get("radius_total") or 0) - (scope.get("kept") or 0)
        if dropped > 0:
            note += "（已剔除区域外 %d 次）" % dropped
        chip = "市域内 %d / 半径内 %d 次" % (scope.get("kept") or 0,
                                            scope.get("radius_total") or 0)
        return note, chip
    note = "中心 %g km 半径内" % radius
    if scope.get("reason"):
        note += "（%s）" % scope["reason"]
    return note, "半径 %g km 内" % radius


def src_note_text(stats):
    """数据来源说明（双源：CENC 近期 + USGS 历史兜底）。"""
    n_cenc = stats.get("n_cenc", 0)
    n_usgs = stats.get("n_usgs", 0)
    if n_cenc and n_usgs:
        return "国家台网 CENC %d ＋ USGS %d" % (n_cenc, n_usgs)
    if n_cenc:
        return "国家台网 CENC（近期）"
    if n_usgs:
        return "USGS（历史兜底）"
    return "—"


def magscale_text(stats):
    """震级口径说明（v1.1.0）。

    实测合肥 22 条里 mb 11 / mw 5 / ml 4 / ms 1 / mb_Lg 1 —— 五把不同的
    尺子混在同一列「震级」里。不给出口径说明，用户就会以为这些数字
    可以直接横向比较（同一次地震不同量规可差 0.3~0.7 级）。
    """
    bits = []
    for code, cnt in (stats.get("mag_types") or {}).items():
        label = (mag_scale_cn(code) or code) if code else "中国台网测定（未标注量规）"
        bits.append("%s %d 条" % (label, cnt))
    if not bits:
        text = "—"
    elif len(bits) == 1:
        text = bits[0]
    else:
        text = " ／ ".join(bits) + "（量规不同，不宜直接横向比较）"
    # 自动测定（未经人工核定）事件单独提示：其震中/震级误差明显更大
    n_auto = stats.get("n_auto", 0)
    if n_auto:
        text += "　·　含 %d 条自动测定（未人工核定）" % n_auto
    return text


def maxintensity_row_html(stats):
    """最大烈度行（v1.2.0）。

    stats 里算了 max_intensity 却从未渲染 —— 典型的死数据：算了、存进
    data.json、页面上永远看不到。烈度描述「地面震动到底多强」，
    比震级更贴近震害，有价值就该显示。
    仅中国台网 CENC 发布该字段，纯 USGS 历史目录时整行不渲染。
    """
    mi = stats.get("max_intensity")
    if mi is None:
        return ""
    try:
        mi_txt = ("%d" % mi) if float(mi).is_integer() else ("%.1f" % mi)
    except (TypeError, ValueError):
        mi_txt = str(mi)
    return ('<div class="r"><b>最大烈度</b>'
            '<span class="v">%s 度（中国烈度表，仅台网数据）</span></div>'
            % mi_txt)


def yearscale_warn_html(quakes):
    """年代直方图「量纲混用」提示（v1.3.0）。

    v1.4.0：原实现有个 `stats` 形参但**从未使用**（里面那句
    `byy = stats.get("by_year")` 算了却没人读）——拆函数时才暴露出来，
    已连同形参一起删除。

    实测那曲杂多：CENC 只覆盖 **1 天**（2026-10-22 巴青 M7.0 的余震序列），
    却贡献了 2026 年 37 条里的 36 条；而历史各年份每年只有 1~8 条。
    把「1 天的余震」和「历史全年计数」画在同一根轴上，2026 年的柱子会
    异常高耸 —— 看着像地震活动剧增，实际是**两个不同量纲的数据混画**。
    图照出、不报错，是最典型的「静默误导」，必须显式写出来。
    """
    cenc_times = [q["time"] for q in quakes
                  if q.get("source") == "CENC" and q.get("time") is not None]
    if not cenc_times:
        return ""
    span_days = (max(cenc_times) - min(cenc_times)) / 86400000.0
    if span_days >= 90 or len(cenc_times) < 3:
        return ""
    y = ms_to_year(max(cenc_times))
    # 同年 USGS 记录条数（旧实现算了 byy 却没用，是死代码，已删）
    same_year_usgs = sum(
        1 for q in quakes
        if q.get("source") != "CENC" and q.get("time") is not None
        and ms_to_year(q["time"]) == y)
    n_day = max(1, int(round(span_days)) + 1)
    return ('<div class="ywarn">⚠ %d 年柱高异常：其中台网近期 %d 条'
            '仅覆盖 %s，与历史各年的<b>全年</b>计数不是同一量纲，'
            '不代表地震活动增强（该年另有 USGS 记录 %d 条）'
            '</div>' % (y, len(cenc_times),
                        ("%d 天" % n_day) if n_day > 1 else "不足 1 天",
                        same_year_usgs))


def build_html(city, province, radius, min_mag, quakes, faults, stats, center,
               boundary=None, scope=None, fault_scope_note="", fault_clip="radius"):
    """把数据填进模板，产出最终 HTML。

    v1.4.0：渲染逻辑已拆到上面的纯函数里，这里只负责「组装 + 占位符替换」，
    从 281 行降到组装层，每一块渲染都能单独单测。
    """
    grouped = group_faults(faults)
    by_year = stats.get("by_year") or {}
    years = sorted(by_year)
    # v1.4.0：原先取 by_year 的**插入顺序**首尾。by_year 虽已由
    # quake_stats 排好序，但让调用方依赖「上游保证有序」是脆弱契约
    # （v1.2.0 就在这上面栽过一次），这里显式 sorted 不依赖任何前提。
    yrange = ("%d – %d" % (years[0], years[-1])) if years else "—"

    comp = stats.get("completeness", "no-data")
    scope_note, scope_chip = scope_text(scope, boundary, radius)

    repl = {
        "__CITY__": city,
        "__CITYJSON__": json.dumps(city, ensure_ascii=False),
        "__PROVINCE__": province or "—",
        "__RADIUS__": radius,
        "__MINMAG__": min_mag,
        "__TOTAL__": stats["total"],
        "__MAXMAG__": ("%.1f" % stats["max_mag"]) if stats["max_mag"] is not None else "—",
        "__DEEPEST__": ("%.1f" % stats["deepest"]) if stats["deepest"] is not None else "—",
        "__NFAULTS__": len(grouped),
        "__NSEGS__": len(faults),
        "__YEARRANGE__": yrange,
        "__YEARHIST__": (year_histogram_html(by_year)
                         or '<div style="color:#6e7681;font-size:11px">无记录</div>'),
        "__COMPLETE_NOTE__": stats.get("completeness_note", ""),
        # 徽标样式类：good=绿 / modern=蓝 / sparse=红 / 空=中性
        "__COMP_CLS__": {"full": "good", "modern": "modern",
                         "sparse": "sparse"}.get(comp, ""),
        "__SCOPENOTE__": scope_note,
        "__SRCNOTE__": src_note_text(stats),
        "__MAGSCALE__": magscale_text(stats),
        "__MAXINTROW__": maxintensity_row_html(stats),
        "__YEARSCALEWARN__": yearscale_warn_html(quakes),
        "__SCOPECHIP__": scope_chip,
        "__FAULTSCOPENOTE__": fault_scope_note,
        "__BNAME__": (boundary["name"] if boundary else "未加载"),
        "__BLEVELNAME__": (boundary["level_name"] if boundary else "—"),
        "__BSOURCE__": (boundary["source"] if boundary else "—"),
        # 角标/概览面板用短名：全名（如「阿里 DataV.GeoAtlas（areas_v3，
        # 三级行政边界）」）放进地图角标会撑破布局
        "__BSOURCE_SHORT__": ((boundary.get("source_short")
                               or boundary["source"]) if boundary else "—"),
        "__COMPTITLE__": {"full": "历史记录完整",
                          "modern": "属仪器记录时代，完整性较好",
                          "sparse": "存在系统性缺口"}.get(comp, "数据说明"),
        "__BOUNDARY__": json.dumps(
            {"name": boundary["name"], "level_name": boundary["level_name"],
             "source": boundary["source"], "rings": boundary["rings"]}
            if boundary else None, ensure_ascii=False, separators=(",", ":")),
        "__MAGROWS__": mag_rows_html(stats),
        # v1.1.0 修：目录表实为 6 列（日期/震级/深度/距中心/范围/来源），
        # 而这里的空表占位原先写的是 colspan="4"，无数据时会渲染出一条
        # 只占 4 列的错位行（模板里 JS 造的空态已是 6 列，两处不一致）。
        "__QROWS__": (quake_rows_html(quakes)
                      or '<tr><td colspan="6">无记录</td></tr>'),
        "__FAULTROWS__": (fault_rows_html(grouped)
                          or "<tr><td colspan=3>—</td></tr>"),
        # v2.1.0：断裂清单默认收起，summary 行内显示「N 条 · 展开全部」
        "__FAULTS__": json.dumps(faults, ensure_ascii=False, separators=(",", ":")),
        "__QUAKES__": json.dumps(quakes, ensure_ascii=False, separators=(",", ":")),
        "__CENTER__": json.dumps(center),
    }
    html = HTML_TMPL
    for k, v in repl.items():
        html = html.replace(k, str(v))
    return html


# ---- 阶段计时（v3.2.0）：每个阶段的耗时直接打印出来，便于定位瓶颈 ----
_T0 = time.perf_counter()


def step(label):
    """打印上一阶段耗时并重置计时器。"""
    global _T0
    now = time.perf_counter()
    dt = now - _T0
    _T0 = now
    print("      ⏱ %s：%.2f s" % (label, dt))
    return dt


def parse_args(argv=None):
    """CLI 参数定义（与执行逻辑分离，便于单测与复用）。"""
    ap = argparse.ArgumentParser(description="城市活动断层 + 历史地震可视化")
    ap.add_argument("city", help="城市名，如 西安 / 唐山")
    ap.add_argument("--out", default=None, help="输出目录（默认 <city>_map）")
    ap.add_argument("--province", default=None, help="省级消歧，如 陕西")
    ap.add_argument("--radius", type=float, default=200.0, help="搜索半径 km，默认 200")
    ap.add_argument("--min-mag", type=float, default=3.0, help="最小震级，默认 3.0")
    ap.add_argument("--start-year", type=int, default=1900)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--html-name", default=None)
    ap.add_argument("--offline", action="store_true",
                    help="额外输出本地 leaflet.js/css 副本，CDN 不可达时自动回退")
    ap.add_argument("--standalone", action="store_true",
                    help=("产出**单文件** HTML：把 leaflet.js/css 内联进页面，"
                          "断网也能打开，适合直接转发给别人"))
    ap.add_argument("--no-boundary", action="store_true",
                    help="不加载行政边界图层（断层将改按半径裁剪）")
    ap.add_argument("--scope", choices=("city", "radius"), default="city",
                    help=("统计范围：city=只保留行政边界内的地震（默认）；"
                          "radius=保留半径内全部地震"))
    ap.add_argument("--lon", type=float, default=None,
                    help="直接指定经度（WGS84），跳过地名解析")
    ap.add_argument("--lat", type=float, default=None,
                    help="直接指定纬度（WGS84），跳过地名解析")
    return ap.parse_args(argv)


def resolve_center(args):
    """地名 -> WGS84 中心点。返回 (lon, lat, city, province)。

    v1.0.5：不再有地名磁盘缓存 —— 每次实时解析（DataV 索引本地计算，
    实测 0.2 s，缓存省下的时间还不够 Cover 缓存带来的故障模式）。
    """
    if args.lon is not None and args.lat is not None:
        print("  → 使用指定坐标 WGS84 (%.5f, %.5f)" % (args.lon, args.lat))
        return args.lon, args.lat, args.city, (args.province or "")
    try:
        lon, lat, city, province = geocode_city(args.city, args.province)
    except GeocodeError as exc:
        raise SystemExit(str(exc))
    print("  → %s  WGS84 (%.5f, %.5f)" % (city, lon, lat))
    return lon, lat, city, province


def fetch_all(args, lon, lat, province, drop_counts):
    """四路取数并行（v1.0.5）：地震(USGS) / 地震(CENC) / 断层库 / 行政边界。

    四路互不依赖，全部并发；墙钟 = 最慢的一路。
    实测（合肥，本机网关）：断库 16 路分块下载 ~58 s 为主干，
    USGS ~5 s / CENC ~1 s / DataV 边界 ~0.2 s 被完全覆盖，不占墙钟。

    drop_counts 是**剔除量回收容器**：非天然事件被剔了多少条由它带回
    （future.result() 之后读取，与子线程写入之间有同步点，无竞争）。
    返回 (quakes, faults, boundary)。
    """

    def _job_usgs():
        return fetch_earthquakes(lon, lat, args.radius, args.min_mag,
                                 args.start_year, args.end_year,
                                 drop_counts=drop_counts)

    def _job_cenc():
        return fetch_cenc_recent(args.min_mag)

    def _job_faults():
        path = fetch_fault_db()
        allf = parse_faults(path)
        print("  → 全国 %d 条断层，裁剪中 …" % len(allf))
        return clip_faults(allf, lon, lat, args.radius)

    def _job_boundary():
        # 必须用原始输入名 args.city，而不是 geocode 返回的
        # 「省·市」拼接展示名 —— 后者匹配不上行政区划名。
        # 主源：阿里 DataV.GeoAtlas（三级行政边界，免 Key、单要素体积小）。
        # 它本身就是 GCJ-02（数据源为高德），与底图同坐标系，无需转换。
        # 回退：GADM 4.1（WGS84，需逐点偏移；仅在 DataV 不可用时启用）。
        if args.no_boundary:
            return None
        try:
            return boundary_datav.get_boundary(args.city, province, lon, lat,
                                               verbose=False)
        except Exception as exc:  # noqa: BLE001
            print("  [warn] DataV 边界获取失败: %s" % exc, file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_usgs = ex.submit(_job_usgs)
        fut_cenc = ex.submit(_job_cenc)
        fut_fault = ex.submit(_job_faults)
        fut_bnd = ex.submit(_job_boundary)
        quakes = fut_usgs.result()
        try:
            cenc_quakes = fut_cenc.result()
            print("  → [CENC] 国家台网近期目录 %d 条" % len(cenc_quakes))
        except Exception as exc:  # noqa: BLE001
            cenc_quakes = []
            print("  [warn] 国家台网(CENC)取数失败，仅用 USGS：%s" % exc,
                  file=sys.stderr)
        faults = fut_fault.result()
        boundary = fut_bnd.result()

    print("  → USGS 命中 %d 次" % len(quakes))
    # 国家地震台网（CENC）近期目录：近期事件的权威补充。
    # 接口是全国近期列表，先裁剪到半径内，再与 USGS 去重（同事件优先 CENC）。
    if cenc_quakes:
        quakes, merge_info = _merge_cenc_usgs(quakes, cenc_quakes, lon, lat,
                                              args.radius)
        # 台网到底补了几条、挤掉了几条，必须打印：这是判断「双源是否真在工作」
        # 的唯一凭据，静默合并等于无法验证。
        print("  → 双源合并：台网新增 %d 条，去重剔除 USGS 重复 %d 条，合计 %d 条"
              % (merge_info["cenc_added"], merge_info["usgs_dropped_dup"],
                 len(quakes)))
    for q in quakes:
        q.setdefault("source", "USGS")
    print("  → 范围内 %d 条断层" % len(faults))
    return quakes, faults, boundary


def prepare_boundary(args, city, province, lon, lat, boundary):
    """边界就绪：DataV 失败时回退 GADM，并把 WGS84 源统一成 GCJ-02。

    只有 WGS84 源才需要偏移。DataV 已是 GCJ-02，再转一次会二次偏移
    约 400–600 m —— 这是「换了数据源却忘了改坐标处理」最容易踩的坑。
    """
    if boundary is None and not args.no_boundary:
        print("  → 回退 GADM 4.1 …", file=sys.stderr)
        try:
            boundary = find_admin_boundary(args.city, province, lon, lat)
        except Exception as exc:  # noqa: BLE001
            print("  [warn] GADM 边界获取失败: %s" % exc, file=sys.stderr)
    if boundary:
        bname = boundary["name"] or city
        print("  → %s（%s，%s） %d 个环"
              % (bname, boundary["level_name"], boundary["source"],
                 len(boundary["rings"])))
        if boundary.get("crs", "WGS84") != "GCJ-02":
            boundary["rings"] = [to_gcj_list(r) for r in boundary["rings"]]
    else:
        print("  → 边界图层已按 --no-boundary 跳过") if args.no_boundary \
            else print("  → 未找到边界（不影响其它图层）")
    return boundary


def clip_and_mark(faults, quakes, boundary):
    """断层按行政边界裁剪 + 给每条地震打「是否在市域内」标记。

    v3.4.0：断裂默认只展示**市域内**的部分（与地震「市域内统计」口径一致）。
    有边界就用多边形裁剪（保留与边界相交的段，边界外的部分精确裁掉）；
    无边界（--no-boundary）时退回半径 bbox 裁剪。
    返回 (faults, fault_clip)。
    """
    fault_clip = "radius"
    if boundary and boundary.get("rings"):
        n_fault_bbox = len(faults)
        faults = clip_faults_to_boundary(faults, boundary["rings"])
        fault_clip = "city"
        print("  → 市域内（含跨界）%d 条，裁剪前半径内 %d 条"
              % (len(faults), n_fault_bbox))
    else:
        print("  → 半径内 %d 条（未加载行政边界）" % len(faults))

    # 无论 scope 都打标记，便于目录区分内/外
    if boundary and boundary.get("rings"):
        _rbbs = _ring_bboxes(boundary["rings"])
        for q in quakes:
            q["in_boundary"] = bool(
                _point_in_rings(q["lon"], q["lat"], boundary["rings"], _rbbs))
    else:
        for q in quakes:
            q["in_boundary"] = None
    return faults, fault_clip


def compose_scope(args, quakes, boundary):
    """统计范围收敛。返回 (quakes, scope)。

    半径查询只是取数手段，页面要回答的是「这个城市发生过什么地震」，
    所以默认把结果收口到行政边界内。保留半径结果用 --scope=radius。
    """
    if args.scope == "city":
        return filter_quakes_in_boundary(quakes, boundary)
    return quakes, {"mode": "radius", "radius_total": len(quakes),
                    "kept": len(quakes), "inside": None,
                    "reason": "命令行指定 --scope=radius"}


def _payload_bytes(faults, boundary, quakes):
    """产物里几何部分的序列化字节数（压缩比要能被观测，见 slim_products）。"""
    return (len(json.dumps([f["coords"] for f in faults], separators=(",", ":")))
            + len(json.dumps((boundary or {}).get("rings") or [],
                             separators=(",", ":")))
            + len(json.dumps(quakes, separators=(",", ":"))))


def slim_products(faults, quakes, boundary):
    """产物体积压缩（v2.0.0）：降精度 + 抽稀，只影响产物体积，不改语义。

    抽稀会改坐标点集，必须在所有几何（边界 / 断裂 / 地震）都确定后做，
    否则下游的裁剪与端点断言会漂。
    压缩比要打印出来 —— 静默的优化无法验证是否真的生效。
    """
    before = _payload_bytes(faults, boundary, quakes)
    faults = slim_faults(faults)
    quakes = slim_quakes(quakes)
    if boundary:
        boundary = dict(boundary)
        boundary["rings"] = slim_rings(boundary["rings"])
    after = _payload_bytes(faults, boundary, quakes)
    if before:
        print("  → 几何压缩 %.0f KB → %.0f KB（省 %.0f%%）"
              % (before / 1024.0, after / 1024.0,
                 (before - after) * 100.0 / before))
    return faults, quakes, boundary


def write_outputs(args, out_dir, city, province, lon, lat, quakes, faults,
                  boundary, scope, stats, center, html):
    """产物落盘：HTML（可选单文件内联）+ data.json + 可选 leaflet 离线副本。

    返回 (html_path, data_path, offline_files)。
    """
    html_path = os.path.join(out_dir, args.html_name
                             or ("%s_断层地震分布图.html" % city))
    if args.standalone:
        # 单文件自包含：把 leaflet 内联进去（体积 +约 160 KB，断网可用）。
        # 必须在**落盘前**做 —— 先写文件再回改会被后面的写回覆盖掉。
        html, ok = inline_leaflet(html)
        print("  → 单文件自包含：%s（leaflet 已内联）" %
              ("成功" if ok else "失败：缺 leaflet 资源"))
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    data_path = os.path.join(out_dir, "data.json")
    with open(data_path, "w", encoding="utf-8") as fh:
        json.dump({"city": city, "province": province,
                   "center_wgs84": [lon, lat],
                   # v3.5.0 修：center_gcj02 原先直接写 `center`（Leaflet 的
                   # [lat,lon] 序），与同一对象里的 center_wgs84（[lon,lat]）
                   # 坐标序相反，下游读 data.json 必然踩坑。
                   # 现在统一为 [lon, lat]；页面用的 Leaflet 序仍走
                   # __CENTER__ 占位符，不受影响。
                   "center_gcj02": [round(center[1], 6), round(center[0], 6)],
                   "radius_km": args.radius, "min_mag": args.min_mag,
                   "scope": scope, "stats": stats, "quakes": quakes,
                   "faults": faults,
                   # 边界为 GCJ-02 坐标（已转换），与地图显示一致
                   "boundary": ({"name": boundary["name"],
                                 "level": boundary["level"],
                                 "level_name": boundary["level_name"],
                                 "admin": boundary["admin"],
                                 "source": boundary["source"],
                                 "rings": boundary["rings"]}
                                if boundary else None)},
                  fh, ensure_ascii=False, indent=1)

    offline_files = []
    if args.offline:
        ljs, lcss = ensure_leaflet_assets()
        for fname, content in (("leaflet.js", ljs), ("leaflet.css", lcss)):
            if content:
                fp = os.path.join(out_dir, fname)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(content)
                offline_files.append(fp)
    return html_path, data_path, offline_files


def main():
    args = parse_args()
    out_dir = os.path.abspath(args.out or (args.city + "_map"))
    os.makedirs(out_dir, exist_ok=True)

    print("[1/7] 地名解析：%s" % args.city)
    lon, lat, city, province = resolve_center(args)
    step("① 地名解析")

    print("[2/7] 四路取数并行：地震(USGS) / 地震(国家台网CENC) / 断层库 / 行政边界")
    # 剔除量回收容器：非天然事件被剔了多少条，必须打到终端。
    # 不打印就是静默丢数据（见 fetch_earthquakes 的 drop_counts）。
    drop_counts = {}
    quakes, faults, boundary = fetch_all(args, lon, lat, province, drop_counts)
    if drop_counts.get("non_tectonic"):
        print("  → 剔除非天然事件 %d 条（type 非 earthquake，如爆破/矿震）"
              % drop_counts["non_tectonic"])
    step("② 四路取数（并行）")

    print("[3/7] 坐标转换 WGS84 → GCJ-02")
    for f in faults:
        f["coords"] = to_gcj_list(f["coords"])
    for q in quakes:
        gx, gy = to_gcj02(q["lon"], q["lat"])
        q["lon"], q["lat"] = round(gx, 6), round(gy, 6)
    # v1.4.0：原先写成 [to_gcj02(lon,lat)[1], to_gcj02(lon,lat)[0]] ——
    # 同一件事算了两遍，且 [lat,lon] 的换序被埋在下标里看不出来。
    cx, cy = to_gcj02(lon, lat)
    center = [round(cy, 6), round(cx, 6)]
    print("  → 中心 GCJ-02 (%.5f, %.5f)" % (center[1], center[0]))
    step("③ 坐标转换")

    print("[4/7] 行政边界（GADM 回退 / 坐标系核对）")
    boundary = prepare_boundary(args, city, province, lon, lat, boundary)
    step("④ 行政边界")

    print("[5/7] 断层按行政边界裁剪（含跨界）")
    faults, fault_clip = clip_and_mark(faults, quakes, boundary)
    step("⑤ 断层裁剪")

    print("[6/7] 统计范围收敛到行政边界（--scope=%s）" % args.scope)
    quakes, scope = compose_scope(args, quakes, boundary)
    if scope["mode"] == "city":
        print("  → 市域内 %d 次 / 半径内 %d 次（剔除 %d 次区域外事件）"
              % (scope["kept"], scope["radius_total"],
                 scope["radius_total"] - scope["kept"]))
    else:
        print("  → 按圆形半径统计 %d 次%s"
              % (len(quakes),
                 "（%s）" % scope["reason"] if scope["reason"] else ""))
    step("⑥ 范围收敛")

    print("[7/7] 生成 HTML")
    faults, quakes, boundary = slim_products(faults, quakes, boundary)
    stats = quake_stats(quakes)
    fault_scope_note = ("已裁剪至行政边界内（含跨界断层）"
                        if fault_clip == "city"
                        else "半径 %g km（未加载边界，按圆形范围）" % args.radius)
    html = build_html(norm_prov(city), norm_prov(province), args.radius,
                      args.min_mag, quakes, faults, stats, center, boundary,
                      scope, fault_scope_note, fault_clip)
    html_path, data_path, offline_files = write_outputs(
        args, out_dir, city, province, lon, lat, quakes, faults, boundary,
        scope, stats, center, html)
    step("⑦ 渲染与写盘")

    print("\n完成")
    print("  HTML : %s (%.1f KB)" % (html_path, os.path.getsize(html_path) / 1024))
    print("  DATA : %s" % data_path)
    for fp in offline_files:
        print("  OFFLN: %s" % os.path.basename(fp))
    # v3.5.0：终端口径原先是「有名字的断裂数」，页面口径是
    # 「分组数（含未命名断裂）」—— 同一份产物两处数字对不上
    # （实测终端 12 条 / 页面 13 条）。现在统一为页面口径：
    # 未命名断裂也算一条（它在清单里就是一行）。
    n_named = len({f["name"] or "未命名断裂" for f in faults})
    print("  地震 %d 次 / 断层 %d 条（%d 段） / 最大震级 %s"
          % (len(quakes), n_named, len(faults),
             ("%.1f" % stats["max_mag"]) if stats["max_mag"] is not None else "—"))
    print("  目录完整性: %s" % (stats.get("completeness_note") or "—"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
