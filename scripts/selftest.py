#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-active-fault-earthquake-map 自检脚本

覆盖 SKILL.md 中承诺的关键行为，重点是那些曾经真实踩过的坑：
  1. GCJ-02 偏移算法对国测局公认校验点
  2. Windows 负时间戳（1900 年前历史地震）处理
  3. GMT 段式文本解析与字段索引
  4. 圆形距离筛选
  5. HTML 模板渲染（占位符全部替换、数据块为合法 JSON）
  6. Leaflet tooltip 用法（不得回退到 L.tooltip(opts, text)）
"""
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map as bm  # noqa: E402


class TestGCJ02(unittest.TestCase):
    """国测局 WGS84 -> GCJ-02 公认校验点。"""

    def test_standard_checkpoints(self):
        cases = [
            ((116.404, 39.915), (116.410244, 39.916404)),
            ((116.397428, 39.90923), (116.403672, 39.910634)),
            ((114.065736, 22.548785), (114.070858, 22.546081)),
        ]
        for (lon, lat), (elon, elat) in cases:
            glon, glat = bm.to_gcj02(lon, lat)
            self.assertAlmostEqual(glon, elon, places=5,
                                   msg="经度偏移错误 for %s" % ((lon, lat),))
            self.assertAlmostEqual(glat, elat, places=5,
                                   msg="纬度偏移错误 for %s" % ((lon, lat),))

    def test_offset_magnitude(self):
        """偏移量应在 400-600 m 量级，太小说明算法没生效。"""
        glon, glat = bm.to_gcj02(116.397428, 39.90923)
        import math
        d = math.hypot((glon - 116.397428) * 85000, (glat - 39.90923) * 111000)
        self.assertTrue(300 < d < 800, "偏移量异常: %.0f m" % d)

    def test_out_of_china_passthrough(self):
        lon, lat = bm.to_gcj02(-74.0, 40.7)  # 纽约，应原样返回
        self.assertAlmostEqual(lon, -74.0, places=9)
        self.assertAlmostEqual(lat, 40.7, places=9)


class TestHaversine(unittest.TestCase):
    def test_known_distance(self):
        # 西安 -> 北京 约 910 km
        d = bm.haversine_km(108.92861, 34.25833, 116.39723, 39.90750)
        self.assertTrue(880 < d < 940, "距离计算偏差: %.0f km" % d)

    def test_zero_distance(self):
        self.assertAlmostEqual(
            bm.haversine_km(108.9, 34.2, 108.9, 34.2), 0.0, places=6)


class TestQuakeStats(unittest.TestCase):
    def test_negative_timestamp_1900(self):
        """Windows time.gmtime 不支持负时间戳，1900 年前事件必须能统计。"""
        pre1900 = -2208988800000  # 1900-01-01
        quakes = [{"time": pre1900, "mag": 5.0, "depth": 10.0},
                  {"time": 1000000000000, "mag": 4.0, "depth": 12.0}]
        stats = bm.quake_stats(quakes)
        self.assertIn(1900, stats["by_year"], "1900 年事件未统计到")
        self.assertEqual(stats["total"], 2)
        self.assertAlmostEqual(stats["max_mag"], 5.0)

    def test_by_mag_buckets(self):
        quakes = [{"time": 0, "mag": 3.2, "depth": 1},
                  {"time": 0, "mag": 3.9, "depth": 1},
                  {"time": 0, "mag": 5.1, "depth": 1}]
        stats = bm.quake_stats(quakes)
        self.assertEqual(stats["by_mag"], {5: 1, 3: 2})

    def test_empty(self):
        stats = bm.quake_stats([])
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["max_mag"])


class TestFaultParsing(unittest.TestCase):
    """用真实 CN-faults.gmt 格式的最小合成样本验证解析。"""

    SAMPLE = (
        "# @VGMT1.0 @GLINESTRING\n"
        "# @NFZN_Ch|FZN_En|FN_Ch|FN_En|FSN_Ch|FSN_En|Fea_Ch|Fea_En|AGE|AGE_Des|RefE\n"
        "# FEATURE_DATA\n"
        ">\n"
        '# @D||招远—黄墩断裂|"Zhaoyuan-huangdun fault"|||正断|normal|Qp1+2|"\n'
        '"middle-early Pleistocene"|UAFS:Qingdao\n'
        "119.237289736 35.5097536800001 0\n"
        "119.277799736 35.5537116800001 0\n"
        "119.318480736 35.6178926800001 0\n"
        ">\n"
        '# @D||百尺河断裂|"Baichihe fault"|||正断|normal|Qp1+2|"\n'
        '"middle-early Pleistocene"|UAFS:Qingdao\n'
        "119.360213473 36.1385560290001 0\n"
        "119.596289331 36.1230756450001 0\n"
        ">\n"
        "# @D||||||正断|normal|Pre-Q|pre-Quaternary|\"SMCAR\"\n"
        "100.0 20.0 0\n"
        "100.1 20.1 0\n"
    )

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".gmt",
                                              delete=False, encoding="utf-8")
        self.tmp.write(self.SAMPLE)
        self.tmp.close()
        self.faults = bm.parse_faults(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_feature_count(self):
        self.assertEqual(len(self.faults), 3)

    def test_field_indices(self):
        """SKILL.md 承诺：中文名 index 2、英文名 index 3、性质 index 6、时代 index 8。"""
        f = self.faults[0]
        self.assertEqual(f["name"], "招远—黄墩断裂")
        self.assertEqual(f["en_name"], "Zhaoyuan-huangdun fault")
        self.assertEqual(f["kind"], "正断")
        self.assertEqual(f["age"], "Qp1+2")

    def test_coords(self):
        self.assertEqual(len(self.faults[0]["coords"]), 3)
        self.assertAlmostEqual(self.faults[0]["coords"][0][0], 119.237289736, places=8)

    def test_unnamed_feature_still_parsed(self):
        self.assertEqual(len(self.faults[2]["coords"]), 2)
        self.assertEqual(self.faults[2]["name"], "")


class TestClipFaults(unittest.TestCase):
    def test_clip_by_bbox(self):
        faults = [
            {"name": "近", "en_name": "", "kind": "逆断", "age": "Q",
             "coords": [[116.0, 39.0], [116.1, 39.1]]},
            {"name": "远", "en_name": "", "kind": "逆断", "age": "Q",
             "coords": [[100.0, 20.0], [100.1, 20.1]]},
        ]
        kept = bm.clip_faults(faults, 116.0, 39.0, 50.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "近")

    def test_min_points_filter(self):
        """只有 1 个点的断裂不应保留。"""
        faults = [{"name": "点", "en_name": "", "kind": "", "age": "",
                   "coords": [[116.0, 39.0]]}]
        self.assertEqual(bm.clip_faults(faults, 116.0, 39.0, 50.0), [])

    def test_clip_to_boundary_keeps_inside_and_crossing(self):
        """v3.4.0：断裂按行政边界多边形裁剪，保留市域内（含跨界）部分。"""
        rings = [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]]
        # 完全在内部
        inside = {"name": "内", "en_name": "", "kind": "正断", "age": "Qh",
                  "coords": [[2.0, 2.0], [3.0, 3.0]]}
        # 跨界穿过（一半在内一半在外）
        cross = {"name": "界", "en_name": "", "kind": "逆断", "age": "Qp3",
                 "coords": [[-1.0, 5.0], [5.0, 5.0]]}
        # 完全在外
        outside = {"name": "外", "en_name": "", "kind": "走滑", "age": "Pre-Q",
                   "coords": [[20.0, 20.0], [21.0, 21.0]]}
        out = bm.clip_faults_to_boundary([inside, cross, outside], rings)
        names = sorted(f["name"] for f in out)
        self.assertEqual(names, ["内", "界"], "应保留市域内与跨界断裂，丢弃市域外")
        # 跨界断裂应被裁成落在边界内的子段
        cross_out = [f for f in out if f["name"] == "界"][0]
        for p in cross_out["coords"]:
            self.assertTrue(0.0 <= p[0] <= 10.0 and 0.0 <= p[1] <= 10.0,
                            "跨界断裂的裁剪结果应全部落在边界内: %s" % p)


class TestHTMLRender(unittest.TestCase):
    def setUp(self):
        quakes = [{"id": "x", "lon": 108.9, "lat": 34.2, "depth": 10.0,
                   "mag": 5.5, "place": "test", "time": 1000000000000,
                   "url": "http://x", "dist_km": 1.0, "type": "earthquake"}]
        faults = [{"name": "测试断裂", "en_name": "Test", "kind": "逆断",
                   "age": "Q1", "coords": [[108.9, 34.2], [109.0, 34.3]]}]
        stats = bm.quake_stats(quakes)
        self.html = bm.build_html("测试市", "陕西", 200, 3.0, quakes,
                                  faults, stats, [34.2, 108.9])

    def test_no_placeholder_left(self):
        import re
        left = re.findall(r"__[A-Z]+__", self.html)
        self.assertEqual(left, [], "未替换的占位符: %s" % set(left))

    def test_data_blocks_are_valid_json(self):
        import re
        for var in ("FAULTS", "QUAKES", "CENTER"):
            m = re.search(r"var %s\s*=\s*(.*?);\n" % var, self.html, re.S)
            self.assertIsNotNone(m, "变量 %s 未找到" % var)
            json.loads(m.group(1))  # 非法则抛异常

    def test_tooltip_uses_setcontent(self):
        """L.tooltip 的第二参是图层不是文本，回归防护。"""
        self.assertNotIn("L.tooltip({permanent:true, direction:'center', className:'maglab'}, label)",
                         self.html)
        self.assertIn(".setContent(label)", self.html)

    def test_has_leaflet_and_amap(self):
        self.assertIn("leaflet@1.9.4", self.html)
        self.assertIn("is.autonavi.com/appmaptile", self.html)

    def test_all_faults_listed(self):
        self.assertIn("测试断裂", self.html)


class TestProvinceNormalize(unittest.TestCase):
    def test_suffix_stripping(self):
        cases = [("河北省", "河北"), ("四川省", "四川"), ("内蒙古自治区", "内蒙古"),
                 ("广西壮族自治区", "广西"), ("北京市", "北京"),
                 ("上海市", "上海"), ("新疆维吾尔自治区", "新疆"),
                 ("香港特别行政区", "香港"), ("西藏自治区", "西藏")]
        for raw, want in cases:
            self.assertEqual(bm.norm_prov(raw), want, "归一化失败: %s" % raw)

    def test_empty(self):
        self.assertEqual(bm.norm_prov(None), "")
        self.assertEqual(bm.norm_prov(""), "")


class TestGeocodeScoring(unittest.TestCase):
    """离线复现真实返回的消歧打分，回归防护。

    实测：短名「唐山」只命中村镇级 PPL（四川/湖北/山东…），
    河北的「唐山市」是 PPLA2，短名根本匹配不到，必须靠「市」变体 + 省份加权。
    """

    def _score(self, r, city, want):
        s = 0
        rname = r.get("name") or ""
        if rname == city:
            s += 100
        elif rname == city + "市":
            s += 95
        elif rname.startswith(city):
            s += 60
        if want:
            if bm.norm_prov(r.get("admin1")) == want:
                s += 120
            if bm.norm_prov(r.get("admin2")) == want:
                s += 40
        fc = r.get("feature_code")
        if fc == "PPLC":
            s += 60
        elif fc in ("PPLA", "PPLA2", "PPLA3", "PPLA4"):
            s += 45
        elif fc == "PPL":
            s += 12
        if r.get("population"):
            s += min(25, r["population"] / 1_000_000)
        return s

    def test_tangshan_resolves_to_hebei(self):
        cand = [
            {"name": "唐山", "admin1": "四川", "feature_code": "PPL",
             "latitude": 30.44083, "longitude": 103.52312, "population": None},
            {"name": "唐山", "admin1": "山东", "feature_code": "PPL",
             "latitude": 37.1441, "longitude": 120.83658, "population": None},
            {"name": "唐山市", "admin1": "河北省", "feature_code": "PPLA2",
             "latitude": 39.64381, "longitude": 118.18319, "population": 3372102},
        ]
        best = max(cand, key=lambda r: self._score(r, "唐山", "河北"))
        self.assertEqual(best["admin1"], "河北省")
        self.assertAlmostEqual(best["latitude"], 39.64381, places=4)

    def test_province_weight_beats_population(self):
        """指定省份时，省份权重必须压过人口权重。"""
        big = {"name": "城", "admin1": "河南省", "feature_code": "PPLA",
               "latitude": 34.0, "longitude": 113.0, "population": 9000000}
        want = {"name": "城", "admin1": "河北省", "feature_code": "PPLA",
                "latitude": 38.0, "longitude": 114.0, "population": 1000000}
        best = max([big, want], key=lambda r: self._score(r, "城", "河北"))
        self.assertEqual(best["admin1"], "河北省")

    def test_xian_default(self):
        cand = [{"name": "西安", "admin1": "陕西", "feature_code": "PPLA",
                 "latitude": 34.25833, "longitude": 108.92861, "population": 12952907}]
        best = max(cand, key=lambda r: self._score(r, "西安", None))
        self.assertAlmostEqual(best["longitude"], 108.92861, places=5)


class TestCompleteness(unittest.TestCase):
    """目录完整性判定：让输出自曝数据可信度，而不是静默给出残缺统计。"""

    @staticmethod
    def _ts(year):
        """某年 1 月 1 日 UTC 的毫秒时间戳。"""
        return int((datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
                    - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
                    ).total_seconds() * 1000)

    def _mk(self, years):
        return [{"time": self._ts(y), "mag": 4.0, "depth": 10.0}
                for y in years]

    def test_sparse_before_1960(self):
        stats = bm.quake_stats(self._mk([1940, 1950, 1955]))
        self.assertEqual(stats["completeness"], "sparse")
        # v1.2.0：本断言原先写 assertIn("1960", note)，守的是「旧硬编码文案里
        # 恰好有 1960 这个字面量」——那句文案说「最早记录晚于 1960 年」，
        # 可实际 earliest=1940，并不晚于 1960，页面结论与数据打架。
        # 文案改为按实际年份动态生成后，断言也要改守真正重要的语义：
        # ① sparse 必须点明「中小地震系统性缺失」；② 年份必须与实际一致。
        self.assertIn("中小地震", stats["completeness_note"])
        self.assertIn("1940", stats["completeness_note"])

    def test_modern(self):
        stats = bm.quake_stats(self._mk([1980, 1990, 2000]))
        self.assertEqual(stats["completeness"], "modern")

    def test_full(self):
        stats = bm.quake_stats(self._mk([1850, 1900, 1950]))
        self.assertEqual(stats["completeness"], "full")

    def test_no_data(self):
        stats = bm.quake_stats([])
        self.assertEqual(stats["completeness"], "no-data")

    def test_year_span_fields(self):
        stats = bm.quake_stats(self._mk([1990, 2000, 2010]))
        self.assertEqual(stats["earliest_year"], 1990)
        self.assertEqual(stats["latest_year"], 2010)


class TestToGCJListSingleCall(unittest.TestCase):
    """to_gcj_list 每个点只应调用一次 to_gcj02（早先实现算了两遍）。"""

    def test_calls_once_per_point(self):
        calls = []
        orig = bm.to_gcj02

        def counting(lon, lat):
            calls.append((lon, lat))
            return orig(lon, lat)

        bm.to_gcj02 = counting
        try:
            pts = [[116.0, 39.0], [116.1, 39.1], [116.2, 39.2]]
            out = bm.to_gcj_list(pts)
        finally:
            bm.to_gcj02 = orig
        self.assertEqual(len(calls), len(pts),
                         "to_gcj02 被调用 %d 次，应为 %d 次" % (len(calls), len(pts)))
        self.assertEqual(len(out), len(pts))

    def test_output_matches(self):
        pts = [[116.397428, 39.90923], [108.92861, 34.25833]]
        out = bm.to_gcj_list(pts)
        for p, o in zip(pts, out):
            self.assertEqual(o, [round(bm.to_gcj02(p[0], p[1])[0], 6),
                                 round(bm.to_gcj02(p[0], p[1])[1], 6)])

    def test_output_rounded_to_six(self):
        """to_gcj_list 必须 round 到 6 位（锁体积，防全精度泄漏）。

        上面的 test_output_matches 用 round(x,6) 做比较对象，对**本身就是
        6 位的值**是恒等的 —— 变异验证实测：把 round 删掉后它照样通过。
        所以这里用一个会产出十几位小数的输入，直接量小数位数。
        """
        out = bm.to_gcj_list([[100.123456789, 25.987654321]])
        for v in out[0]:
            s = repr(float(v))
            n = len(s.split(".")[1]) if "." in s else 0
            self.assertLessEqual(n, 6,
                                 "to_gcj_list 未 round 到 6 位（小数 %d 位）" % n)


class TestQuakeTable(unittest.TestCase):
    """地震目录表：用户明确要求「列举所有 M>3 地震」，必须成表可查。"""

    def setUp(self):
        quakes = [
            {"id": "a", "lon": 118.2, "lat": 39.6, "depth": 23.0, "mag": 7.5,
             "place": "唐山", "time": 235144800000, "url": "u", "dist_km": 19.4,
             "in_boundary": True, "source": "USGS", "type": "earthquake"},
            {"id": "b", "lon": 118.3, "lat": 39.7, "depth": None, "mag": 4.0,
             "place": "无深度", "time": 1451520000000, "url": None,
             "dist_km": 25.0, "in_boundary": False, "source": "CENC",
             "type": "earthquake"},
        ]
        stats = bm.quake_stats(quakes)
        self.html = bm.build_html("测试", "河北", 200, 3.0, quakes, [], stats,
                                  [39.6, 118.2])

    def test_table_present(self):
        self.assertIn('id="qtable"', self.html)
        self.assertNotIn("__QROWS__", self.html)  # 占位符必须已被替换

    def test_row_count(self):
        import re
        self.assertEqual(len(re.findall(r'<tr data-i="\d+" data-date=', self.html)),
                         2)

    def test_row_has_data_i_for_filter(self):
        """目录表行必须带 data-i，否则前端筛选器无法联动隐藏行。"""
        idx = [int(m) for m in
               re.findall(r'<tr data-i="(\d+)" data-date=', self.html)]
        self.assertEqual(idx, [0, 1], "data-i 必须按 quakeItems 顺序连续编号")

    def test_row_has_sort_attributes(self):
        self.assertIn('data-mag="7.5"', self.html)
        self.assertIn('data-lat="39.6"', self.html)

    def test_missing_depth_handled(self):
        """深度为 null 时不能写出 'None'。"""
        self.assertNotIn(">None<", self.html)

    def test_sort_handlers_present(self):
        self.assertIn("data-k=\"mag\"", self.html)
        self.assertIn("sortRows", self.html)

    def test_scope_column_present(self):
        """地震目录必须含「范围」列，区分市域内/半径内。"""
        self.assertIn("data-k=\"scope\"", self.html,
                      "目录缺少范围列（data-k=scope）")
        self.assertIn("市域内", self.html, "未渲染市域内标记")
        self.assertIn("半径内", self.html, "未渲染半径内标记")
        self.assertIn("data-scope=\"市域内\"", self.html)
        self.assertIn("data-scope=\"半径内\"", self.html)

    def test_source_column_present(self):
        """地震目录必须含「来源」列，标注 CENC / USGS。"""
        self.assertIn("data-k=\"src\"", self.html,
                      "目录缺少来源列（data-k=src）")
        self.assertIn("data-src=\"台网\"", self.html, "未渲染 CENC 台网标记")
        self.assertIn("data-src=\"USGS\"", self.html, "未渲染 USGS 标记")


class TestOfflineFallback(unittest.TestCase):
    """CDN 不可达时必须有本地回退，否则地图整个白屏。"""

    def setUp(self):
        quakes = [{"id": "a", "lon": 118.2, "lat": 39.6, "depth": 10.0,
                   "mag": 5.0, "place": "x", "time": 0, "url": None,
                   "dist_km": 1.0, "type": "earthquake"}]
        stats = bm.quake_stats(quakes)
        self.html = bm.build_html("测试", "河北", 200, 3.0, quakes, [], stats,
                                  [39.6, 118.2])

    def test_onerror_fallback_present(self):
        self.assertIn("onerror", self.html)
        self.assertIn("leaflet.js", self.html)
        self.assertIn("leaflet.css", self.html)

    def test_init_wrapped_in_function(self):
        self.assertIn("function initMap()", self.html)
        self.assertIn("end initMap", self.html)

    def test_retry_when_l_missing(self):
        self.assertIn("typeof L === 'undefined'", self.html)

    def test_user_facing_failure_message(self):
        self.assertIn("地图库加载失败", self.html)

    def test_idempotent_start_guard(self):
        """initMap 必须幂等：CDN 与本地 script 都可能触发启动。

        早先版本用 __mapInited 标记但从未在 CDN 成功路径设置，
        结果「L 已定义但地图没初始化」的空图。
        """
        self.assertIn("var __mapInited = false;", self.html)
        self.assertIn("function startMap()", self.html)
        self.assertIn("if (__mapInited) { return true; }", self.html)
        self.assertIn("__mapInited = true;", self.html)

    def test_startmap_guards_undefined_l(self):
        self.assertIn("if (typeof L === 'undefined') { return false; }", self.html)

    def test_no_double_polling_in_initmap(self):
        """initMap 内部不应再自行轮询 L，会与外部守卫打架。"""
        import re
        m = re.search(r"function initMap\(\)\{(.*?)\n\} // end initMap",
                      self.html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("setTimeout(initMap", m.group(1),
                         "initMap 内不应自行轮询")

    def test_local_script_onload_uses_function(self):
        """onload 必须是函数调用形式，不能是立即求值的表达式。"""
        self.assertIn("s.onload=function(){window.__leafletReady&&window.__leafletReady();};",
                      self.html)


class TestHistAxis(unittest.TestCase):
    """年代直方图 X 轴：只标首/中/末，避免标签互相压叠。"""

    def setUp(self):
        quakes = [{"id": str(i), "lon": 118.2, "lat": 39.6, "depth": 10.0,
                   "mag": 4.0, "place": "x", "time": 1451520000000 + i * 31536000000,
                   "url": None, "dist_km": 1.0, "type": "earthquake"}
                  for i in range(40)]
        stats = bm.quake_stats(quakes)
        self.html = bm.build_html("测试", "河北", 200, 3.0, quakes, [], stats,
                                  [39.6, 118.2])

    def test_hist_bars_present(self):
        # v3.4.0：柱子容器改名 .hbars，轴标签行 .histax 独立成行
        # 注意：self.html 是**模板原文**（占位符未替换），
        # .hbars 的 class 是 Python 端 build_html 生成的，
        # 所以模板里只能查 CSS 选择器，实际 class 要查 Python 源码。
        self.assertIn(".hbars", self.html, "模板 CSS 缺少 .hbars 样式")
        self.assertIn("histax", self.html)

    def test_axis_labels_not_per_bar(self):
        """每根柱子不应都带年份标签（早先实现会挤成一团）。"""
        import re
        bars = re.findall(r'<div class="b"[^>]*>(.*?)</div>', self.html)
        self.assertTrue(bars)
        for b in bars:
            self.assertNotIn("<i>", b, "柱子内不应内嵌年份标签")


class TestGeocodeError(unittest.TestCase):
    """地名解析失败必须给出可操作提示，而不是干巴巴一句「失败」。"""

    def test_carries_candidates(self):
        cands = [{"name": "唐山", "admin1": "四川"},
                 {"name": "唐山", "admin1": "山东"}]
        e = bm.GeocodeError("唐山", "河北", cands)
        msg = str(e)
        self.assertIn("河北", msg)
        self.assertIn("四川", msg)
        self.assertIn("候选", msg)

    def test_suggests_explicit_coords(self):
        e = bm.GeocodeError("汶川", "四川", [])
        msg = str(e)
        self.assertIn("--lon", msg)
        self.assertIn("--lat", msg)
        self.assertIn("汶川", msg)

    def test_is_exception(self):
        self.assertTrue(issubclass(bm.GeocodeError, Exception))


class TestBoundaryMatch(unittest.TestCase):
    """行政边界匹配：曾多次静默选错城市/层级。"""

    def test_exact_match_scores_100(self):
        self.assertEqual(bm._match_score("唐山市", "唐山市"), 100)
        self.assertEqual(bm._match_score("西安", "西安"), 100)

    def test_suffix_stripped_match_scores_90(self):
        # 双方各去一个行政后缀后相等
        self.assertEqual(bm._match_score("唐山", "唐山市"), 90)
        self.assertEqual(bm._match_score("西安", "西安市"), 90)

    def test_prefix_containment_rejected(self):
        """'西安' 不应命中 '长安区'（前缀包含误判）。"""
        self.assertEqual(bm._match_score("西安", "长安区"), 0)
        self.assertEqual(bm._match_score("唐山", "唐山丰南"), 0)

    def test_unrelated_names_rejected(self):
        self.assertEqual(bm._match_score("西安", "安康市"), 0)
        self.assertEqual(bm._match_score("唐山", "巴中市"), 0)

    def test_empty_inputs_zero(self):
        self.assertEqual(bm._match_score("", "西安"), 0)
        self.assertEqual(bm._match_score("西安", ""), 0)
        self.assertEqual(bm._match_score(None, None), 0)

    def test_score_is_not_constant_for_all_candidates(self):
        """回归：早期 `stripped == want` 恒真 → 任意候选都拿 90 分。"""
        scores = {c: bm._match_score("西安", c)
                  for c in ("西安市", "安康市", "汉中市", "宝鸡市")}
        self.assertEqual(scores["西安市"], 90)
        for c in ("安康市", "汉中市", "宝鸡市"):
            self.assertEqual(scores[c], 0,
                             "非目标城市 %s 不应拿到分数" % c)
        self.assertGreater(len(set(scores.values())), 1)

    def test_gadm_prov_parses_piped_name(self):
        """GADM 的 NL_NAME_1 是「繁体|简体」竖线格式。"""
        self.assertEqual(bm._gadm_prov("陝西|陕西"), bm.norm_prov("陕西"))
        self.assertEqual(bm._gadm_prov("江蘇|江苏"), bm.norm_prov("江苏"))
        self.assertEqual(bm._gadm_prov("河北"), bm.norm_prov("河北省"))

    def test_gadm_prov_empty(self):
        self.assertEqual(bm._gadm_prov(""), "")
        self.assertEqual(bm._gadm_prov(None), "")

    def test_norm_cn_name_keeps_suffix(self):
        """归一化**不剥**行政后缀，否则 '西安市'->'西安' 会与 '长安区' 混淆。"""
        self.assertEqual(bm._norm_cn_name("西安市"), "西安市")
        self.assertEqual(bm._norm_cn_name("新城区"), "新城区")

    def test_norm_cn_name_handles_pipes_and_parens(self):
        self.assertEqual(bm._norm_cn_name("陝西|陕西"), "陕西")
        self.assertEqual(bm._norm_cn_name("石家庄市（副本）"), "石家庄市")
        self.assertEqual(bm._norm_cn_name(""), "")

    def test_is_district_type(self):
        self.assertTrue(bm._is_district_type("Shìxiáqū"))
        self.assertTrue(bm._is_district_type("District"))
        self.assertFalse(bm._is_district_type("Dìjíshì"))
        self.assertFalse(bm._is_district_type("Xiàn"))
        self.assertFalse(bm._is_district_type(""))
        self.assertFalse(bm._is_district_type(None))

    def test_point_in_rings(self):
        square = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0]]
        self.assertTrue(bm._point_in_rings(5.0, 5.0, [square]))
        self.assertFalse(bm._point_in_rings(15.0, 5.0, [square]))
        self.assertFalse(bm._point_in_rings(-1.0, 5.0, [square]))
        # 边界上视为内部
        self.assertTrue(bm._point_in_rings(0.0, 5.0, [square]))


class TestBoundaryHtml(unittest.TestCase):
    """HTML 中的边界图层与图例必须存在。"""

    def setUp(self):
        boundary = {
            "name": "唐山市", "level": 2, "level_name": "地级市",
            "admin": "河北", "source": "GADM 4.1",
            "rings": [[[118.0, 39.0], [118.0, 40.0], [119.0, 40.0], [119.0, 39.0]]],
        }
        quakes = [{"date": "1976-07-28", "mag": 7.4, "lon": 118.0, "lat": 39.6,
                   "depth": 12.0, "dist_km": 18.8, "id": "t1", "place": "河北",
                   "time": 2075520000000, "url": "", "type": "earthquake"}]
        self.html = bm.build_html(
            "唐山", "河北", 200.0, 3.0, quakes, [],
            bm.quake_stats(quakes), [118.1, 39.6], boundary)

    def test_boundary_layer_present(self):
        self.assertIn("BOUNDARY", self.html)
        self.assertIn("boundaryLayer", self.html)
        self.assertIn("行政边界", self.html)

    def test_boundary_uses_red_stroke(self):
        """边界用清晰红色主线 + 白色底衬，保证在卫星底图上醒目可辨。"""
        self.assertIn("#ff2d2d", self.html)
        self.assertIn("#ffffff", self.html)

    def test_boundary_data_json_valid(self):
        import re
        m = re.search(r"var BOUNDARY = (\{.*?\});", self.html, re.S)
        self.assertIsNotNone(m, "未找到 BOUNDARY 数据块")
        data = json.loads(m.group(1))
        self.assertEqual(data["name"], "唐山市")
        self.assertEqual(data["level_name"], "地级市")
        self.assertEqual(data["source"], "GADM 4.1")
        self.assertTrue(data["rings"])

    def test_panel_shows_boundary_name(self):
        self.assertIn("唐山市", self.html)
        self.assertIn("地级市", self.html)


class TestFaultPlainLanguage(unittest.TestCase):
    """断裂「性质 / 时代」必须转成非专业用户能懂的表述。"""

    def test_age_cn_translates_codes(self):
        self.assertIn("全新世", bm._age_cn("Qh"))
        self.assertIn("更新世", bm._age_cn("Qp3"))
        self.assertIn("第四纪", bm._age_cn("Pre-Q"))

    def test_age_cn_empty_and_unknown(self):
        self.assertEqual(bm._age_cn(""), "未标注")
        self.assertIn("地质年代代码", bm._age_cn("Zzz"))

    def test_kind_cn_translates(self):
        self.assertIn("拉张", bm._kind_cn("正断"))
        self.assertIn("挤压", bm._kind_cn("逆断"))
        self.assertIn("水平", bm._kind_cn("走滑"))
        self.assertEqual(bm._kind_cn(""), "未标注")

    def test_age_rank_newer_ranks_higher(self):
        """Qh（全新世）应比 Qp3（晚更新世）更新，Pre-Q 最老。"""
        self.assertGreater(bm._age_rank("Qh"), bm._age_rank("Qp3"))
        self.assertGreater(bm._age_rank("Qp3"), bm._age_rank("Pre-Q"))

    def test_fault_table_uses_plain_text(self):
        """断裂清单里不应再出现 Qh/Qp1 这类裸代码作为主表述。"""
        faults = [{"name": "测试断裂", "en_name": "T", "kind": "正断",
                   "age": "Qh", "coords": [[110.0, 35.0], [110.1, 35.1]]}]
        html = bm.build_html("测试", "河北", 200, 3.0, [], faults,
                             bm.quake_stats([]), [110.0, 35.0])
        self.assertIn("拉张型", html)
        self.assertIn("全新世", html)
        # 表头用「最新活动年代」而不是含糊的「时代」
        self.assertIn("最新活动年代", html)
        self.assertIn("活动方式", html)


class TestLayerControlMultiSelect(unittest.TestCase):
    """右上角图层控制必须是全多选（checkbox），不能是 Leaflet 默认的 radio。"""

    def setUp(self):
        self.html = bm.build_html("测试", "河北", 200, 3.0, [], [],
                                  bm.quake_stats([]), [110.0, 35.0])

    def test_no_default_radio_layers_control(self):
        """L.control.layers 对底图是 radio 单选，本技能自建面板取代它。

        注意只查「实际调用」—— 模板注释里会提到它作为对比说明。
        """
        import re as _re
        calls = _re.findall(r"L\.control\.layers\s*\(", self.html)
        self.assertEqual(calls, [], "不应调用 Leaflet 默认图层控件（底图是 radio 单选）")

    def test_custom_panel_present(self):
        self.assertIn('id="lctrl"', self.html)
        self.assertIn('id="lbody"', self.html)
        self.assertIn("LAYER_ITEMS", self.html)
        self.assertIn("setLayer", self.html)

    def test_all_layers_toggleable(self):
        for lid in ("sat", "label", "bnd", "flt", "qk"):
            self.assertIn("'%s'" % lid, self.html)

    def test_left_right_layout(self):
        """左右分栏骨架：侧栏 + 地图区。"""
        self.assertIn('class="app"', self.html)
        self.assertIn('class="side"', self.html)
        self.assertIn('class="stage"', self.html)
        self.assertIn("flex-direction:column", self.html)  # 窄屏降级为上下

    def test_quake_icon_is_custom(self):
        """地震改用自定义 divIcon（发光内核），不再是纯 circleMarker。"""
        self.assertIn("quakeIcon", self.html)
        self.assertIn("L.divIcon", self.html)
        self.assertIn("radial-gradient", self.html)


class TestRightPanelLayout(unittest.TestCase):
    """布局迁移：概览与图例从左侧栏移到右侧地图区。

    判据用**容器归属**而不是「字符串是否出现」——概览文字若仍留在
    .side 里，单看 __TOTAL__ 存在与否是查不出来的。
    """

    def setUp(self):
        self.html = bm.build_html("测试", "河北", 200, 3.0, [], [],
                                  bm.quake_stats([]), [110.0, 35.0])

    def _side_block(self):
        m = re.search(r'<aside class="side">(.*?)</aside>', self.html, re.S)
        self.assertIsNotNone(m, "找不到左侧栏 <aside class=\"side\">")
        return m.group(1)

    def _stage_block(self):
        m = re.search(r'<main class="stage">(.*?)</main>', self.html, re.S)
        self.assertIsNotNone(m, "找不到地图区 <main class=\"stage\">")
        return m.group(1)

    def test_overview_in_sidebar_overview_tab(self):
        side = self._side_block()
        stage = self._stage_block()
        # v3.4.0：统计概览迁入左侧栏「概览」tab（与筛选同屏），不再浮在地图区
        self.assertIn('id="mstat"', side, "统计概览面板应位于左侧栏概览 tab")
        self.assertNotIn('id="mstat"', stage, "统计概览不应再浮在地图区")
        # 左栏第一个 tab 必须是「概览」，且概览 tab 内含统计概览
        self.assertIn('data-tab="overview"', side, "左栏缺少「概览」tab")
        self.assertIn("统计概览", side, "左栏概览 tab 应含统计概览")

    def test_right_panel_has_no_tabs(self):
        """v3.4.0：右上面板取消 tab，【图层 / 分布 / 图例】三段常驻展开。"""
        stage = self._stage_block()
        self.assertNotIn('id="mtabs"', self.html, "右上面板不应再出现 tab 栏")
        self.assertNotIn("mtabs", self.html, "mtabs 样式/逻辑应已移除")
        # 三段（图层 / 分布 / 图例）仍以 mpane 承载，但默认全部可见
        for mp in ("layer", "chart", "legend"):
            self.assertIn('data-mpane="%s"' % mp, stage,
                         "右上面板缺少 %s 段" % mp)
        self.assertIn(".mpane{display:block}", self.html,
                      "mpane 必须默认 display:block（取消 tab 后常驻展示）")

    def test_quake_table_default_sort_descending(self):
        """v3.4.0：地震目录默认时间倒序（最新在前）。"""
        self.assertIn("var sortKey = 'date', sortAsc = false",
                      self.html, "目录默认应为时间倒序（sortAsc=false）")

    def test_max_recent_markers_present(self):
        """v3.4.0：地图上明确标出最大地震与最近地震。"""
        self.assertIn("specialLayer", self.html, "缺少最大/最近地震标注图层")
        self.assertIn("markSpecial", self.html, "缺少标注函数")
        self.assertIn("最大地震", self.html, "图例/标注缺少「最大地震」")
        self.assertIn("最近地震", self.html, "图例/标注缺少「最近地震」")

    def test_no_id_selector_overrides_pane_display(self):
        """⚠️ 回归：ID 选择器会压过 .pane{display:none}，导致面板同屏。

        实测踩过：`#qsec{display:flex;...}`（权重 1,0,0）盖过
        `.pane{display:none}`（权重 0,1,0），于是「地震目录」面板
        **即使没被激活也照样显示** —— 概览与目录两个面板同屏，
        正是用户明确要求避免的「在对应 tab 只能看懂对应的东西」。

        判据：凡是给侧栏 pane 元素（.pane / #qsec / #fsec）设 display 的
        CSS 规则，必须带 .on 限定。
        """
        css = self.html.split("</style>")[0]
        # 抽取所有形如 `#xxx{...}` 或 `#xxx.on{...}` 的规则
        import re as _re
        for m in _re.finditer(r"(#(?:qsec|fsec)\b[^{]*)\{([^}]*)\}", css):
            sel, body = m.group(1), m.group(2)
            if "display" in body:
                self.assertIn(".on", sel,
                              "%s 设置了 display 但没带 .on —— "
                              "会压过 .pane{display:none}，面板会同时显示" % sel)

    def test_hist_axis_labels_not_nested_in_bars(self):
        """回归：轴标签不能是柱子容器的子节点。

        实测踩过：.histax 被写在 .hist（display:flex）内部，成了柱子的
        兄弟 flex 项，被挤到 74px 宽，三个年份标签叠成「194019802020」。
        """
        # 模板侧：柱容器已拆出，轴标签行独立
        self.assertIn(".hbars", self.html, "模板 CSS 缺少 .hbars（柱子容器）")
        self.assertNotIn('<div class="hist">', self.html,
                         ".hist 不应再作为柱子容器（轴标签会被挤扁）")
        # 轴标签必须绝对定位到柱心
        self.assertIn(".histax span{position:absolute", self.html,
                      "轴标签应绝对定位，否则会互相压叠")
        # Python 侧：必须生成 .hbars 包裹的柱子
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read()
        self.assertIn('class="hbars"', src,
                      "build_map.py 必须把柱子包在 .hbars 里")
        self.assertIn('class="histax"', src,
                      "build_map.py 必须生成独立的 .histax 轴标签行")

    def test_fault_clip_grid_equivalence(self):
        """网格索引加速后，裁剪结果必须与逐边扫描**完全一致**。

        v3.4.0 引入 _EdgeGrid 把 O(段×边) 降到近似 O(段)。
        加速的最危险失败模式是「漏掉某些相交边」→ 断裂被裁短或整条丢失。
        这里用合成多边形做对照：网格路径 vs 逐边扫描路径，逐一比对。
        """
        # 一个带凹口的方形边界（凹口用来制造「必须精确求交」的边界）
        rings = [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0],
                  [5.0, 10.0], [5.0, 5.0], [0.0, 5.0], [0.0, 0.0]]]
        faults = [
            # 完全在内
            {"name": "内", "en_name": "", "kind": "", "age": "",
             "coords": [[1, 1], [2, 2], [3, 1]]},
            # 跨界（穿过 x=5 凹口边界）
            {"name": "跨", "en_name": "", "kind": "", "age": "",
             "coords": [[1, 2], [9, 2]]},
            # 完全在外
            {"name": "外", "en_name": "", "kind": "", "age": "",
             "coords": [[20, 20], [21, 21]]},
        ]
        got = bm.clip_faults_to_boundary(faults, rings)

        # 参照实现：强制走逐边扫描（grid=None）
        # ⚠️ v3.6.0：替身签名必须与生产函数**逐参数对齐** ——
        # `_clip_polyline` 新增了 ring_bboxes 参数（bbox 剪枝），
        # 替身若漏了这个参数，生产代码传 5 个实参就会抛 TypeError，
        # 而 TypeError 不会被本用例的 except 捕获 → 整条链路误报失败。
        orig = bm._clip_polyline
        try:
            def _no_grid(coords, edges, rings_, grid=None, ring_bboxes=None):
                return orig(coords, edges, rings_, None, ring_bboxes)
            bm._clip_polyline = _no_grid
            ref = bm.clip_faults_to_boundary(faults, rings)
        finally:
            bm._clip_polyline = orig

        def norm(xs):
            return sorted((f["name"], len(f["coords"])) for f in xs)
        self.assertEqual(norm(got), norm(ref),
                         "网格索引结果与逐边扫描不一致（可能漏边）")
        names = {f["name"] for f in got}
        self.assertIn("内", names, "完全在市域内的断裂被误删")
        self.assertIn("跨", names, "跨界断裂应保留")
        self.assertNotIn("外", names, "完全在市域外的断裂应被剔除")

    def test_no_disk_cache_layer(self):
        """v1.0.5：技能不得保留任何跨运行磁盘缓存设施。

        用户明确要求「不要缓存」。断言：
        ① build_map.py 不再有 cache_load/cache_save/cache_key_path/
           CACHE_SCHEMA/cpath/parse_faults_cached/default_cache_dir/set_cache_dir；
        ② boundary_datav.py 不再有 set_cache_dir / 磁盘读写缓存；
        ③ argparse 不再接受 --no-cache/--cache-dir/--cache-ttl
           （没有缓存，这三个开关失去存在意义）。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "build_map.py"), encoding="utf-8") as fh:
            bm_src = fh.read()
        with open(os.path.join(here, "boundary_datav.py"), encoding="utf-8") as fh:
            bd_src = fh.read()
        for dead in ("cache_load(", "cache_save(", "cache_key_path(",
                     "CACHE_SCHEMA", "parse_faults_cached",
                     "default_cache_dir", "set_cache_dir",
                     "def cpath("):
            self.assertNotIn(dead, bm_src,
                             "build_map.py 仍保留缓存设施: %s" % dead)
        self.assertNotIn("set_cache_dir", bd_src,
                         "boundary_datav.py 仍保留 set_cache_dir")
        for flag in ("--no-cache", "--cache-dir", "--cache-ttl"):
            self.assertNotIn('"%s"' % flag, bm_src,
                             "argparse 仍保留缓存开关 %s" % flag)
        # 断库下载函数必须仍在（只是不再缓存），且落到一次性临时目录
        self.assertIn("def fetch_fault_db(", bm_src, "断库下载函数被误删")
        self.assertIn("TMP_DIR", bm_src, "下载未落到一次性临时目录")

    def test_parse_faults_reads_fresh_each_time(self):
        """parse_faults 每次实时解析（无 pickle 缓存），结果稳定一致。"""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            src = os.path.join(td, "t.gmt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("# FEATURE_DATA\n")
                fh.write("> 甲断裂\n")
                fh.write('# @D|FZN|FZN_EN|FN|FN_EN|FSN|FSN_EN|FEA|FEA_EN|AGE|AGE_DES|REF\n')
                fh.write("1.0 2.0\n3.0 4.0\n")
            a = bm.parse_faults(src)
            b = bm.parse_faults(src)
            self.assertEqual(len(a), 1)
            self.assertEqual(a, b, "两次实时解析结果不一致")
        self.assertIn(".qann", self.html, "缺少永久标注样式")
        self.assertIn(".qring", self.html, "缺少虚线环样式")

    def test_no_seismic_belt_leftover(self):
        """删除主要地震带后，模板与构建脚本都不应再残留 belt 字眼。"""
        low = self.html.lower()
        self.assertNotIn("belt", low, "HTML 中仍残留 belt（地震带）")
        self.assertNotIn("__belts__", low, "HTML 中仍残留 __BELTS__ 占位符")
        bm_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "build_map.py"), encoding="utf-8").read().lower()
        self.assertNotIn("seismic_belt", bm_src, "build_map.py 仍 import/引用地震带模块")

    def test_legend_merged_into_layer_panel(self):
        """v2.1.0：图例并入图层面板（原独立 mlegend 浮层取消）。

        布局重构依据（实测 1600×950）：左上概览 349px + 右上面板 620px +
        左下图例 297px 三面合围，地图有效区被压成中间一条。现在图例作为
        `.lctrl .leg` 内嵌，浮层从 4 个降到 3 个。
        """
        side = self._side_block()
        stage = self._stage_block()
        # 图例内容必须仍在地图区，且不再有独立浮层
        self.assertIn('class="leg"', stage, "图例应内嵌于图层面板")
        self.assertNotIn('id="mlegend"', stage,
                         "独立图例浮层应已取消（v2.1.0 并入图层面板）")
        self.assertNotIn("mlegend", stage)
        self.assertNotIn(">图例<", side)
        # v3.0.0：图例在右上 tab 面板的 legend 分页里
        m = re.search(r'<div class="lctrl" id="lctrl">(.*?)\n    </div>\n  </div>',
                      stage, re.S)
        self.assertIsNotNone(m, "找不到图层面板")
        panel = m.group(1)
        self.assertIn('data-mpane="legend"', panel, "图层面板缺少图例分页")
        self.assertIn('class="leg"', panel, "图例不在图层面板内")

    def test_left_panel_keeps_data_sections(self):
        """迁移后左栏仍须保留需要纵向浏览的交互内容。

        v1.8.0：震级分布 / 年代分布已迁到右侧新建的「分布」面板
        （`.mchart`），左栏只留筛选器、目录表与断裂清单。
        """
        side = self._side_block()
        for kw in ("地震筛选", "地震目录", "断层清单"):
            self.assertIn(kw, side, "左栏丢失了「%s」" % kw)

    def test_charts_moved_to_new_right_panel(self):
        """v1.8.0：震级分布 / 年代分布迁到地图区新建面板，且左栏不再重复。

        用容器归属断言（`<main class="stage">` 切片），只查字符串会漏掉
        「两边各写一份」的情况。
        """
        side = self._side_block()
        stage = self._stage_block()
        # v3.0.0：分布面板不再是独立浮层，而是右上 tab 面板的一个分页
        self.assertNotIn('id="mchart"', stage,
                         "分布面板已并入右上 tab，不应再有独立浮层")
        self.assertIn('data-mpane="chart"', stage, "缺少分布分页")
        self.assertIn("震级分布", stage)
        self.assertIn("年代分布", stage)
        self.assertNotIn("震级分布", side, "左栏仍有重复的震级分布")
        self.assertNotIn("年代分布", side, "左栏仍有重复的年代分布")
        # 占位符在渲染结果里已被替换，归属断言必须查模板源文件
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r'<div class="mpane" data-mpane="chart">(.*?)</div>\s*</div>',
                      src, re.S)
        self.assertIsNotNone(m, "模板里找不到分布分页")
        self.assertIn("__MAGROWS__", m.group(1))
        self.assertIn("__YEARHIST__", m.group(1))

    def test_map_overlays_four_corners(self):
        """v2.1.0 四角布局：左上概览 / 右上图层 / 左下分布 / 右下数据源。

        实测依据：三面合围时（左上 349 + 右上 620 + 左下 297）地图有效区
        被压成中间一条。四角平衡后中央留白。断言各面板 CSS 定位到对应角。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        # v3.0.0：分布面板已并入右上 tab —— 不再有左下浮层
        self.assertNotRegex(src, r"\.mchart\{",
                            "左下分布浮层样式应已移除（v3.0.0 并入右上 tab）")
        # 右下：数据源角标（必须有 id，否则自动化验证查不到）
        self.assertIn('id="mfoot"', src, "数据源角标缺 id")
        m2 = re.search(r"\.mfoot\{[^}]*\}", src)
        self.assertIn("right:16px", m2.group(0), "数据源角标应定位到右下")
        # 图例不得再是独立 absolute 浮层
        self.assertNotRegex(src, r"\.mlegend\{",
                            "独立图例浮层样式应已移除")
        # 窄屏收起分布面板的逻辑必须按**地图区实测高度**判断
        # （media query 查视口高度不靠谱，v1.8.0 实测撞过 162×104px）
        self.assertRegex(src, r"st\.clientHeight",
                         "窄屏自适应应按地图区实测高度判断")
        self.assertIn("h < 460", src, "收起阈值应为 460px")

    def test_fault_list_collapsible_and_paged(self):
        """v2.1.0：断裂清单默认折叠 + 只列前 8 条。

        实测断裂清单 565px 常驻时，左栏内容 1412px vs 可见 793px，
        目录表被挤出视口。改为 details 折叠 + 前 8 条 + 展开全部。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        # v3.0.0：断裂清单移到独立 tab，不再用 details 折叠
        self.assertIn('data-pane="fault"', src, "断裂清单应有独立 tab")
        self.assertNotIn('<details id="fdet">', src,
                         "v3.0.0 已改 tab 化，details 折叠结构应移除")
        self.assertIn('id="fmorebtn"', src, "应有「展开全部」入口")
        self.assertIn("FSTEP = 8", src, "断裂清单默认条数应为 8")
        # 按钮写「展开全部」就必须一次展完 —— 分批展开与文案不符（实测踩过）
        self.assertNotIn("fshown +=", src, "存在分批展开，与「展开全部」文案不符")
        self.assertIn("fshown = frowsAll.length", src, "「展开全部」应一次展完")
        self.assertIn('id="fmorebtn"', src, "应有「展开全部」入口")
        self.assertIn("FSTEP = 8", src, "断裂清单默认条数应为 8")
        # 按钮写「展开全部」就必须一次展完 —— 分批展开与文案不符（实测踩过）。
        # 断言「不存在分批写法」而不是「存在一次展完」：后者在有两处入口时
        # 会漏掉只改掉其一的情况（变异验证抓到过）。
        self.assertNotIn("fshown +=", src, "存在分批展开，与「展开全部」文案不符")
        self.assertIn("fshown = frowsAll.length", src,
                      "「展开全部」应一次展完")
        # summary 快捷入口走 pendingFull + toggle，不是直接改 fshown
        self.assertIn("pendingFull", src,
                      "summary 快捷入口应经 pendingFull 交给 toggle 处理")
        # data-name 由 Python 端写进行里，查模板查不到 —— 必须查 build_map.py
        bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "build_map.py"), encoding="utf-8").read()
        self.assertRegex(bsrc, r"data-name=",
                         "断裂行应带 data-name（hover 联动用）")

    def test_row_hover_highlights_map(self):
        """v2.1.0：表格行 hover 高亮地图对应要素（双向联动）。"""
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        # 只查 "__fname" 变量名是永真的（去掉赋值后比较处还在）——
        # 必须查**赋值表达式**本体，且兜底名要与 Python 端一致。
        self.assertRegex(src, r"__fname\s*=\s*f\.name \|\| '未命名断裂'",
                         "断裂 polyline 未标记所属名称")
        bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "build_map.py"), encoding="utf-8").read()
        self.assertIn('f["name"] or "未命名断裂"', bsrc,
                      "Python 分组兜底名必须与 JS 端一致，否则 hover 匹配不上")
        self.assertIn("__highlightFault", src,
                      "应暴露高亮函数供验证")
        self.assertIn("e.key === 'Escape'", src, "应支持 Esc 关弹窗")
        # circleMarker 与 divIcon 两条高亮路径都要有
        self.assertIn("hlR0 * 2.1", src, "小震圆点应有放大高亮")
        self.assertIn("classList.add('qkhl')", src,
                      "M≥6 的 divIcon 应有 CSS 高亮类")

    def test_layer_switch_is_not_plain_checkbox(self):
        """v1.8.0 图层开关改为 switch：必须有 .tg 滑块，且无旧的 .ck 方框。"""
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("'<span class=\"tg\"><i></i></span>'", src)
        self.assertRegex(src, r"\.lctrl label\.on \.tg i\{")
        self.assertNotIn('class="ck"', src, "旧的方框式 checkbox 残留")
        # 开关状态必须显式判断，不能用 className.indexOf('on') 子串
        self.assertIn("el.className !== 'on'", src)

    def test_completeness_badge_present(self):
        """目录完整性：原先是 .notice 块，现改为地图区徽标。

        查模板源文件而不是渲染结果 —— __COMP_CLS__ 在 build_html 里
        已被替换成具体类名，在渲染结果里搜占位符必然落空。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('class="badge __COMP_CLS__"', src)
        self.assertIn("__COMPLETE_NOTE__", src)
        # 徽标三类样式都要有
        for cls in (".mstat .badge.good", ".mstat .badge.modern",
                    ".mstat .badge.sparse"):
            self.assertIn(cls, src)

    def test_no_hardcoded_gadm_in_template(self):
        """v1.5.0 起边界主源是 DataV，模板里不得再写死 GADM 4.1。

        曾发现 mfoot 与页脚都写死「边界 GADM 4.1」，而实际数据来自 DataV，
        属文档与实现不一致的静默错误。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("GADM 4.1", src)
        self.assertIn("__BSOURCE_SHORT__", src)
        self.assertIn("__BSOURCE__", src)

    def test_source_short_placeholder_resolved(self):
        """短名占位符必须被替换，且不得残留未替换占位符。"""
        self.assertNotIn("__BSOURCE_SHORT__", self.html)
        left = re.findall(r"__[A-Z]+__", self.html)
        self.assertEqual(left, [], "未替换的占位符: %s" % set(left))


