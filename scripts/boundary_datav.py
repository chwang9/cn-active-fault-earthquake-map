# -*- coding: utf-8 -*-
"""阿里 DataV.GeoAtlas 中国三级行政边界（免 Key）。

接口
----
- ``areas_v3/bound/all.json``      —— 全国行政区划索引（省/市/区县级 3238 条）
- ``areas_v3/bound/{adcode}.json`` —— 该行政区**自身**的边界（单个要素）

坐标系
------
**DataV.GeoAtlas 的数据源是高德开放平台，坐标为 GCJ-02**（火星坐标），
与本技能使用的高德卫星瓦片坐标系一致，因此**不需要再做偏移转换**。
（这是它相对 GADM 的最大优势：GADM 是 WGS84，必须逐点转换。）

更细一级（``{adcode}_full.json``）返回的是**其下级**全部子级边界，
例如唐山市（130200）的 full 返回 14 个区；县级没有下级会返回 404，
所以取边界一律用 ``{adcode}.json``。

许可
----
DataV 页面声明「数据来源于高德开放平台，仅供学习交流使用」，
本技能仅作个人研究用途，不可再分发。
"""
import json
import sys
import time
import urllib.request

# ---- 取数策略（v1.0.5：不落磁盘缓存）----
#
# 设计决策（用户明确要求「不要缓存」）：本模块不再有缓存目录，
# 每次运行实时下载索引与边界要素；临时文件由 build_map 的一次性
# 临时目录承载，进程退出即删。
#
# 唯一保留的是**进程内单次复用**（_INDEX_MEMO）：一次运行里
# geocode_city 与 get_boundary 都要用全国索引（340 KB），
# 不记忆就会下载两遍。这只是同一进程内的变量，不跨运行、
# 不落盘，与「缓存」不是一回事。
INDEX_URL = "https://geo.datav.aliyun.com/areas_v3/bound/all.json"
FEAT_URL = "https://geo.datav.aliyun.com/areas_v3/bound/%s.json"

_INDEX_MEMO = None

SOURCE_NAME = "阿里 DataV.GeoAtlas（areas_v3，三级行政边界）"
SOURCE_SHORT = "DataV.GeoAtlas"
CRS = "GCJ-02"

# 层级 → 中文名
LEVEL_CN = {"province": "省级", "city": "地级市", "district": "县级",
            "country": "国家级"}

# 用于归一化时剥离的后缀。注意只做「双向精确匹配」，
# 不做「前缀包含」—— 否则「西安」会命中「西安新城区」这类问题重现。
_SUFFIXES = (
    "维吾尔自治区", "壮族自治区", "回族自治区", "特别行政区",
    "自治州", "自治县", "自治区", "地区", "盟",
    "市", "县", "区", "旗", "省",
)


# ---------------------------------------------------------------- 下载

def _download(url, timeout=90, retries=3):
    """GET 下载 + 重试。

    本机网关对大响应会间歇 504，且POST 一律被拦，所以只走 GET。
    """
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "cn-active-fault-earthquake-map/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            # 5xx 短等，指数退避（本机实测 3 次内成功率 >95%）
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("下载失败 %s: %s" % (url, last))


def ensure_index(force=False):
    """下载全国行政区划索引（340 KB，3238 条省/市/区县）。

    v1.0.5 起不落磁盘缓存：每次运行实时下载（实测 ~0.2 s）。
    `force=True` 或进程内尚未下载过时才真正发起请求；
    同一进程内的重复调用直接复用已下载的索引（见 _INDEX_MEMO）。
    """
    global _INDEX_MEMO
    if not force and _INDEX_MEMO is not None:
        return _INDEX_MEMO
    print("  [fetch] DataV 行政区划索引 …")
    raw = _download(INDEX_URL)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list) or len(data) < 1000:
        raise RuntimeError("DataV 索引格式异常：%r" % type(data))
    _INDEX_MEMO = data
    return data


def fetch_feature(adcode):
    """取某个 adcode 的边界要素（含几何）。v1.0.5 起不落磁盘缓存。"""
    raw = _download(FEAT_URL % adcode)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- 名称匹配

def _strip_suffix(name):
    s = (name or "").strip()
    for suf in _SUFFIXES:
        if len(s) > len(suf) and s.endswith(suf):
            return s[:-len(suf)]
    return s


def _name_match(want, cand):
    """名称匹配打分（0 = 不匹配）。

    **只做双向精确匹配**，不做前缀包含：
    「西安」不应命中「西安市长安区」。需要模糊时靠坐标距离决胜。
    """
    if not want or not cand:
        return 0
    w, c = want.strip(), cand.strip()
    if w == c:
        return 100
    ws, cs = _strip_suffix(w), _strip_suffix(c)
    if ws and ws == cs:
        return 90
    if ws and cs and (ws == c or cs == w):
        return 80
    return 0