class TestQuakeFilterUI(unittest.TestCase):
    """地震点位筛选器：DOM 元素齐全，且筛选同时作用于地图与目录表。"""

    def setUp(self):
        self.html = bm.build_html("测试", "河北", 200, 3.0, [], [],
                                  bm.quake_stats([]), [110.0, 35.0])

    def test_controls_exist(self):
        for eid in ("fMagMin", "fMagMax", "fYearMin", "fYearMax",
                    "fMagChips", "fReset", "fBig", "fRecent", "qcount"):
            self.assertIn('id="%s"' % eid, self.html,
                          "筛选器缺少元素 #%s" % eid)

    def test_stat_bar_exists(self):
        """统计条元素必须真实存在于 DOM。

        曾只写了 .fstat 的 CSS 而漏建 DOM，JS 里 getElementById 返回 null，
        页面不报错但统计条永不显示 —— 典型的静默失效。
        """
        self.assertIn('id="fstat"', self.html)

    def test_filter_updates_marker_and_table(self):
        """筛选必须同时控制 marker / 永久标签 / 目录表三者。

        目录表联动在 v2.0.0 从「隐藏已有行」改为**整体重渲染**：
        分页后 tbody 里只有当前页的行，隐藏它们毫无意义，必须重画。
        所以断言盯的是「筛选把结果交给表格 → 通知重渲染」这条链。
        """
        self.assertIn("removeLayer(it.marker)", self.html)
        self.assertIn("removeLayer(it.label)", self.html)
        self.assertIn("window.__quakeVisible = visible", self.html,
                      "筛选结果必须导出给表格")
        self.assertIn("window.__renderQTable()", self.html,
                      "筛选后必须通知表格重渲染")

    def test_table_row_linkage_uses_data_i(self):
        """表格行与 quakeItems 的对应必须走 data-i，不得靠 indexOf 反查。

        v2.0.0 起行由 JS 现造（分页），所以断言查的是**造行时写入
        data-i、定位时读 data-***，而不是在 innerHTML 里找字符串。
        """
        self.assertIn("data-i=\"'+i+'\"", self.html,
                      "生成的行必须带 data-i（排序/联动的依据）")
        self.assertIn("getAttribute('data-lat')", self.html,
                      "点击行定位必须读 data-lat")
        self.assertNotIn("quakeItems.indexOf(it)", self.html)

    def test_quake_table_is_paged(self):
        """目录表必须分页渲染（v2.0.0）。

        实测汶川 1268 行 = 7600 个 DOM 节点，占整页节点数的大部分；
        分页后整页 DOM 14412 → 3656，排序 9.4ms → 1.7ms。
        """
        tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "template.html"), encoding="utf-8").read()
        self.assertIn("PAGE_SIZE", tpl, "目录表必须有分页")
        self.assertIn("window.__renderQTable", tpl)
        self.assertIn("id=\"qmorebtn\"", tpl, "必须有「显示更多」入口")
        # 排序必须作用于**全量**索引，不能只对已渲染的行排序
        self.assertIn("order.sort(", tpl,
                      "排序必须作用于全量 order，否则分页会排错")
        self.assertIn("quakeItems.map(function(_, i){ return i; })", tpl,
                      "order 必须覆盖全量数据")
        # Python 端也必须只输出首页
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read()
        self.assertRegex(src, r"(?m)^QTABLE_FIRST_PAGE\s*=\s*\d+")
        # v1.4.0：原先断言源码里必须出现 `quakes[:QTABLE_FIRST_PAGE]` ——
        # 这是典型的「断言跟着实现细节跑」：build_html 一拆分（切片挪进
        # quake_rows_html）断言立刻失效，而**产品行为一点没变**。
        # 改成行为断言：直接喂 300 条，数实际渲染出的行数。
        big = [{"id": "x%d" % i, "lon": 117.0, "lat": 31.0, "depth": 10,
                "mag": 4.0, "place": "p", "time": 86400000 * (i + 1),
                "dist_km": 1.0, "source": "USGS", "in_boundary": True}
               for i in range(300)]
        rows = bm.quake_rows_html(big)
        self.assertEqual(rows.count("<tr data-i="), bm.QTABLE_FIRST_PAGE,
                         "Python 端不得全量输出目录表行（300 条只应渲染首页）")

    def test_big_button_disabled_when_no_m6(self):
        """最大震级不足 6 时「只看 M≥6」必须禁用，而不是静默筛出 0 条。

        v3.5.0 修：禁用判据原先是 `if(m1 < 6)`（滑块上界，向上取整到 0.5）。
        真实最大震级 5.6 → m1=6.0 → 判为可点，点下去命中 0 条
        且按钮显示激活态（死胡同）。现在判据用**真实最大震级 realMaxM**。
        """
        self.assertIn("if(realMaxM === null || realMaxM < 6)", self.html,
                      "禁用判据必须基于真实最大震级 realMaxM，不能用滑块上界 m1")
        self.assertNotIn("if(m1 < 6)", self.html,
                         "不得再用滑块上界 m1 作为 M≥6 按钮的禁用判据")
        self.assertIn("bBig.disabled = true", self.html)

    def test_big_button_title_uses_real_max_mag(self):
        """「只看 M≥6」的提示语必须用**真实最大震级**，不能用滑块上界。

        v3.3.0 实测：震级滑块上界 m1 是按 0.5 向上取整的
        （淮南 M3.2 → m1=3.5）。拿 m1 当「最大震级」报出去，
        会与概览面板的「最大震级 3.2」在同一页面上自相矛盾。
        """
        self.assertIn("var realMaxM = mags.length ? Math.max.apply(null, mags) : null;",
                      self.html, "应单独算出真实最大震级")
        self.assertIn("realMaxM.toFixed(1)", self.html,
                      "提示语应使用真实最大震级")
        # 提示语里不能再出现 m1.toFixed(1)
        import re as _re
        m = _re.search(r"bBig\.title\s*=\s*'本研究区最大震级 '([\s\S]{0,200}?);",
                       self.html)
        self.assertIsNotNone(m, "找不到 bBig.title 赋值")
        self.assertNotIn("m1.toFixed", m.group(1),
                         "提示语不得用滑块上界 m1 当最大震级")
        self.assertIn("realMaxM", m.group(1),
                      "提示语应使用 realMaxM")

    def test_exposes_state_for_verification(self):
        """闭包内的变量必须导出到 window，否则外部无法验证（可观测性）。"""
        for name in ("__map", "__quakeItems", "__setLayer"):
            self.assertIn("window.%s" % name, self.html)