def resolve_adcode(city, province=None, lon=None, lat=None, index=None):
    """把城市名解析成 adcode 候选列表（按可信度降序）。

    参数
    ----
    city      : 用户输入，如「唐山」「汶川县」
    province  : 省级消歧（可选）
    lon / lat : 中心坐标（WGS84）。有值时用它对同名候选做距离决胜，
                避免「城口县」「城口」这类歧义。

    返回
    ----
    list of dict: {adcode, name, level, center, dist_deg}
    """
    if index is None:
        index = ensure_index()
    want = (city or "").strip()

    # 省级过滤：DataV 的 parent 链是「县 → 地级市 → 省」，
    # **县级的直接 parent 是地级市 adcode，不是省**。
    # 只比对直接 parent 会让「省 + 县」组合永远匹配不上，
    # 且是静默失效（回落 GADM 而不报错）。
    prov_codes = set()
    if province:
        pw = _strip_suffix(province)
        for it in index:
            if it.get("level") == "province" and \
                    (_strip_suffix(it["name"]) == pw or it["name"] == province):
                prov_codes.add(it["adcode"])

    # adcode → 省级 adcode（沿 parent 链上溯，深度 ≤ 3）
    prov_of = {}
    by_code = {it["adcode"]: it for it in index}

    def _province_of(code):
        if code in prov_of:
            return prov_of[code]
        cur = code
        seen = set()
        result = None
        while cur and cur not in seen:
            seen.add(cur)
            it = by_code.get(cur)
            if it is None:
                break
            if it.get("level") == "province":
                result = cur
                break
            cur = it.get("parent")
        prov_of[code] = result
        return result

    cands = []
    for it in index:
        lv = it.get("level")
        if lv not in ("city", "district"):
            continue
        if prov_codes and _province_of(it["adcode"]) not in prov_codes:
            continue
        sc = _name_match(want, it.get("name"))
        if sc == 0:
            continue
        rec = dict(it)
        rec["score"] = sc
        # 索引条目只有 lng/lat，没有 center —— 坐标决胜必须用这两个键，
        # 否则所有候选 dist 恒为 1e9，坐标决胜形同虚设。
        if it.get("lng") is not None and it.get("lat") is not None:
            rec["center"] = (it["lng"], it["lat"])
        cands.append(rec)

    if not cands:
        return []

    # 同名候选：用中心坐标距离决胜（500 m 级的 GCJ/WGS 差异不影响排序）
    if lon is not None and lat is not None:
        for c in cands:
            ctr = c.get("center")
            c["dist"] = ((ctr[0] - lon) ** 2 + (ctr[1] - lat) ** 2) ** 0.5 \
                if ctr else 1e9
    else:
        for c in cands:
            c["dist"] = 0.0

    # 级别优先：用户给「XX县」时县级更可能；给「XX市」时地级市更可能。
    # 靠 score 区分（完全同名 100 > 剥后缀 90），这里再叠一层级别偏好。
    lv_pref = 0
    if want.endswith(("县", "自治县")):
        lv_pref = 1          # 偏好 district
    for c in cands:
        c["pref"] = 1 if (c["level"] == "district") == bool(lv_pref) else 0

    cands.sort(key=lambda c: (-c["score"], -c["pref"], c["dist"]))
    return cands


# ---------------------------------------------------------------- 几何

def _iter_rings(geom):
    """从 GeoJSON geometry 抽出全部环（Polygon / MultiPolygon，含内环/洞）。"""
    if not geom:
        return []
    t = geom.get("type")
    co = geom.get("coordinates") or []
    out = []
    if t == "Polygon":
        for ring in co:
            if ring and len(ring) >= 3:
                out.append(ring)
    elif t == "MultiPolygon":
        for poly in co:
            for ring in poly:
                if ring and len(ring) >= 3:
                    out.append(ring)
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or []:
            out.extend(_iter_rings(g))
    return out


def _find_by_adcode(index, adcode):
    for it in index:
        if it.get("adcode") == adcode:
            return it
    return None


def _province_name(index, adcode):
    """沿 parent 链取省级名。"""
    cur = _find_by_adcode(index, adcode)
    guard = 0
    while cur and guard < 5:
        if cur.get("level") == "province":
            return cur.get("name") or ""
        parent = cur.get("parent")
        if not parent:
            break
        cur = _find_by_adcode(index, parent)
        guard += 1
    return ""


def get_boundary(city, province=None, lon=None, lat=None, verbose=True):
    """取城市行政边界。

    返回 dict: {name, level, level_name, admin, source, crs, adcode, rings}
    找不到返回 None（由调用方决定是否回退到 GADM）。
    """
    try:
        index = ensure_index()
    except Exception as exc:  # noqa: BLE001
        print("  [warn] DataV 索引获取失败: %s" % exc, file=sys.stderr)
        return None

    cands = resolve_adcode(city, province, lon, lat, index)
    if not cands:
        if verbose:
            print("  [warn] DataV 未匹配到「%s」的行政区划" % city,
                  file=sys.stderr)
        return None

    for c in cands[:4]:          # 同名候选逐个尝试，容忍单个下载失败
        try:
            data = fetch_feature(c["adcode"])
        except Exception as exc:  # noqa: BLE001
            print("  [warn] DataV 边界下载失败 %s: %s" % (c["adcode"], exc),
                  file=sys.stderr)
            continue
        feats = data.get("features") or []
        if not feats:
            continue
        rings = [r for r in _iter_rings(feats[0].get("geometry")) if len(r) >= 3]
        if not rings:
            continue
        if verbose:
            print("  → %s（%s，DataV adcode %d）%d 个环"
                  % (c["name"], LEVEL_CN.get(c["level"], c["level"]),
                     c["adcode"], len(rings)))
        return {
            "name": c["name"],
            "level": {"city": 2, "district": 3}.get(c["level"], 0),
            "level_name": LEVEL_CN.get(c["level"], c["level"]),
            "admin": _province_name(index, c["adcode"]),
            "source": SOURCE_NAME,
            # 短名：地图角标/概览面板用，全名太长会撑破布局
            "source_short": SOURCE_SHORT,
            # 关键：DataV 本身就是 GCJ-02，与高德底图一致，调用方不要再转
            "crs": CRS,
            "adcode": c["adcode"],
            "rings": rings,
        }

    if verbose:
        print("  [warn] DataV 候选边界几何均为空: %s" % city, file=sys.stderr)
    return None