class TestBoundaryDataV(unittest.TestCase):
    """行政边界改用阿里 DataV.GeoAtlas（三级行政边界，免 Key）。"""

    def setUp(self):
        import boundary_datav as bd
        self.bd = bd

    def test_strip_suffix_only_both_ways(self):
        """归一化只做双向精确匹配，不做前缀包含。

        「西安」不应命中「西安新城区」—— 前缀包含匹配正是 v1.2.0 修过的坑。
        """
        self.assertEqual(self.bd._name_match("唐山", "唐山市"), 90)
        # 双向剥后缀相等同样得 90（顺序无关）
        self.assertEqual(self.bd._name_match("唐山市", "唐山"), 90)
        self.assertEqual(self.bd._name_match("唐山", "唐山"), 100)
        self.assertEqual(self.bd._name_match("西安", "西安新城区"), 0)
        self.assertEqual(self.bd._name_match("汶川", "汶川县"), 90)

    def test_source_declares_gcj02(self):
        """DataV 必须声明自己是 GCJ-02，调用方据此决定不再做偏移。

        这是换数据源时最容易错的地方：GADM 是 WGS84 需要逐点转换，
        DataV 源自身就是 GCJ-02，再转一次会二次偏移 400–600 m。
        """
        self.assertEqual(self.bd.CRS, "GCJ-02")
        self.assertIn("GCJ-02", self.bd.SOURCE_NAME + self.bd.__doc__)

    def test_build_map_respects_crs_flag(self):
        """build_map 必须按 crs 字段决定是否转换，不能无条件 to_gcj_list。"""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read()
        self.assertIn('boundary.get("crs", "WGS84") != "GCJ-02"',
                      src, "build_map 必须按 crs 字段判断是否偏移")
        # DataV 必须是主源，GADM 只作回退
        self.assertIn("boundary_datav.get_boundary(", src)
        self.assertIn("回退 GADM", src)

    def test_ring_extraction_handles_multipolygon(self):
        geom = {"type": "MultiPolygon",
                "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]],
                                 [[[5, 5], [6, 5], [6, 6], [5, 5]]]]}
        rings = self.bd._iter_rings(geom)
        self.assertEqual(len(rings), 2)

    def test_adjacent_boundary_seam(self):
        """接缝校验：相邻两县的共边界段必须重合（坐标系一致的硬证据）。

        原理：DataV 的县界来自同一套官方界线，相邻县的共界段顶点应当几乎
        重合。若我们对 DataV 误做了一次 WGS84→GCJ 偏移，这些接缝点会
        整体错开约 500 m，占比会崩塌。

        需要网络（首次下载），不可达时跳过而不是让整套自检失败。
        """
        try:
            ts = self.bd.get_boundary("唐山", None, 118.19, 39.64,
                                      verbose=False)
            cd_ = self.bd.get_boundary("承德", "河北", 117.93, 40.98,
                                       verbose=False)
        except Exception as exc:  # noqa: BLE001
            self.skipTest("DataV 不可达，跳过接缝校验: %s" % exc)
        if not ts or not cd_:
            self.skipTest("未取到唐山/承德边界")

        import math

        def flat(rings):
            return [tuple(p) for r in rings for p in r]

        a = flat(ts["rings"])
        near = 0
        total = 0
        for p in flat(cd_["rings"]):
            d = min(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
                    for q in a) * 111000 * math.cos(math.radians(p[1]))
            total += 1
            if d < 20:
                near += 1
        ratio = near / max(1, total)
        self.assertGreater(ratio, 0.02,
                           "共边界重合率仅 %.2f%%，疑似坐标系处理错误"
                           % (ratio * 100))

    def test_province_filter_walks_parent_chain(self):
        """省级过滤必须沿 parent 链上溯，不能只比对直接 parent。

        DataV 索引里**县级的直接 parent 是地级市 adcode**：
        汶川县(513221) → 阿坝州(513200) → 四川省(510000)。
        只比直接 parent 会让「省 + 县」组合永远匹配不上，
        表现为静默回退到 GADM（不报错、只是边界换了源）。
        """
        try:
            index = self.bd.ensure_index()
        except Exception as exc:  # noqa: BLE001
            self.skipTest("DataV 索引不可达: %s" % exc)

        by_code = {it["adcode"]: it for it in index}
        # 先确认前提：县级的 parent 确实不是省
        wx = by_code.get(513221)
        self.assertIsNotNone(wx, "索引里应存在汶川县 513221")
        self.assertNotEqual(wx.get("parent"), 510000,
                            "前提变了：DataV 索引结构可能已调整")

        cands = self.bd.resolve_adcode("汶川", "四川", 103.59, 31.48,
                                       index=index)
        self.assertTrue(cands, "「汶川 + 四川」应能匹配到候选")
        self.assertEqual(cands[0]["name"], "汶川县")
        self.assertEqual(cands[0]["level"], "district")

    def test_resolve_uses_real_coordinates(self):
        """坐标决胜必须用索引里的 lng/lat。

        索引条目没有 center 键，只有 lng/lat。若代码读 center，
        所有候选 dist 恒为 1e9，坐标决胜形同虚设。
        """
        try:
            index = self.bd.ensure_index()
        except Exception as exc:  # noqa: BLE001
            self.skipTest("DataV 索引不可达: %s" % exc)
        cands = self.bd.resolve_adcode("汶川", "四川", 103.59, 31.48,
                                       index=index)
        self.assertTrue(cands)
        for c in cands:
            self.assertLess(c["dist"], 1.0,
                            "dist 恒为 1e9 说明 center 没从 lng/lat 构造")
            self.assertIn("center", c)

    def test_source_short_defined(self):
        """边界来源必须有短名，供地图角标使用。"""
        self.assertTrue(getattr(self.bd, "SOURCE_SHORT", None))
        self.assertLess(len(self.bd.SOURCE_SHORT), len(self.bd.SOURCE_NAME))


class TestCityScope(unittest.TestCase):
    """v1.8.0：统计范围收敛到行政边界内（只展示该城市范围内的地震点）。"""

    # 合成边界：一个以 (117.0, 31.8) 为中心、约 0.5° 见方的方框（合肥量级）
    BOX = [[[116.75, 31.55], [117.25, 31.55], [117.25, 32.05],
            [116.75, 32.05], [116.75, 31.55]]]

    @staticmethod
    def _q(lon, lat, mag=3.5):
        return {"lon": lon, "lat": lat, "mag": mag, "depth": 10.0,
                "time": 0, "place": "", "url": None, "dist_km": 0.0}

    def _boundary(self, rings=None):
        return {"name": "合肥市", "level": 2, "level_name": "地级市",
                "admin": "安徽", "source": "test", "source_short": "test",
                "crs": "GCJ-02", "rings": rings if rings is not None else self.BOX}

    def test_keeps_only_points_inside(self):
        """边界内的保留，边界外的剔除。"""
        qs = [self._q(117.0, 31.8),      # 内
              self._q(117.2, 31.9),      # 内
              self._q(118.5, 31.8),      # 外（东）
              self._q(117.0, 33.5)]      # 外（北）
        # min_keep 显式传 1：本用例只有 4 条，默认的退化阈值（8）会把它
        # 判成「记录过少」而退回半径结果，掩盖真正的包含判定行为。
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary(),
                                                  min_keep=1)
        self.assertEqual(len(kept), 2)
        self.assertEqual(info["mode"], "city")
        self.assertEqual(info["inside"], 2)
        self.assertEqual(info["radius_total"], 4)
        self.assertEqual(info["kept"], 2)

    def test_no_boundary_falls_back_to_radius(self):
        """无边界（`--no-boundary` 或下载失败）→ 按半径统计，并写明原因。"""
        qs = [self._q(117.0, 31.8), self._q(118.5, 31.8)]
        kept, info = bm.filter_quakes_in_boundary(qs, None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(info["mode"], "radius")
        self.assertIn("未加载行政边界", info["reason"])

    def test_empty_rings_falls_back(self):
        """rings 为空等同于无边界，不能因为「有 boundary 对象」就全剔成 0。"""
        qs = [self._q(117.0, 31.8)]
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary(rings=[]))
        self.assertEqual(len(kept), 1)
        self.assertEqual(info["mode"], "radius")

    def test_zero_inside_falls_back(self):
        """市域内 0 条时退回半径结果 —— 宁可多画，不可给一张空图。

        实测依据（这就是阈值定为 1 而不是 8 的原因）：合肥半径 200 km 内
        20 次、市域内只有 2 次（2024-09 肥东 M4.2 / M4.9）。若阈值定成 8，
        页面最大震级会显示 6.4 —— 那是 1917 年六安霍山地震，距合肥 131 km。
        把邻市大震算成本市的，正是这一版要修的问题。
        """
        qs = [self._q(118.5, 31.8), self._q(118.6, 31.9)]
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary())
        self.assertEqual(len(kept), 2, "0 条时应保留全部半径结果")
        self.assertEqual(info["mode"], "radius")
        self.assertEqual(info["inside"], 0)
        self.assertIn("无目录记录", info["reason"])

    def test_two_inside_is_kept(self):
        """市域内只有 2 次也必须按市域统计 —— 这是合肥的真实形态。

        回归用例：早期把退化阈值定为 8，合肥因此被错误地退回半径统计，
        把 131 km 外的 M6.4 说成了合肥的地震。
        """
        qs = [self._q(117.0, 31.8), self._q(117.2, 31.9)] + \
             [self._q(118.5 + i * 0.1, 31.8) for i in range(18)]
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary())
        self.assertEqual(len(kept), 2)
        self.assertEqual(info["mode"], "city")
        self.assertEqual(info["radius_total"], 20)

    def test_threshold_is_configurable(self):
        """退化阈值必须是模块级常量，便于按数据密度调整。"""
        self.assertIsInstance(bm.CITY_SCOPE_MIN_KEEP, int)
        self.assertGreaterEqual(bm.CITY_SCOPE_MIN_KEEP, 1)
        qs = [self._q(117.0, 31.8)] * 5
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary(),
                                                  min_keep=5)
        self.assertEqual(info["mode"], "city", "达到阈值应按市域统计")
        kept2, info2 = bm.filter_quakes_in_boundary(
            qs, self._boundary(), min_keep=6)
        self.assertEqual(info2["mode"], "radius", "低于阈值应回退")

    def test_hole_ring_excludes(self):
        """带内环的多边形：落在洞里的点必须判为外部。

        DataV 的 rings 是「外环 + 内环」扁平列表，逐环 XOR 是标准做法。
        若哪天改成「只在第一个环内」就会漏掉这个用例。
        """
        outer = [[116.0, 31.0], [118.0, 31.0], [118.0, 33.0],
                 [116.0, 33.0], [116.0, 31.0]]
        hole = [[116.8, 31.6], [117.4, 31.6], [117.4, 32.2],
                [116.8, 32.2], [116.8, 31.6]]
        qs = [self._q(117.1, 31.9),    # 落在洞里
              self._q(117.9, 32.8)]    # 在外环内、洞外
        kept, info = bm.filter_quakes_in_boundary(
            qs, self._boundary(rings=[outer, hole]), min_keep=1)
        self.assertEqual(info["inside"], 1)
        self.assertEqual(kept[0]["lon"], 117.9)

    def test_scope_note_rendered(self):
        """范围说明必须出现在页面里 —— 否则「12 次」会被误读成城市全史。"""
        qs = [self._q(117.0, 31.8), self._q(118.5, 31.8),
              self._q(118.6, 31.8)]
        kept, info = bm.filter_quakes_in_boundary(qs, self._boundary(),
                                                  min_keep=1)
        html = bm.build_html("合肥", "安徽", 200, 3.0, kept, [],
                             bm.quake_stats(kept), [31.82, 117.23],
                             boundary=self._boundary(), scope=info,
                             fault_scope_note="已裁剪至行政边界内（含跨界断裂）",
                             fault_clip="city")
        self.assertIn("合肥市 行政区域内", html)
        self.assertIn("已剔除区域外 2 次", html)
        self.assertNotIn("__SCOPENOTE__", html)
        self.assertNotIn("__SCOPECHIP__", html)

    def test_radius_mode_note(self):
        """--scope=radius 时页面必须说明是半径统计，不能混着写。"""
        qs = [self._q(117.0, 31.8)]
        info = {"mode": "radius", "radius_total": 1, "kept": 1,
                "inside": None, "reason": ""}
        html = bm.build_html("合肥", "安徽", 200, 3.0, qs, [],
                             bm.quake_stats(qs), [31.82, 117.23],
                             boundary=self._boundary(), scope=info,
                             fault_scope_note="半径 200 km（未加载边界，按圆形范围）",
                             fault_clip="radius")
        self.assertIn("中心 200 km 半径内", html)
        self.assertNotIn("行政区域内", html)

    def test_fault_scope_note_rendered(self):
        """断裂范围说明必须出现在页面里 —— 用户要知道看到的是市域内还是半径内。"""
        qs = [self._q(117.0, 31.8)]
        info = {"mode": "radius", "radius_total": 1, "kept": 1,
                "inside": None, "reason": ""}
        html = bm.build_html("合肥", "安徽", 200, 3.0, qs, [],
                             bm.quake_stats(qs), [31.82, 117.23],
                             boundary=self._boundary(), scope=info,
                             fault_scope_note="已裁剪至行政边界内（含跨界断裂）",
                             fault_clip="city")
        self.assertIn("已裁剪至行政边界内", html)
        self.assertNotIn("__FAULTSCOPENOTE__", html)

    def test_scope_written_to_data_json(self):
        """data.json 要带 scope 字段，便于二次分析时知道数字口径。"""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read()
        self.assertIn('"scope": scope', src)

    def test_cli_has_scope_flag(self):
        """--scope 必须有 city / radius 两个取值，默认 city。"""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read()
        self.assertIn('"--scope", choices=("city", "radius")', src)
        self.assertIn('default="city"', src)

    def test_no_belt_feature_anywhere(self):
        """「主要地震带」功能已在 v3.4.0 整体下线。

        断言必须覆盖三处：**模板 DOM/JS**、**Python 数据结构**、**产物键**。
        只查一处会漏 —— 之前就出现过「JS 删了但 data.json 里还塞 belts」。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            tpl_src = fh.read().lower()
        for kw in ("belt", "__belts__", "地震带"):
            self.assertNotIn(kw, tpl_src,
                             "template.html 仍残留地震带相关内容：%s" % kw)
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_map.py"), encoding="utf-8").read().lower()
        for kw in ("seismic_belt", "build_belts", "__belts__"):
            self.assertNotIn(kw, src,
                             "build_map.py 仍残留地震带相关内容：%s" % kw)
        # build_map.py 里也不该再出现「地震带」三字（历史注释也要清干净，
        # 否则读者会以为该功能还在）
        self.assertNotIn("地震带", src,
                         "build_map.py 仍残留「地震带」字样（注释/help 文本）")

    def test_belt_modules_deleted(self):
        """地震带模块文件必须真的从技能里删掉，不能留成死代码。"""
        base = os.path.dirname(os.path.abspath(__file__))
        for name in ("seismic_belts.py", "seismic_belt_geom.py"):
            self.assertFalse(os.path.exists(os.path.join(base, name)),
                             "%s 应已删除" % name)

    def test_initial_view_fits_boundary(self):
        """只展示市域内地震点后，初始视野必须收口到边界。

        固定 zoom=9 会让「视野范围」与「数据范围」对不上：
        大城市点挤在中间，小城市连边界都出屏。
        """
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        with io.open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("map.fitBounds(bb", src)
        # fitBounds 前必须先用 bbox 判有效性，否则空 rings 会抛异常
        self.assertIn("bb.isValid()", src)


class TestWarningFree(unittest.TestCase):
    """Python 3.12+ 对 JS 正则里的 \\s 会发 SyntaxWarning，必须转义。"""

    def test_no_invalid_escape_in_template(self):
        """JS 模板已外置到 template.html，断言必须查真实文件。

        早期版本只查 build_map.py，模板外置后该断言变成永真 ——
        静默失效的典型案例（断言通过但什么都没检查）。
        """
        import re as _re
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "template.html")
        self.assertTrue(os.path.exists(tpl), "template.html 必须存在")
        with open(tpl, encoding="utf-8") as _fh:
            src = _fh.read()
        # 确认这个文件里确实有那段 JS 正则（否则断言同样会空转）
        self.assertIn("[▲▼]", src, "模板中应含震级标签的排序箭头正则")
        # 模板已从 Python 字符串里**外置**到 .html，写法随之改变：
        # 内嵌时代要写 `\\s`（Python 转义后才是 `\s`），外置后直接写 `\s`。
        # 断言必须跟着改，否则会把「正确写法」判成错误。
        self.assertIn("[▲▼]\\s*$", src,
                      "排序箭头正则应为单反斜杠 \\s（模板已外置，不再经 Python 转义）")
        self.assertNotIn("[▲▼]\\\\s*$", src,
                         "外置后写成 \\\\s 反而会匹配字面的 s")

    def test_build_map_reads_external_template(self):
        """build_map.py 必须从 template.html 读模板，不得再内嵌一份副本。"""
        import re as _re
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "build_map.py")
        with open(_p, encoding="utf-8") as _fh:
            src = _fh.read()
        self.assertIn("template.html", src)
        # 不得再出现内联的 <!DOCTYPE html> 模板副本
        self.assertEqual(_re.findall(r"<!DOCTYPE html>", src), [],
                         "模板应只存在于 template.html，不得有内嵌副本")


class TestV150NoCacheAtAll(unittest.TestCase):
    """v1.0.5：技能不使用任何磁盘缓存（用户明确要求「不要缓存」）。

    背景：v3.x 曾把缓存放在操作系统用户缓存目录以加速重复运行，但缓存
    引入了真实故障——旧 pickle 静默遮蔽新字段（belt 恒为空）、地震目录
    TTL 内看不到新事件、geocode 缓存让 DataV 更新不生效。v1.0.5 起整体
    移除缓存设施，速度改由「16 路分块并发下载 + 四路取数并行」保证。
    """

    SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SCRIPTS = os.path.join(SKILL_DIR, "scripts")

    def test_no_cache_dir_in_skill(self):
        """scripts/_cache/ 不得存在。"""
        self.assertFalse(
            os.path.exists(os.path.join(self.SCRIPTS, "_cache")),
            "技能目录内不得有 _cache/")

    def test_no_pycache_in_skill(self):
        """scripts/__pycache__/ 不得存在（bytecode 也是缓存）。"""
        self.assertFalse(
            os.path.exists(os.path.join(self.SCRIPTS, "__pycache__")),
            "技能目录内不得有 __pycache__/")

    def test_no_user_cache_dir_concept(self):
        """源码中不得再出现用户缓存目录相关标识（环境变量/目录名）。"""
        for fn in ("build_map.py", "boundary_datav.py"):
            with open(os.path.join(self.SCRIPTS, fn), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("CN_ACTIVE_FAULT_CACHE", src,
                             "%s 仍引用缓存目录环境变量" % fn)
            self.assertNotIn("CN_FAULT_SEISMIC_CACHE", src,
                             "%s 仍引用旧缓存目录环境变量" % fn)

    def test_source_never_hardcodes_skill_cache_dir(self):
        """运行时脚本不得再把缓存写到技能目录里的 _cache。

        pack.py 例外：它保留 "_cache" 在排除列表里是**防御性**做法 ——
        老版本安装过的用户目录里可能还留着 scripts/_cache，
        打包时必须把它剔掉。
        """
        for fn in ("build_map.py", "boundary_datav.py"):
            p = os.path.join(self.SCRIPTS, fn)
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(
                '"_cache"', src,
                "%s 仍硬编码 _cache 目录名" % fn)
            self.assertNotIn(
                "'_cache'", src,
                "%s 仍硬编码 _cache 目录名" % fn)

    def test_pack_still_excludes_cache_dir(self):
        """pack.py 必须继续排除 _cache / __pycache__（老安装目录可能还有残留）。"""
        p = os.path.join(self.SCRIPTS, "pack.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"_cache"', src, "打包必须排除 _cache")
        self.assertIn('"__pycache__"', src, "打包必须排除 __pycache__")

    def test_download_parts_use_temp_dir(self):
        """分块下载的临时分片必须放一次性临时目录（TMP_DIR），退出即删。"""
        with open(os.path.join(self.SCRIPTS, "build_map.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('tempfile.mkdtemp(prefix="cn-fault-seismic-dl-", dir=TMP_DIR)',
                      src, "下载分片应放在 TMP_DIR 下")
        self.assertIn(
            "atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)",
            src, "TMP_DIR 必须在退出时清理")

    def test_main_flow_fetches_in_parallel(self):
        """主流程必须四路并行取数（v1.0.5 提速核心）。

        串行时墙钟 = 四路之和；并行时 = 最大值。断言：
        ① ThreadPoolExecutor(max_workers=4)；
        ② 四个 job（USGS/CENC/断库/边界）都以 ex.submit 提交。
        """
        with open(os.path.join(self.SCRIPTS, "build_map.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("ThreadPoolExecutor(max_workers=4)", src,
                      "主流程未按 4 路并行取数")
        for job in ("_job_usgs", "_job_cenc", "_job_faults", "_job_boundary"):
            self.assertIn("ex.submit(%s)" % job, src,
                          "%s 未提交到线程池" % job)

    def test_fault_download_uses_16_workers(self):
        """断库分块并发必须为 16 路（实测 8 路 92 s → 16 路 58 s）。"""
        with open(os.path.join(self.SCRIPTS, "build_map.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("workers=16", src, "断库下载未按 16 路分块并发")
        self.assertIn("workers=16, timeout=150", src,
                      "fetch_fault_db 未按 16 路 / 150 s 超时调用")

class TestV330GeocodeFallback(unittest.TestCase):
    """v3.3.0：地名解析的第二数据源（DataV 行政区划索引）。

    实测根因：GeoNames 把不少中国城市的主名字段存成**拉丁转写**
    （淮南 -> "Huainan"、厦门 -> "Xiamen"、温州 -> "Wenzhou"、
      宿州 -> "Suzhou"、宣城 -> "Xuancheng"），
    中文查询恒 0 命中 —— 52 城抽样实测失败 5 城（9.6%）。
    v3.2.0 直接报错退出，用户拿不到任何图。
    """

    def test_gcj_to_wgs_inverse_roundtrip(self):
        """GCJ-02 -> WGS84 逆变换必须能还原（误差 < 1 m）。"""
        import math
        for lon, lat in [(117.018329, 32.647574), (108.948024, 34.263161),
                         (118.175393, 39.635113), (116.404, 39.915)]:
            wlon, wlat = bm.gcj02_to_wgs84(lon, lat)
            glon, glat = bm.to_gcj02(wlon, wlat)
            d = math.hypot((glon - lon) * 85000, (glat - lat) * 111000)
            self.assertLess(d, 1.0,
                            "往返误差 %.3f m 过大 @ %s" % (d, (lon, lat)))

    def test_inverse_is_not_accumulating(self):
        """逆变换不得写成累积形式（w -= f(w)），否则误差会累加到公里级。"""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "build_map.py"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.find("def gcj02_to_wgs84")
        self.assertGreater(i, 0, "找不到 gcj02_to_wgs84 函数")
        # 取函数体（到下一个顶层 def/class 为止）
        tail = src[i:]
        m = re.search(r"\n(?=(?:def |class ))", tail[10:])
        body = tail[:10 + m.start()] if m else tail[:1200]
        self.assertIn("w_lon = lon - (g_lon - w_lon)", body,
                      "逆变换必须每轮从原始 GCJ 重算，不能累积")
        self.assertNotIn("w_lon -= ", body,
                         "逆变换不得使用累积写法 w -= f(w)")
        self.assertIn("for _ in range(10):", body,
                      "逆变换迭代次数应与 JS 端一致（10 轮）")

    def test_inverse_matches_js_template(self):
        """Python 逆变换必须与 template.html 的 JS 版逐项一致。

        模板里的 JS 版用于 GeoJSON 导出（CSV/GeoJSON 标称 WGS84）。
        两边若不一致，导出的坐标就会与页面显示对不上。
        """
        tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "template.html"), encoding="utf-8").read()
        self.assertIn("function gcj02ToWgs84", tpl, "模板缺少 JS 逆变换")
        self.assertIn("wLon = lon - (g[0] - wLon);", tpl,
                      "JS 逆变换应每轮从原始 GCJ 重算")
        self.assertIn("for (var i=0;i<10;i++)", tpl,
                      "JS 逆变换迭代次数应与 Python 端一致（10 轮）")

    def test_fallback_resolves_romanized_cities(self):
        """中文名在 GeoNames 查不到的城市，DataV 索引必须能解析出来。

        这几个是实测失败的城市（主名为拉丁转写）。
        """
        try:
            index = bm.boundary_datav.ensure_index()
        except Exception as exc:  # noqa: BLE001
            self.skipTest("DataV 索引不可达: %s" % exc)
        for city, prov, expect in [("淮南", "安徽", "淮南市"),
                                   ("厦门", "福建", "厦门市"),
                                   ("温州", "浙江", "温州市"),
                                   ("宿州", "安徽", "宿州市"),
                                   ("宣城", "安徽", "宣城市")]:
            cands = bm.boundary_datav.resolve_adcode(city, prov, None, None,
                                                     index)
            self.assertTrue(cands, "「%s」应在 DataV 索引里解析出候选" % city)
            self.assertEqual(cands[0]["name"], expect,
                             "「%s」应解析为 %s" % (city, expect))

    def test_fallback_converts_gcj_to_wgs84(self):
        """DataV 坐标是 GCJ-02，回退路径必须做逆变换。

        不做逆变换会让 USGS 半径查询偏 400–600 m。
        """
        try:
            index = bm.boundary_datav.ensure_index()
        except Exception as exc:  # noqa: BLE001
            self.skipTest("DataV 索引不可达: %s" % exc)
        cands = bm.boundary_datav.resolve_adcode("淮南", "安徽", None, None,
                                                 index)
        self.assertTrue(cands)
        lon_g, lat_g = cands[0]["center"]
        lon, lat = bm.gcj02_to_wgs84(lon_g, lat_g)
        # 逆变换后与原 GCJ 点的距离应在 400–800 m（真实偏移量级），
        # 若为 0 则说明根本没做变换。
        import math
        d = math.hypot((lon_g - lon) * 85000, (lat_g - lat) * 111000)
        self.assertGreater(d, 200.0, "疑似未做 GCJ-02 → WGS84 逆变换")
        self.assertLess(d, 1200.0, "逆变换位移异常: %.0f m" % d)

    def test_geocode_datav_actually_applies_inverse(self):
        """**函数级**验证：`_geocode_datav` 的返回值确实已转成 WGS84。

        为什么必须到函数级测：只测 `gcj02_to_wgs84()` 这个辅助函数，
        断言的是「工具存在且正确」，而不是「回退路径**调用了**它」。
        变异验证实测：把 `_geocode_datav` 里的调用删掉（直接返回 GCJ-02 原值），
        函数级断言照样全绿 —— 这就是典型的「测了实现细节、没测接线」。

        做法：mock 掉 DataV 的索引与解析，喂一个已知的 GCJ-02 点，
        再看 `_geocode_datav` 返回的是 WGS84 还是原值。
        """
        from unittest import mock
        # 淮南市的 GCJ-02 坐标（与真实 DataV 索引一致）
        gcj_lon, gcj_lat = 117.018329, 32.647574
        fake_index = [{"adcode": 340400, "name": "淮南市", "level": "city",
                       "parent": 340000, "lng": gcj_lon, "lat": gcj_lat}]
        fake_cands = [{"adcode": 340400, "name": "淮南市", "level": "city",
                       "parent": 340000, "center": (gcj_lon, gcj_lat)}]
        with mock.patch.object(bm.boundary_datav, "ensure_index",
                               return_value=fake_index), \
             mock.patch.object(bm.boundary_datav, "resolve_adcode",
                               return_value=fake_cands), \
             mock.patch.object(bm.boundary_datav, "_province_name",
                               return_value="安徽省"):
            lon, lat, name, prov = bm._geocode_datav("淮南", "安徽")
        import math
        d = math.hypot((lon - gcj_lon) * 85000, (lat - gcj_lat) * 111000)
        self.assertGreater(d, 200.0,
                           "回退未对 GCJ-02 做逆变换（返回值与原值相同）")
        self.assertLess(d, 1200.0, "逆变换位移异常: %.0f m" % d)
        self.assertEqual(name, "淮南市")
        self.assertEqual(prov, "安徽省")

    def test_geocode_city_uses_datav_primary(self):
        """geocode_city 必须以 DataV 为唯一地名数据源，不再依赖 GeoNames。

        ① 保留 DataV 解析函数；② geocode_city 直接调用它（而非仅作回退）；
        ③ 不再出现 Open-Meteo / GeoNames 的 GEOCODE 端点与 _geocode_raw。
        """
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "build_map.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("def _geocode_datav(", src, "缺少 DataV 地名解析函数")
        # geocode_city 直接以 DataV 为唯一源：先调用、命中即返回
        self.assertIn("hit = _geocode_datav(city, province)", src,
                      "geocode_city 必须先调用 DataV 解析")
        self.assertNotIn("GEOCODE =", src,
                         "不应再保留 GeoNames/Open-Meteo 端点常量")
        self.assertNotIn("_geocode_raw", src,
                         "不应再依赖 GeoNames/Open-Meteo 地名解析")
        self.assertNotIn("ThreadPoolExecutor(max_workers=min(6", src,
                         "不应再为 GeoNames 多变体并发查询")

    def test_geocode_city_returns_wgs84_and_normalized_name(self):
        """**函数级**验证：geocode_city 返回 WGS84 坐标 + 归一化展示名。

        mock 掉 DataV 的索引与解析，喂一个已知的 GCJ-02 点，确认：
        ① 返回坐标已逆变换为 WGS84（非原 GCJ-02 值）；
        ② 展示名做了 norm_prov（「安徽省·合肥市」->「安徽·合肥」）；
        ③ 第四项仍为原始省级名（供边界匹配消歧，不得被归一化）。
        """
        from unittest import mock
        # 合肥市的 GCJ-02 中心（与 DataV 索引一致）
        fake_cands = [{"adcode": 340100, "name": "合肥市", "level": "city",
                       "parent": 340000, "center": (117.28083, 31.86389)}]
        with mock.patch.object(bm.boundary_datav, "ensure_index",
                               return_value=[]), \
             mock.patch.object(bm.boundary_datav, "resolve_adcode",
                               return_value=fake_cands), \
             mock.patch.object(bm.boundary_datav, "_province_name",
                               return_value="安徽省"):
            lon, lat, name, prov = bm.geocode_city("合肥", "安徽")
        import math
        d = math.hypot((lon - 117.28083) * 85000, (lat - 31.86389) * 111000)
        self.assertGreater(d, 200.0, "返回坐标疑似未做 GCJ-02 → WGS84 逆变换")
        self.assertLess(d, 1200.0, "逆变换位移异常: %.0f m" % d)
        self.assertEqual(name, "安徽·合肥", "展示名应归一化为「安徽·合肥」")
        self.assertEqual(prov, "安徽省", "省级名应保持原始值供边界消歧")


class TestV350AuditFixes(unittest.TestCase):
    """v3.5.0「审视技能修复问题」的回归断言。

    覆盖本轮**先用最小合成输入 / 真实接口证实、再动手**的全部缺陷，
    每条都对应一个曾经真实存在的错误行为。
    """

    def setUp(self):
        self.tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "template.html")
        with open(self.tpl_path, encoding="utf-8") as fh:
            self.tpl = fh.read()
        self.src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "build_map.py")
        with open(self.src_path, encoding="utf-8") as fh:
            self.src = fh.read()

    # ---------- S1：_clip_polyline 跨多边形间隙硬连接 ----------
    def test_clip_polyline_splits_at_multipolygon_gap(self):
        """两个不相交方块 + 一条横穿间隙的断裂 → 必须断成 2 段。

        修复前：cur 在段循环外初始化且只在末尾 flush 一次，
        段与段之间的空隙被**硬连**，输出里出现一条完全在边界外的
        (10,5)→(20,5) 连线。
        """
        rings = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],
            [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0), (20.0, 0.0)],
        ]
        edges = []
        for ring in rings:
            for i in range(len(ring) - 1):
                edges.append((ring[i], ring[i + 1]))
        coords = [(2.0, 5.0), (8.0, 5.0), (10.0, 5.0), (20.0, 5.0),
                  (22.0, 5.0), (28.0, 5.0)]
        runs = bm._clip_polyline(coords, edges, rings)
        self.assertEqual(len(runs), 2,
                         "跨越两个不相交多边形的断裂应切成 2 段，实得 %d 段"
                         % len(runs))
        gap_links = 0
        for r in runs:
            for i in range(len(r) - 1):
                mx = (r[i][0] + r[i + 1][0]) / 2.0
                if 10.0 < mx < 20.0:
                    gap_links += 1
        self.assertEqual(gap_links, 0,
                         "不得存在跨越多边形间隙的连线段（%d 条）" % gap_links)

    def test_clip_polyline_keeps_continuous_run_whole(self):
        """回归：完全位于单个多边形内的折线必须仍是 1 段（不能误切）。"""
        rings = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0),
                  (0.0, 0.0)]]
        edges = [(rings[0][i], rings[0][i + 1]) for i in range(4)]
        runs = bm._clip_polyline([(1.0, 1.0), (5.0, 5.0), (9.0, 1.0)],
                                 edges, rings)
        self.assertEqual(len(runs), 1,
                         "多边形内部的连续折线应保持 1 段，实得 %d 段" % len(runs))

    def test_clip_faults_splits_discontinuous_segments(self):
        """clip_faults（半径兜底路径）也必须按连续性切段。"""
        # 半径 50 km 覆盖约 0.45° 纬 / 0.58° 经，中心取 116.0/39.0。
        # 中间两点跳到明显在范围外，再回到范围内 → 必须切成 2 段。
        faults = [{"name": "跳", "en_name": "", "kind": "", "age": "",
                   "coords": [[116.00, 39.00], [116.05, 39.05],
                              [117.60, 39.60], [116.10, 39.10],
                              [116.20, 39.20]]}]
        kept = bm.clip_faults(faults, 116.0, 39.0, 50.0)
        self.assertGreaterEqual(len(kept), 2,
                                "中间跳出范围的点应把折线切成多段，实得 %d 段"
                                % len(kept))
        for k in kept:
            for p in k["coords"]:
                self.assertTrue(115.0 <= p[0] <= 117.1,
                                "裁剪结果不应含范围外的点: %s" % p)

    # ---------- S2：USGS 年份上界丢掉 12 月 31 日 ----------
    def test_usgs_endtime_keeps_dec31(self):
        """时间上界必须是 12-31T23:59:59，不能写成裸 12-31。

        USGS 把裸日期解析为该日 T00:00:00，写成 "12-31" 会丢掉
        12 月 31 日整天（实测全球 2019-12-31：裸日期 0 条，
        加时刻 136 条）。
        """
        self.assertNotIn('"endtime"] = "%d-12-31" %', self.src,
                         "不得再用裸日期作 endtime（会丢掉 12-31 当天）")
        self.assertIn('"endtime"] = "%d-12-31T23:59:59" %', self.src,
                      "endtime 必须带上当日最后一秒")

    # ---------- P1-1：.qkhl 高亮被同权重规则覆盖 ----------
    def test_qkhl_highlight_outranks_main_rules(self):
        """高亮态选择器权重必须高于主线 .qk .halo / .qk .core。

        修复前两者权重相同（0,2,0）而后写的赢 → 高亮从未生效。
        """
        self.assertIn(".qkhl .qk .halo", self.tpl,
                      "高亮选择器必须多一层 .qk 才能压过 .qk .halo")
        self.assertIn(".qkhl .qk .core", self.tpl)
        # 提取两条规则的权重做比较（按「类选择器个数」计，
        # 因为两处都没用 ID / 属性 / 伪类，类个数即权重）
        import re as _re

        def weight(sel):
            return len(_re.findall(r"\.[A-Za-z_][\w-]*", sel))
        hl = _re.search(r"\.qkhl \.qk \.halo\{([^}]*)\}", self.tpl)
        main = _re.search(r"\.qk \.halo\{([^}]*)\}", self.tpl)
        self.assertIsNotNone(hl)
        self.assertIsNotNone(main)
        self.assertGreater(weight(".qkhl .qk .halo"),
                           weight(".qk .halo"),
                           "高亮规则的类选择器个数必须多于主线规则，"
                           "否则同权重下后写的会覆盖先写的")

    # ---------- P1-3：快捷按钮与滑块状态脱节 ----------
    def test_shortcut_buttons_reset_other_slider(self):
        """两个快捷预设互斥时，必须同时复位对方的滑块。"""
        self.assertIn("bRecent.className = '';", self.tpl)
        # bBig 分支里必须复位年代滑块
        idx_big = self.tpl.find("bBig.addEventListener('click'")
        idx_recent = self.tpl.find("bRecent.addEventListener('click'")
        self.assertGreater(idx_big, 0)
        self.assertGreater(idx_recent, idx_big)
        big_block = self.tpl[idx_big:idx_recent]
        self.assertIn("elYMin.value = y0", big_block,
                      "点「只看 M≥6」必须复位年代滑块")
        recent_block = self.tpl[idx_recent:idx_recent + 1200]
        self.assertIn("elMin.value = m0", recent_block,
                      "点「只看近 30 年」必须复位震级滑块")

    def test_recent_button_handles_stale_data_range(self):
        """数据止于 30 年前时，「近 30 年」不得把范围塌缩成 [y1,y1]。"""
        idx = self.tpl.find("bRecent.addEventListener('click'")
        self.assertGreater(idx, 0)
        block = self.tpl[idx:idx + 1400]
        self.assertIn("(t > y1) ? y0", block,
                      "近 30 年下界超过数据末年时应退回全区间")

    # ---------- P1-4：断裂导出计数与文件内容一致 ----------
    def test_fault_export_count_uses_unique_names(self):
        """导出按钮条数必须按**名称去重**，与 GeoJSON/CSV 的 feature 数一致。"""
        self.assertIn("Object.keys(fnSet).length", self.tpl,
                      "断裂导出条数必须用去重后的名称数")
        self.assertNotIn("var fn = FAULTS.length;", self.tpl,
                         "不得用段数（FAULTS.length）当导出条数")

    # ---------- P2-5：窄屏图例保留错误的行 ----------
    def test_narrow_legend_keeps_key_rows(self):
        """窄屏图例必须保留「地图要素」4 行，隐藏震级色标段与城市中心。"""
        self.assertNotIn("nth-last-child(n+4)", self.tpl,
                         "该规则恰好隐藏了要保留的 4 行，必须换掉")
        self.assertIn("nth-last-child(-n+6)", self.tpl,
                      "应隐藏 .leg 内最后 6 个兄弟（震级色标段 + 城市中心）")

    # ---------- P2-6：M≥6 标记与真值错位 17px ----------
    def test_quake_icon_fills_divicon_box(self):
        """.qk 必须铺满 divIcon 元素，否则光晕与内核偏离锚点 17px。"""
        self.assertIn(".qk{position:relative;width:100%;height:100%}", self.tpl,
                      ".qk 必须是 100%×100%，否则 50%/50% 落在 0×0 框上")
        self.assertNotIn(".qk{position:relative;width:0;height:0}", self.tpl)

    # ---------- P2-7：chip 重开丢背景色 ----------
    def test_chip_background_restored_on_reopen(self):
        """chip 重新打开时必须恢复背景色，否则近黑字配透明底不可读。"""
        self.assertIn("c.style.background = chipState[b] ? magColor(b) : '';",
                      self.tpl, "chip 背景色必须双向同步")

    # ---------- P2-9：hover 反复触发 fitBounds ----------
    def test_fault_hover_dedupes_fitbounds(self):
        """断裂表 hover 必须按行去重，否则划过一行触发多次 fitBounds。"""
        self.assertIn("var hlRow = null;", self.tpl)
        self.assertIn("if (tr === hlRow) return;", self.tpl,
                      "同一行的 hover 不应重复定位")

    # ---------- P3：排序方向 / 快捷键 / 图例溢出 / 死代码 ----------
    def test_date_column_defaults_descending(self):
        """从别的列切回「日期」列必须是倒序（与表头 ▼ 一致）。"""
        self.assertIn("sortAsc = (k!=='date')", self.tpl)
        self.assertNotIn("sortAsc = (k==='date')", self.tpl)

    def test_slash_focuses_search_everywhere(self):
        """概览 tab 没有搜索框，/ 必须能退到目录 tab 的搜索框。"""
        idx = self.tpl.find("e.key === '/'")
        self.assertGreater(idx, 0)
        block = self.tpl[idx:idx + 900]
        self.assertIn("|| document.querySelector('#qsearch')", block,
                      "/ 必须有兜底搜索框")
        self.assertIn("__switchSideTab", block,
                      "兜底时必须先切到可见的 tab 再聚焦")

    def test_legend_scrolls_when_compressed(self):
        """图例被压缩时必须可滚动，否则内容被裁掉且看不全。"""
        self.assertIn("leg.style.overflowY", self.tpl,
                      "压缩图例时必须打开 overflow-y")

    def test_no_dead_css_rules(self):
        """删除已失效的 CSS：.hist/.peak/tr.hl（旧直方图与未使用的行高亮）。"""
        self.assertNotIn(".hist{display:flex", self.tpl,
                         "旧 .hist 规则已失效（Python 端输出 .hbars）")
        self.assertNotIn(".peak{", self.tpl, ".peak 规则已失效")
        self.assertNotIn("#qtable tbody tr.hl td", self.tpl,
                         "hl 类从未被应用，属死代码")

    # ---------- Python 侧其他修复 ----------
    def test_fault_name_not_backfilled_from_zone(self):
        """断裂名不得用「断裂带名」回退（会造成错误合并）。

        v1.1.0 修订：**v3.6.0 的结论被实测推翻**。当时判定「FZN_Ch 带名
        恒为空、已整体下线」，但那是在磁盘缓存时代做的判断 —— 断库解析
        结果的 pickle 缓存 key 只含源文件 size+mtime，旧缓存里没有该字段
        就被读成了「数据源本身没有」。v1.1.0 在无缓存前提下复测断库前
        541 段：FZN_Ch 非空率 **18%**（如「乌拉山断裂带」「喜马拉雅造山带」），
        是真实分类信息，因此以 zone 字段恢复落盘。

        本断言随之收缩到**唯一仍然成立**的约束：name 只能来自 a[2]（FN_Ch）。
        把 a[0]（带名）当作断裂名的回退，会让不同断裂因同属一条带而被
        错误合并 —— 这才是当初真正要防的事，与字段是否落盘无关。
        """
        # 只看 name 的赋值表达式本身。旧写法取「赋值处往后 300 字符」做窗口，
        # 而 zone 的赋值紧跟其后 → 被误判成回退（是断言写得太粗，不是被测代码错）
        # 必须**限定在 parse_faults 内**查找：build_map.py 里另有
        # `name = zf.namelist()[0]`（GADM 解压）等同名赋值，
        # 全文首个匹配会命中它 —— 断言会假失败（这次就踩到了）。
        pf = self.src.find("def parse_faults")
        self.assertGreater(pf, 0, "找不到 parse_faults")
        m = re.search(r"^\s+name\s*=\s*.*$", self.src[pf:pf + 4000], re.M)
        self.assertIsNotNone(m, "找不到断裂名赋值语句")
        name_expr = m.group(0)
        self.assertIn("a[2]", name_expr, "断裂名必须来自 a[2]（FN_Ch）")
        self.assertNotIn("a[0]", name_expr,
                         "断裂名不得回退到 a[0]（断裂带名，会造成错误合并）")
        # zone / ref 已恢复落盘（v1.1.0）：仅作弹窗展示，不参与命名与分组
        self.assertIn('"zone": zone.strip()', self.src,
                      "断裂带名应以 zone 字段落盘")
        self.assertIn('"ref": ref.strip()', self.src,
                      "断层参考文献应以 ref 字段落盘（可溯源性的唯一依据）")

    def test_no_cache_means_logic_never_masked(self):
        """v1.0.5：「旧缓存静默遮蔽新逻辑」这类故障已从根上消除。

        v3.6.0 实测踩坑：断库解析缓存的 key 只含源文件 size+mtime，
        v3.5.0 新增字段后源文件没变 → 命中旧 pickle → 新字段被静默遮蔽
        （产物里恒为空，代码却明明写了 —— 无任何报错）。
        当时的修法是给缓存 key 加逻辑版本号；v1.0.5 直接移除全部磁盘
        缓存，故障模式随之消失。本用例守住这个前提：任何缓存设施
        （含 schema 版本表本身）都不得回归。
        """
        self.assertNotIn("CACHE_SCHEMA", self.src,
                         "缓存 schema 版本表不应回归（已无缓存）")
        self.assertNotIn("faultsparsed_v", self.src,
                         "断库 pickle 缓存 key 不应回归")
        self.assertNotIn('cache_key_path("usgs"', self.src,
                         "地震目录缓存不应回归")
        self.assertNotIn('cache_key_path("geocode"', self.src,
                         "地名缓存不应回归")
        # 每次运行都必须实时取数：主流程直接调用取数函数，无缓存旁路
        self.assertIn("fetch_earthquakes(lon, lat, args.radius, args.min_mag,",
                      self.src, "地震目录必须每次实时取数")
        self.assertIn("path = fetch_fault_db()", self.src,
                      "断库必须每次实时下载")

    def test_point_in_rings_bbox_pruning_equivalent(self):
        """bbox 剪枝必须**不改变**射线法判定结果（纯优化，不得改语义）。"""
        rings = [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0],
                  [5.0, 10.0], [5.0, 5.0], [0.0, 5.0], [0.0, 0.0]],
                 # 第二个不相交环（测试多环剪枝）
                 [[20.0, 20.0], [30.0, 20.0], [30.0, 30.0], [20.0, 30.0],
                  [20.0, 20.0]]]
        rbbs = bm._ring_bboxes(rings)
        pts = [(5.0, 5.0), (2.0, 2.0), (25.0, 25.0), (15.0, 15.0),
               (0.0, 0.0), (7.5, 7.5), (5.0, 7.5), (-1.0, 5.0), (11.0, 5.0)]
        for (x, y) in pts:
            a = bm._point_in_rings(x, y, rings)
            b = bm._point_in_rings(x, y, rings, rbbs)
            self.assertEqual(a, b,
                             "bbox 剪枝改变了判定结果 @ (%s, %s)" % (x, y))

    def test_legend_has_no_belt_entry(self):
        """图例中不得出现已下线的「带」类要素项。"""
        legend = self.tpl[self.tpl.find('data-mpane="legend"'):]
        legend = legend[:legend.find("</div>\n      </div>") + 20]
        self.assertNotIn("地震带", legend, "图例仍残留地震带项")
        self.assertNotIn("断裂带", legend, "图例仍残留断裂带项")
        self.assertNotIn("belt", legend.lower(), "图例仍残留 belt 项")

    def test_legend_swatches_match_map_styles(self):
        """图例色标必须与地图实际绘制一致（图例不能说谎）。"""
        # 行政边界：图例用 .sw.bnd（清晰红实线），对应地图的 #ff2d2d 红线
        self.assertIn(".sw.bnd", self.tpl, "行政边界图例缺少专用样式")
        self.assertIn("border-top-color:#ff2d2d", self.tpl,
                      "行政边界图例色值应与地图红线 #ff2d2d 一致")
        # 最大/最近地震：地图是 fill:false 的虚线圆环，图例也必须是圆环
        self.assertIn('class="c ring"', self.tpl,
                      "最大/最近地震图例应为虚线圆环（与地图一致）")
        self.assertIn(".c.ring", self.tpl, "缺少虚线圆环样式")
        # 城市中心：地图是 #4cc2ff，图例也必须是 #4cc2ff
        self.assertIn('style="background:#4cc2ff"></span>城市中心', self.tpl,
                      "城市中心图例色值应与地图 #4cc2ff 一致")

    def test_quake_dedup_handles_missing_id(self):
        """地震去重必须处理 id 缺失（否则全部塌缩成一条）。

        v1.4.0：原先断言源码里必须出现 `fid = "fp:%s|%s|%s|%s"` 这个
        **字面量**——指纹逻辑抽成 feat_fingerprint() 后断言立刻失效，
        而行为一点没变。改成直接调用该函数验证去重语义。
        """
        def feat(fid=None, t=None, xy=None, mag=None):
            return {"id": fid, "properties": {"time": t, "mag": mag},
                    "geometry": {"coordinates": xy or []}}

        # 有 id 时直接用 id
        self.assertEqual(bm.feat_fingerprint(feat(fid="abc")), "abc")
        # 无 id 时用 时间+经纬度+震级 构造指纹：不同事件必须不同
        a = bm.feat_fingerprint(feat(t=1000, xy=[117.0, 31.0], mag=4.0))
        b = bm.feat_fingerprint(feat(t=2000, xy=[117.0, 31.0], mag=4.0))
        c = bm.feat_fingerprint(feat(t=1000, xy=[118.0, 32.0], mag=5.0))
        self.assertNotEqual(a, b, "时间不同的无 id 事件不得塌缩成同一条")
        self.assertNotEqual(a, c, "位置/震级不同的无 id 事件不得塌缩成同一条")
        # 完全相同的输入必须稳定（同一事件多次出现才该被去重掉）
        self.assertEqual(a, bm.feat_fingerprint(feat(t=1000, xy=[117.0, 31.0],
                                                     mag=4.0)))

    def test_center_gcj02_order_matches_wgs84(self):
        """data.json 里两个中心点坐标序必须一致（都是 [lon, lat]）。"""
        self.assertIn('"center_gcj02": [round(center[1], 6), round(center[0], 6)]',
                      self.src)

    def test_iter_rings_keeps_holes(self):
        """边界环提取必须保留内环（洞），否则洞内区域被误判为边界内。"""
        self.assertIn("def _iter_rings(geom):", self.src)
        seg = self.src[self.src.find("def _iter_rings(geom):"):
                       self.src.find("def _iter_rings(geom):") + 1200]
        self.assertNotIn("yield poly[0]", seg,
                         "只取 poly[0] 会丢掉内环（洞）")
        self.assertIn("for ring in poly:", seg)

    def test_gadm_failure_is_not_silent(self):
        """GADM 获取失败必须打印告警，不能静默 continue。"""
        self.assertIn("[warn] GADM level=%d 获取失败", self.src)


class TestCencSource(unittest.TestCase):
    """国家地震台网（CENC）近期目录解析。

    用录制的 Wolfx 镜像样本（真实 CENC 审定数据）做 fixture，
    即便本机/沙箱连不上接口也能验证解析逻辑。
    """

    SAMPLE = json.dumps({
        "No1": {
            "type": "reviewed", "EventID": "CD.20261023200501.188",
            "time": "2026-10-23 19:54:42", "ReportTime": "2026-10-23 20:05:11",
            "location": "青海玉树州杂多县", "placeName": "青海玉树州杂多县",
            "magnitude": "3.7", "depth": "10", "latitude": "32.54",
            "longitude": "93.89", "intensity": "5"},
        "No2": {
            "type": "automatic", "EventID": "CD.20261023163312.504",
            "time": "2026-10-23 16:33:12", "location": "西藏那曲市巴青县",
            "magnitude": "2.1", "depth": "10", "latitude": "32.43",
            "longitude": "93.97"},
        "No3": {
            "type": "reviewed", "EventID": "CD.20260101000000.001",
            "time": "2026-01-01 08:00:00", "location": "安徽合肥市肥东县",
            "magnitude": "4.9", "depth": "10", "latitude": "31.86",
            "longitude": "117.28"},
        "md5": "deadbeef",
    }, ensure_ascii=False)

    def _fetch(self, min_mag=3.0):
        with mock.patch.object(bm, "http_get", return_value=self.SAMPLE):
            return bm.fetch_cenc_recent(min_mag=min_mag)

    def test_parses_reviewed_and_skips_md5(self):
        q = self._fetch()
        self.assertEqual(len(q), 2, "应跳过 md5 键、且过滤掉 M2.1 的 No2")
        self.assertIn("青海玉树州杂多县", [x["place"] for x in q])
        self.assertIn("安徽合肥市肥东县", [x["place"] for x in q])

    def test_source_and_event_id(self):
        q = self._fetch()
        for x in q:
            self.assertEqual(x["source"], "CENC")
            self.assertTrue(str(x["event_id"]).startswith("CD."))
            self.assertIn("cenc:", x["id"])

    def test_beijing_time_to_utc(self):
        """CENC 北京时间必须转成 UTC 毫秒，与 USGS 同口径。"""
        q = self._fetch()
        hebei = next(x for x in q if "肥东" in x["place"])
        expected = int((datetime.datetime(2026, 1, 1, 8, 0, 0)
                        - datetime.timedelta(hours=8)
                        - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
        self.assertEqual(hebei["time"], expected)

    def test_min_mag_filter(self):
        q = self._fetch(min_mag=4.0)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["mag"], 4.9)


class TestMergeCencUsgs(unittest.TestCase):
    """CENC 与 USGS 双源融合去重。"""

    def test_dedupe_same_event_keeps_cenc(self):
        # USGS 与 CENC 同一事件（时间±120s、位置<50km）→ 丢弃 USGS 副本
        cenc_ms = int((datetime.datetime(2026, 1, 1, 0, 0, 0)
                       - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
        usgs = [{"id": "usgs:x", "lon": 117.28, "lat": 31.86, "depth": 10,
                 "mag": 4.9, "place": "29 km E of Hefei", "time": cenc_ms,
                 "url": "u", "dist_km": 29.0, "type": "earthquake",
                 "source": "USGS"}]
        cenc = [{"id": "cenc:CD.20260101000000.001", "lon": 117.28, "lat": 31.86,
                 "depth": 10, "mag": 4.9, "place": "安徽合肥市肥东县",
                 "time": cenc_ms, "url": "", "dist_km": None,
                 "type": "earthquake", "source": "CENC",
                 "event_id": "CD.20260101000000.001"}]
        merged, info = bm._merge_cenc_usgs(usgs, cenc, 117.28, 31.86, 200.0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "CENC")
        # v1.2.0：去重条数必须回传，供主流程打印（静默合并 = 无法验证）
        self.assertEqual(info["cenc_added"], 1)
        self.assertEqual(info["usgs_dropped_dup"], 1)

    def test_usgs_only_event_retained(self):
        # 仅 USGS 有、CENC 没有的历史事件应保留
        usgs = [{"id": "usgs:y", "lon": 117.0, "lat": 31.0, "depth": 20,
                 "mag": 5.0, "place": "old", "time": 100000000000,
                 "url": "u", "dist_km": 50.0, "type": "earthquake",
                 "source": "USGS"}]
        cenc = []  # 近期无事件
        merged, info = bm._merge_cenc_usgs(usgs, cenc, 117.0, 31.0, 200.0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "USGS")
        self.assertEqual(info, {"cenc_added": 0, "usgs_dropped_dup": 0})


class TestV110DataFidelity(unittest.TestCase):
    """v1.1.0：数据真实性与字段完整性。

    本轮的四条实证：
      · USGS 目录里 mb/mw/ml/ms 混用（合肥 22 条实测 5 种量规），
        必须逐条保留口径，否则「最大震级」是拿不同尺子比出来的；
      · USGS 目录理论上有非天然事件（爆破/矿震），默认剔除且**必须可观测**；
      · CENC 的 type 字段语义是「审定状态」，与 USGS 的「事件类型」冲突，
        双源合并后必须统一到 status；
      · 断库 idx0（断裂带名）非空率 18%、idx10（参考文献）100%，
        此前被整体丢弃，现已恢复。
    """

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with io.open(os.path.join(here, "build_map.py"), encoding="utf-8") as fh:
            self.src = fh.read()
        with io.open(os.path.join(here, "template.html"), encoding="utf-8") as fh:
            self.tpl = fh.read()

    # ---------------- 震级口径 ----------------
    def test_mag_scale_mapping(self):
        self.assertEqual(bm.mag_scale_cn("mb"), "体波震级 mb")
        self.assertEqual(bm.mag_scale_cn("mw"), "矩震级 Mw")
        self.assertEqual(bm.mag_scale_cn("MB_LG"), "短周期面波震级 mb_Lg",
                         "量规代码大小写不敏感")
        self.assertEqual(bm.mag_scale_cn(""), "", "空量规必须返回空，不得臆造")
        self.assertEqual(bm.mag_scale_cn("xyz"), "xyz", "未收录代码原样返回")

    def test_stats_carry_mag_type_distribution(self):
        quakes = [
            {"mag": 4.0, "time": 0, "depth": 10, "mag_type": "mb",
             "source": "USGS", "status": "reviewed"},
            {"mag": 3.5, "time": 0, "depth": 10, "mag_type": "mw",
             "source": "USGS", "status": "reviewed"},
            {"mag": 3.1, "time": 0, "depth": 10, "mag_type": "",
             "source": "CENC", "status": "reviewed", "intensity": 5.0},
        ]
        st = bm.quake_stats(quakes)
        self.assertEqual(st["mag_types"], {"mb": 1, "mw": 1, "": 1})
        self.assertEqual(st["n_auto"], 0)
        self.assertEqual(st["max_intensity"], 5.0, "烈度应被统计（CENC 独有）")
        self.assertEqual(st["n_cenc"], 1)
        self.assertEqual(st["n_usgs"], 2)

    def test_auto_status_counted(self):
        """自动测定（未人工核定）条数必须单独统计并在页面提示。"""
        st = bm.quake_stats([{"mag": 3.0, "time": 0, "depth": None,
                              "mag_type": "ml", "source": "USGS",
                              "status": "automatic"}])
        self.assertEqual(st["n_auto"], 1)

    # ---------------- 非天然事件过滤 ----------------
    def test_non_tectonic_filtered_and_counted(self):
        """爆破/矿震等非天然事件必须剔除，且剔除量要能取回（不得静默）。"""
        feats = {
            "features": [
                {"id": "a", "properties": {"mag": 4.0, "type": "earthquake",
                                           "magType": "mb", "status": "reviewed",
                                           "time": 0, "place": "p"},
                 "geometry": {"coordinates": [117.28, 31.86, 10]}},
                {"id": "b", "properties": {"mag": 3.5, "type": "quarry blast",
                                           "magType": "ml", "status": "reviewed",
                                           "time": 0, "place": "采石场"},
                 "geometry": {"coordinates": [117.29, 31.87, 1]}},
                {"id": "c", "properties": {"mag": 3.6, "type": "explosion",
                                           "magType": "ml", "status": "reviewed",
                                           "time": 0, "place": "爆破"},
                 "geometry": {"coordinates": [117.30, 31.88, 0]}},
            ]}
        with mock.patch.object(bm, "http_get",
                               return_value=json.dumps(feats)):
            drops = {}
            q = bm.fetch_earthquakes(117.28, 31.86, 200.0, 3.0, 1900, 2026,
                                     drop_counts=drops)
        self.assertEqual(len(q), 1, "只应保留天然地震")
        self.assertEqual(q[0]["id"], "a")
        self.assertEqual(drops.get("non_tectonic"), 2,
                         "剔除量必须回收 —— 静默剔除等同于静默丢数据")

    def test_mag_type_carried_per_quake(self):
        feats = {"features": [
            {"id": "a", "properties": {"mag": 4.0, "type": "earthquake",
                                       "magType": "mb", "status": "reviewed",
                                       "time": 0, "place": "p"},
             "geometry": {"coordinates": [117.28, 31.86, 10]}}]}
        with mock.patch.object(bm, "http_get", return_value=json.dumps(feats)):
            q = bm.fetch_earthquakes(117.28, 31.86, 200.0, 3.0, 1900, 2026)
        self.assertEqual(q[0]["mag_type"], "mb")
        self.assertEqual(q[0]["mag_type_cn"], "体波震级 mb",
                         "中文口径由 Python 落盘，避免 JS 再抄一份映射表")

    # ---------------- CENC 语义对齐 ----------------
    def test_cenc_status_not_type(self):
        """CENC 的 type 是审定状态，不得写进事件类型字段。"""
        sample = json.dumps({
            "No1": {"type": "automatic", "EventID": "CD.1",
                    "time": "2026-10-23 16:33:12", "location": "西藏那曲市巴青县",
                    "magnitude": "4.5", "depth": "10", "latitude": "32.43",
                    "longitude": "93.97", "intensity": "6"}},
            ensure_ascii=False)
        with mock.patch.object(bm, "http_get", return_value=sample):
            q = bm.fetch_cenc_recent(min_mag=3.0)
        self.assertEqual(q[0]["type"], "earthquake", "事件类型恒为天然地震")
        self.assertEqual(q[0]["status"], "automatic", "审定状态走 status")
        self.assertEqual(q[0]["intensity"], 6.0, "烈度应被解析")
        self.assertEqual(q[0]["mag_type_cn"], "", "台网未标注量规，不得臆造")

    # ---------------- 断库字段完整性 ----------------
    def _gmt(self, attrs):
        return ("# FEATURE_DATA\n> \n# @D%s\n"
                "116.0 30.0 0\n116.1 30.1 0\n" % attrs)

    def test_parse_faults_carries_zone_and_ref(self):
        # 字段序 FZN_Ch|...|FN_Ch|FN_En|FSN_Ch|FSN_En|Fea_Ch|Fea_En|AGE|AGE_Des|RefE
        # 断库原文格式：# @D 之后直接接 FZN_Ch|FZN_En|FN_Ch|...（无前导分隔符）
        attrs = ("乌拉山断裂带|Wulashan zone|万寿山断裂|Wanshou"  # idx0..3
                 "|||正断|normal|Qh|Holocene|SMCAR; Xu et al., 2016")  # idx4..10
        path = os.path.join(tempfile.mkdtemp(), "f.gmt")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(self._gmt(attrs))
        f = bm.parse_faults(path)
        self.assertEqual(f[0]["zone"], "乌拉山断裂带")
        self.assertEqual(f[0]["ref"], "SMCAR; Xu et al., 2016")
        self.assertEqual(f[0]["name"], "万寿山断裂", "断裂名仍来自 FN_Ch")

    def test_clip_faults_keeps_new_fields(self):
        """裁剪不得吃掉新增属性（v1.1.0 修：原为逐字段重建，静默丢字段）。"""
        faults = [{"name": "n", "en_name": "e", "kind": "正断", "age": "Qh",
                   "zone": "乌拉山断裂带", "ref": "UAFS:Qingdao",
                   "coords": [[116.0, 30.0], [116.1, 30.1], [116.2, 30.2]]}]
        clipped = bm.clip_faults(faults, 116.1, 30.1, 200.0)
        self.assertTrue(clipped)
        self.assertEqual(clipped[0]["zone"], "乌拉山断裂带")
        self.assertEqual(clipped[0]["ref"], "UAFS:Qingdao")

    # ---------------- 模板与产物 ----------------
    def test_empty_quake_placeholder_colspan_matches_columns(self):
        """空目录占位行的 colspan 必须等于目录表实际列数（6 列）。

        v1.1.0 修：Python 侧写 4、模板 JS 侧写 6，两处不一致。
        """
        # v1.4.0：原先扫源码里「同时含 __QROWS__ 与 colspan 的那一行」——
        # build_html 拆分后两者落到不同行，断言立刻失效（产品行为没变）。
        # 改成行为断言：直接看**渲染结果**，这也是唯一真正要守的东西。
        html = bm.build_html("测试", "河北", 200, 3.0, [], [],
                             bm.quake_stats([]), [31.0, 117.0])
        self.assertIn('colspan="6"', html, "空目录占位必须占满 6 列")
        self.assertIn('colspan="6"', self.tpl, "模板空态占位应为 colspan=6")

    def test_magscale_row_in_template(self):
        self.assertIn("__MAGSCALE__", self.tpl)
        self.assertIn('"__MAGSCALE__"', self.src)

    def test_popup_unified_and_shows_new_fields(self):
        """三处弹窗统一到 quakePopupHtml，口径/烈度/审定状态只写一份。"""
        self.assertIn("function quakePopupHtml(", self.tpl)
        # 排除函数定义行本身（它同样含 "quakePopupHtml(q"），只数调用点
        calls = [l for l in self.tpl.splitlines()
                 if "quakePopupHtml(q" in l and "function " not in l]
        self.assertEqual(len(calls), 3,
                         "图层点 / 最大与最近地震 / 目录行点击 三处都应复用")
        for kw in ("震级口径", "震中烈度", "自动测定"):
            self.assertIn(kw, self.tpl, "弹窗应显示 %s" % kw)

    def test_fault_popup_shows_zone_and_ref(self):
        self.assertIn("所属断裂带", self.tpl)
        self.assertIn("资料来源", self.tpl)

    # ---------------- 死代码 ----------------
    def test_no_dead_symbols(self):
        self.assertNotIn("_PROV_SUFFIX", self.src, "未使用常量应已删除")
        bd = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "boundary_datav.py")
        with io.open(bd, encoding="utf-8") as fh:
            head = fh.read(1200)
        self.assertNotIn("import os", head, "boundary_datav.py 未使用 os")
        pk = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "pack.py")
        with io.open(pk, encoding="utf-8") as fh:
            psrc = fh.read()
        self.assertNotIn("def read_version", psrc, "未调用函数应已删除")

    def test_pack_strips_export_suffix(self):
        """pack.py 不得再产出 xxx_v1.0.0_v1.0.5 套娃目录（本次事故根因）。"""
        pk = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "pack.py")
        with io.open(pk, encoding="utf-8") as fh:
            psrc = fh.read()
        self.assertIn('_v\\d+(?:\\.\\d+)*$', psrc, "SKILL_NAME 应剥离导出后缀")


class TestV120MergeOrderAndCompleteness(unittest.TestCase):
    """v1.2.0 修复回归：双源合并顺序 + 目录完整性判定。

    这组用例全部来自**实测发现的真实故障**，不是凭空补的覆盖：
    那曲杂多（半径 80 km）一次跑出 earliest=2026 / latest=2025 ——
    最早年份晚于最晚年份，页面顶部的完整性徽章因此给出与数据相反的结论。
    """

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with io.open(os.path.join(here, "build_map.py"), encoding="utf-8") as fh:
            self.src = fh.read()
        with io.open(os.path.join(here, "template.html"), encoding="utf-8") as fh:
            self.tpl = fh.read()

    @staticmethod
    def _q(year, src="USGS"):
        """按**年份**造一条事件。

        注意：不能用「1000 / 9000」这种小整数当时间戳——它们是毫秒，
        两年都落在 1970-01-01，且间隔小于去重阈值 120 s，会被误判成
        同一次地震。测试数据必须落在真实量级上。
        """
        ts = int((datetime.datetime(year, 1, 1)
                  - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
        return {"id": "%s:%d" % (src, year), "lon": 117.0, "lat": 31.0,
                "depth": 10, "mag": 4.0, "place": "p", "time": ts,
                "url": "", "dist_km": 1.0, "type": "earthquake",
                "source": src, "mag_type": "mb", "intensity": None}

    def test_merge_returns_time_ascending(self):
        """台网近期事件不得整块排到历史 USGS 之前。"""
        # USGS 历史在前，CENC 近期在后 —— cenc_in + kept 拼接后天然乱序
        usgs = [self._q(1970), self._q(1990)]
        cenc = [self._q(2026, "CENC")]
        merged, _info = bm._merge_cenc_usgs(usgs, cenc, 117.0, 31.0, 200.0)
        times = [q["time"] for q in merged]
        self.assertEqual(times, sorted(times),
                         "合并结果必须按时间升序，否则目录表顺序错乱")
        self.assertEqual(len(merged), 3)

    def test_merge_reports_drop_counts(self):
        """合并必须回传新增/去重条数（静默合并 = 无法验证双源是否工作）。"""
        usgs = [self._q(1970), self._q(2026)]   # 第二条与 CENC 同期 → 应被去重
        cenc = [self._q(2026, "CENC")]
        _merged, info = bm._merge_cenc_usgs(usgs, cenc, 117.0, 31.0, 200.0)
        self.assertEqual(info["cenc_added"], 1)
        self.assertEqual(info["usgs_dropped_dup"], 1)

    def test_earliest_latest_independent_of_input_order(self):
        """earliest/latest 必须取 min/max，不能取插入顺序首尾。"""
        # 故意打乱输入：近期在前、历史在后（这正是合并 bug 时的真实形态）。
        # 修复前会得到 earliest=2026 / latest=1970 这种自相矛盾的结果。
        q = [self._q(2026), self._q(1970)]
        st = bm.quake_stats(q)
        self.assertLess(st["earliest_year"], st["latest_year"],
                        "earliest 必须早于 latest")
        self.assertEqual(st["earliest_year"], 1970)
        self.assertEqual(st["latest_year"], 2026)

    def test_epoch_zero_event_is_counted(self):
        """时间戳恰好为 0（1970-01-01T00:00:00）的事件不得被跳过。

        原写法 `if q["time"]:` 用真值判断，0 是 falsy → 该事件被静默排除在
        by_year / earliest 之外。这是概率极低但确实存在的静默丢数据。
        """
        q = dict(self._q(1970), mag_type="mb")
        self.assertEqual(q["time"], 0, "构造数据的时间戳必须是 0")
        st = bm.quake_stats([q])
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["earliest_year"], 1970)
        self.assertEqual(st["by_year"], {1970: 1})
        self.assertNotEqual(st["completeness"], "no-data")

    def test_completeness_note_matches_actual_earliest_year(self):
        """文案里的年份必须与实际 earliest 一致。

        合肥实测 earliest=1917 判为 sparse，旧文案却写「最早记录晚于 1960 年」
        —— 1917 明显不晚于 1960，页面结论与数据自相矛盾。
        """
        st = bm.quake_stats([self._q(1917)])
        self.assertEqual(st["earliest_year"], 1917)
        self.assertEqual(st["completeness"], "sparse")
        self.assertIn("1917", st["completeness_note"])
        self.assertNotIn("晚于 1960", st["completeness_note"],
                         "1917 年不晚于 1960 年，旧文案与事实不符")

    def test_completeness_note_no_data_branch(self):
        st = bm.quake_stats([])
        self.assertEqual(st["completeness"], "no-data")
        self.assertEqual(st["completeness_note"], "该范围内无目录记录")
        self.assertIsNone(st["earliest_year"])

    def test_intensity_stored_as_int(self):
        """烈度是中国烈度表整数，不得落盘成 9.0 再显示「9.0 度」。"""
        raw = {"magnitude": "7.0", "latitude": "32.5", "longitude": "93.9",
               "depth": "10", "time": "2026-10-22 01:00:00",
               "location": "西藏那曲市巴青县", "intensity": "9",
               "EventID": "CD.20261022010000.001", "type": "reviewed"}
        with mock.patch.object(bm, "http_get",
                               return_value=json.dumps({"k": raw})):
            out = bm.fetch_cenc_recent(3.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["intensity"], 9)
        self.assertIsInstance(out[0]["intensity"], int)
        self.assertEqual(out[0]["status"], "reviewed")
        # 死字段 report_time 已移除（落盘但无人读 = 冗余）
        self.assertNotIn("report_time", out[0])

    def test_max_intensity_rendered_in_overview(self):
        """stats 算了 max_intensity 就必须渲染出来，否则是死数据。"""
        self.assertIn('"__MAXINTROW__"', self.src)
        self.assertIn("__MAXINTROW__", self.tpl)


class TestV130HistogramScaleWarning(unittest.TestCase):
    """v1.3.0：年代直方图不得把「几天的台网余震」与「历史全年计数」混画。

    实测那曲杂多（radius 80 km）：CENC 只覆盖 **1 天**（2026-10-22 巴青
    M7.0 余震）却贡献 36 条，而历史各年每年仅 1~8 条。同轴绘制后 2026 年
    柱子异常高耸，读起来像「地震活动剧增」——图照出、不报错，典型的静默误导。
    """

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with io.open(os.path.join(here, "build_map.py"), encoding="utf-8") as fh:
            self.src = fh.read()
        with io.open(os.path.join(here, "template.html"), encoding="utf-8") as fh:
            self.tpl = fh.read()

    def test_template_has_warning_slot(self):
        self.assertIn("__YEARSCALEWARN__", self.tpl)
        self.assertIn('"__YEARSCALEWARN__"', self.src)
        self.assertIn(".ywarn", self.tpl, "需要配套样式，否则提示无样式")

    def test_warn_logic_present_and_guarded(self):
        """提示必须带触发条件，不能无条件显示（多数城市无台网数据）。

        v1.4.0：原先断言源码里必须出现 `span_days < 90` / `len(cenc_times) >= 3`
        这两个**字面量**——重构时把条件改写成早返回（`if span_days >= 90
        or len(cenc_times) < 3: return ""`），断言立刻失效，而判定语义完全
        没变。断言该守**判定结果**，不是守表达式写法。
        """
        DAY = 86400000
        t0 = 1760000000000  # 2025-10 附近的一个毫秒基准

        def cenc(n, span_days):
            return [{"time": t0 + i * (span_days * DAY // max(1, n - 1)),
                     "source": "CENC"} for i in range(n)]

        # ① 触发：>=3 条且时间跨度 < 90 天
        self.assertTrue(bm.yearscale_warn_html(cenc(5, 2)),
                        "5 条台网事件集中在 2 天内，必须提示量纲混用")
        # ② 不触发：跨度 >= 90 天（台网覆盖够长，与全年计数可比）
        self.assertEqual(bm.yearscale_warn_html(cenc(5, 200)), "",
                         "跨度 200 天属正常覆盖，不应误报")
        # ③ 不触发：不足 3 条（样本太少，柱状差异不构成误导）
        self.assertEqual(bm.yearscale_warn_html(cenc(2, 1)), "",
                         "不足 3 条不应提示")
        # ④ 不触发：根本没有台网数据（合肥等绝大多数城市）
        self.assertEqual(bm.yearscale_warn_html(
            [{"time": t0, "source": "USGS"}] * 10), "",
            "纯 USGS 目录不应出现台网量纲提示")

    def test_warn_mentions_different_units(self):
        """文案必须点明「不是同一量纲」，否则提示等于没说。"""
        self.assertIn("不是同一量纲", self.src)
        self.assertIn("不代表地震活动增强", self.src)


class TestV140CodeStructure(unittest.TestCase):
    """v1.4.0 代码结构优化：巨型函数拆分 + 无效阈值清理。

    拆分前的实测基线（cProfile/AST 统计）：
        build_html  281 行 / 分支复杂度 62
        main        286 行 / 分支复杂度 33
    两个函数各自塞了五段互不相干的渲染/编排逻辑，改任何一块都要在
    近 300 行里定位，且无法对单块断言。

    拆分**必须保持输出不变**：已用 v1.3.0 的真实产物做过逐字节回归
    （合肥 125399 字节 / 杂多 171128 字节，diff 为 0）。
    """

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with io.open(os.path.join(here, "build_map.py"), encoding="utf-8") as fh:
            self.src = fh.read()
        self.lines = self.src.splitlines()

    @staticmethod
    def _span(name):
        """取函数源码（含签名行到 return/下个 def 之前）。"""
        src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "build_map.py"), encoding="utf-8").read()
        i = src.find("\ndef %s(" % name)
        if i < 0:
            return ""
        j = src.find("\ndef ", i + 1)
        return src[i:j] if j > 0 else src[i:]

    def test_build_html_is_assembly_only(self):
        """build_html 只做组装，不再塞渲染逻辑（拆分前 281 行）。"""
        span = self._span("build_html")
        self.assertLessEqual(len(span.splitlines()), 100,
                             "build_html 应只做占位符组装（实测 %d 行）"
                             % len(span.splitlines()))
        # 组装层不应再有 HTML 拼接字面量
        self.assertNotIn("<tr ", span, "HTML 行构造应已拆到 *_html() 函数里")

    def test_main_is_orchestration_only(self):
        """main 只做编排与阶段打印（拆分前 286 行 / 复杂度 33）。"""
        span = self._span("main")
        self.assertLessEqual(len(span.splitlines()), 110,
                             "main 应只做编排（实测 %d 行）"
                             % len(span.splitlines()))
        self.assertNotIn("<tr ", span)
        self.assertNotIn("json.dump(", span, "落盘应已拆到 write_outputs()")

    def test_render_helpers_exist_and_are_pure(self):
        """拆出的渲染函数必须存在，且**不依赖调用顺序**（可单独调用）。"""
        for fn in ("mag_rows_html", "group_faults", "fault_rows_html",
                   "year_histogram_html", "quake_rows_html", "scope_text",
                   "src_note_text", "magscale_text", "maxintensity_row_html",
                   "yearscale_warn_html"):
            self.assertIn("def %s(" % fn, self.src)
            self.assertTrue(callable(getattr(bm, fn)),
                            "%s 应可被直接调用（纯函数）" % fn)

    def test_ms_to_dt_handles_pre_1970(self):
        """1900 年前的事件（负时间戳）必须能转换。

        Windows 的 time.gmtime 不支持负时间戳，早期版本用它会在
        1917 年霍山地震这类事件上直接抛异常，所以统一走 datetime。
        """
        y1917 = bm.ms_to_year(int((datetime.datetime(1917, 1, 1)
                                   - datetime.datetime(1970, 1, 1)
                                   ).total_seconds() * 1000))
        self.assertEqual(y1917, 1917, "负时间戳（1917 年）必须正确换算")
        self.assertEqual(bm.ms_to_year(0), 1970)
        # time.gmtime 在这类值上会抛 —— 反向确认实现走的是 datetime。
        # 注意：**docstring 里也写了 time.gmtime 这个名字**（说明为什么不用它），
        # 直接全文断言会被自己的注释打脸，所以只查非文档字符串的代码行。
        code = "\n".join(l for l in self._span("ms_to_dt").splitlines()
                         if not l.strip().startswith(("#", '"""', "Windows")))
        self.assertNotIn("time.gmtime", code, "实现不得用 time.gmtime")
        self.assertIn("datetime.timedelta", code)

    def test_completeness_of_is_pure_and_year_consistent(self):
        """完整性结论必须与实际年份自洽（v1.2.0 的核心修复，抽出后仍成立）。"""
        for year, code in ((1899, "full"), (1917, "sparse"), (1959, "sparse"),
                           (1960, "modern"), (2020, "modern")):
            got, note = bm.completeness_of(year)
            self.assertEqual(got, code, "%d 年应判为 %s" % (year, code))
            self.assertIn(str(year), note,
                          "文案里的年份必须与实际一致（%d / %s）" % (year, note))
        code, note = bm.completeness_of(None)
        self.assertEqual(code, "no-data")

    def test_plan_batches_covers_range_exactly(self):
        """分批规划：不超上限时不分批；分批时区间必须**连续无重叠全覆盖**。"""
        P = bm.USGS_PAGE
        self.assertEqual(bm.plan_batches(1900, 2026, None), [],
                         "总量未知时不分批")
        self.assertEqual(bm.plan_batches(1900, 2026, P), [],
                         "未超上限时不分批")
        batches = bm.plan_batches(1900, 2026, P * 10)
        self.assertGreater(len(batches), 1)
        self.assertEqual(batches[0][0], 1900, "必须从起始年开始")
        self.assertEqual(batches[-1][1], 2026, "必须覆盖到结束年")
        for i in range(len(batches) - 1):
            self.assertEqual(batches[i][1] + 1, batches[i + 1][0],
                             "相邻区间必须连续（无空隙、无重叠）")

    def test_boundary_dp_removed_as_ineffective(self):
        """边界抽稀已删（实测收益 0%），常量不得再出现。

        实测合肥 1080→1080 / 杂多 1221→1221：eps=0.0001 下一个点都压不掉；
        而压得动的 eps（0.001）会带来 111 m 偏移，放大到 z15 是 28 px。
        """
        self.assertNotIn("BND_DP_EPS =", self.src,
                         "边界抽稀容差已确认为无效优化，不应复活")
        # 但断裂侧的有效抽稀必须保留
        self.assertIn("FAULT_DP_EPS", self.src)

    def test_slim_rings_still_closes_rings(self):
        """不做抽稀后，环闭合保护仍必须有效（否则填充会漏）。"""
        ring = [[117.0, 31.0], [118.0, 31.0], [118.0, 32.0], [117.0, 31.0]]
        slim = bm.slim_rings([ring])
        self.assertEqual(len(slim), 1)
        self.assertEqual(slim[0][0], slim[0][-1], "环必须闭合")
        # 未闭合的输入也必须被补闭
        slim2 = bm.slim_rings([[[117.0, 31.0], [118.0, 31.0], [118.0, 32.0]]])
        self.assertEqual(slim2[0][0], slim2[0][-1], "未闭合的环应被补闭")
        self.assertEqual(bm.slim_rings([]), [])
        self.assertEqual(bm.slim_rings(None), [])

    def test_quake_stats_uses_completeness_of(self):
        """quake_stats 必须复用抽出的完整性判定（不能两处各判一套）。"""
        self.assertIn("completeness_of(earliest)", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
