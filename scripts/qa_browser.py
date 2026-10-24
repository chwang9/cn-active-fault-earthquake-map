# -*- coding: utf-8 -*-
"""产物浏览器验收（v1.4.0）：Playwright 做 DOM 探测 + 交互测试。

与 selftest.py 的分工：selftest 查**代码与数据**（214 项，无需浏览器）；
本脚本查**渲染结果**——骨架、概览、目录表、tab、图层开关、点击弹窗、
搜索、导出、控制台错误（42 项）。两者互补，不能互相替代。

用法（必须先起本地 HTTP 服务，file:// 会被浏览器拦截）：
    python -m http.server 8899
    python scripts/qa_browser.py http://127.0.0.1:8899/<产物>.html

依赖：playwright + 本机 Chrome（channel="chrome"，免下载 chromium）。

选择器经实地核对，不要用猜的：
    .hbars / .b   直方图（不是 .hist）
    .lg           图例（不是 .legend）
    label > .sw   图层开关（.sw 与图例色块重名；状态 .on 在**父 label** 上）
    .ywarn        v1.3.0 量纲警示条
    地震圆点在 .leaflet-marker-pane（pane:'markerPane'，默认 SVG；
       模板注释明确禁止 canvas——canvas 会整块挡住下层断裂的点击）

点击测试的关键（v1.4.0 修）：**不能点包围盒中心**。断层多是陡倾直线
（合肥的郯庐系尤其明显），包围盒中心与线上点能差 4~12 px，而线宽只有
2~3 px，必然点空（实测合肥 9 段中 8 段的 bbox 中心都不在线上）。
要用 SVG 的 getPointAtLength(L/2) 取**线上**中点，再经 getScreenCTM
换算成屏幕坐标。
"""
import json
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %-44s %s" % ("PASS" if ok else "FAIL", name, detail))


with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1600, "height": 1000})

    console_err, page_err = [], []
    bad_resp = []
    pg.on("console", lambda m: console_err.append(m.text)
          if m.type == "error" else None)
    pg.on("pageerror", lambda e: page_err.append(str(e)))
    pg.on("response", lambda r: bad_resp.append((r.url, r.status))
          if r.status >= 400 else None)

    pg.goto(URL, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(2500)

    print("=" * 78)
    print("A. 骨架")
    print("=" * 78)
    for sel, desc in [("#map", "地图容器"), (".mstat", "统计概览"),
                      ("#qtable", "地震目录表"), (".hbars", "直方图柱容器"),
                      (".histax", "直方图轴"), (".lg", "图例"),
                      (".sw", "图层开关"), (".tab", "tab 按钮")]:
        n = pg.locator(sel).count()
        check("存在 %s (%s)" % (desc, sel), n > 0, "count=%d" % n)

    box = pg.evaluate("""() => { const e=document.getElementById('map');
        if(!e) return null; const r=e.getBoundingClientRect();
        return {w:r.width,h:r.height}; }""")
    check("地图容器可见尺寸", box and box["w"] > 300 and box["h"] > 300, str(box))

    print()
    print("=" * 78)
    print("B. 概览渲染")
    print("=" * 78)
    ov = pg.evaluate("""() => { const o={};
        document.querySelectorAll('.mrow .r').forEach(r=>{
          const k=r.querySelector('b'), v=r.querySelector('.v');
          if(k&&v) o[k.textContent.trim()]=v.textContent.trim(); }); return o; }""")
    print("   ", json.dumps(ov, ensure_ascii=False))
    bad = [k for k, v in ov.items()
           if (not v) or v == "—" or "undefined" in v or "__" in v]
    check("概览无空/undefined/未替换占位", not bad, str(bad))
    for key in ("统计范围", "地震来源", "震级口径", "时间跨度"):
        check("概览含「%s」" % key, key in ov)

    cards = pg.evaluate("""() => { const o={};
        document.querySelectorAll('.mk .c').forEach(c=>{
          const k=c.querySelector('.k'), v=c.querySelector('.v');
          if(k&&v) o[k.textContent.trim()]=v.textContent.trim(); }); return o; }""")
    print("   ", json.dumps(cards, ensure_ascii=False))
    check("统计卡 4 项", len(cards) >= 4)
    check("统计卡无 undefined", not any("undefined" in v for v in cards.values()))

    print()
    print("=" * 78)
    print("C. 目录表")
    print("=" * 78)
    tbl = pg.evaluate("""() => { const t=document.getElementById('qtable');
        if(!t) return null;
        const head=[...t.querySelectorAll('thead th')].map(x=>x.textContent.trim());
        const rows=[...t.querySelectorAll('tbody tr')];
        return {ncol:head.length, head, nrow:rows.length,
                hasScope:rows.some(r=>r.getAttribute('data-scope')),
                hasSrc:rows.some(r=>r.getAttribute('data-src'))}; }""")
    print("   表头:", tbl["head"], " 行数:", tbl["nrow"])
    check("列数为 6", tbl["ncol"] == 6, "%d" % tbl["ncol"])
    check("行带 data-scope", tbl["hasScope"])
    check("行带 data-src", tbl["hasSrc"])
    check("有数据行", tbl["nrow"] > 0)

    # 空态 colspan：把筛选拉到不可能区间，触发无记录
    empty = pg.evaluate("""() => {
        const inp=document.getElementById('fMagMin');
        if(!inp) return null;
        inp.value = inp.max; inp.dispatchEvent(new Event('input',{bubbles:true}));
        inp.dispatchEvent(new Event('change',{bubbles:true}));
        const t=document.getElementById('qtable');
        const rows=[...t.querySelectorAll('tbody tr')];
        const c = rows[0] && rows[0].children[0];
        return {nrow:rows.length, colspan:c?c.getAttribute('colspan'):null,
                txt:rows[0]?rows[0].textContent.trim().slice(0,20):''};
    }""")
    print("   空态:", empty)
    if empty and empty["nrow"] == 1 and empty["colspan"]:
        check("空态占位 colspan==6", empty["colspan"] == "6",
              empty["colspan"])
    # 复原
    pg.evaluate("""() => { const i=document.getElementById('fMagMin');
        if(i){ i.value=i.min; i.dispatchEvent(new Event('input',{bubbles:true}));
               i.dispatchEvent(new Event('change',{bubbles:true})); } }""")
    pg.wait_for_timeout(300)

    srt = pg.evaluate("""() => { const t=document.getElementById('qtable');
        const th=[...t.querySelectorAll('thead th')].find(x=>/震级/.test(x.textContent));
        if(!th) return null;
        const g=()=>[...t.querySelectorAll('tbody tr')].slice(0,4)
                  .map(r=>r.children[1]?r.children[1].textContent.trim():'');
        const before=g(); th.click(); const after=g(); return {before,after}; }""")
    if srt:
        print("   排序:", srt["before"], "->", srt["after"])
        check("表头排序生效", srt["before"] != srt["after"])

    print()
    print("=" * 78)
    print("D. Tab 切换")
    print("=" * 78)
    for want, key in [("地震目录", "quake"), ("断层清单", "fault"), ("概览", "overview")]:
        act = pg.evaluate("""(want) => {
            const b=[...document.querySelectorAll('.tab')].find(x=>x.textContent.trim()===want);
            if(b) b.click();
            return [...document.querySelectorAll('.pane')]
                .filter(p=>p.classList.contains('on'))
                .map(p=>p.getAttribute('data-pane')); }""", want)
        check("切到「%s」激活 %s" % (want, key), act == [key], str(act))

    print()
    print("=" * 78)
    print("E. 图层开关")
    print("=" * 78)
    # 注意：.sw 被两种东西复用 —— 图层开关（父 <label class=on>）与
    # 图例色块（.lctrl .leg .lg .sw）。状态 .on 加在**父 label** 上，
    # 不是 .sw 自身。只取 label 下的才是真正的开关。
    sw = pg.evaluate("""() => [...document.querySelectorAll('label > .sw')]
        .map(e=>({id:e.parentElement.getAttribute('data-id'),
                  txt:e.parentElement.textContent.trim().slice(0,16),
                  on:e.parentElement.classList.contains('on')}))""")
    print("   ", json.dumps(sw, ensure_ascii=False)[:400])
    check("图层开关 >=3 个", len(sw) >= 3, "%d" % len(sw))
    check("默认开关全部为 on", all(x["on"] for x in sw),
          str([x["id"] for x in sw if not x["on"]]))

    # 逐个开关切换：视觉状态与图层数必须同步
    tog = pg.evaluate("""() => {
        const labs=[...document.querySelectorAll('label > .sw')]
            .map(e=>e.parentElement);
        // 底图/注记是**瓦片 img**不是 path，必须一并统计，
        // 否则 sat / label 两个开关看起来「点了没反应」。
        const snap=()=>({
            marker: document.querySelectorAll('.leaflet-marker-pane path').length,
            overlay: document.querySelectorAll('.leaflet-overlay-pane path').length,
            bnd: document.querySelectorAll('.leaflet-boundary-pane path').length,
            tiles: document.querySelectorAll('.leaflet-tile').length,
            tilePanes: document.querySelectorAll('.leaflet-tile-pane').length,
        });
        const out=[];
        for (const L2 of labs){
            const id=L2.getAttribute('data-id');
            const wasOn=L2.classList.contains('on');
            const b=snap(); L2.click(); const a=snap(); L2.click(); const r=snap();
            out.push({id, wasOn, afterOn:L2.classList.contains('on'),
                      changed: JSON.stringify(b)!==JSON.stringify(a),
                      // 「还原」看 UI 开关状态是否回到初始，而非瓦片 img 数——
                      // sat/label 是 Leaflet 瓦片图层，关闭再打开后瓦片异步加载，
                      // 两次 click 间 img 尚未渲染，用数量判定会误判 FAIL。
                      restored: L2.classList.contains('on')===wasOn});
        }
        return out;
    }""")
    for t in tog:
        print("   开关[%s] 初始on=%s 点击后变化=%s 还原=%s"
              % (t["id"], t["wasOn"], t["changed"], t["restored"]))
    check("每个开关点击后图层数都变化",
          all(t["changed"] for t in tog),
          str([t["id"] for t in tog if not t["changed"]]))
    check("开关可还原", all(t["restored"] for t in tog),
          str([t["id"] for t in tog if not t["restored"]]))
    check("开关状态回到 on", all(t["afterOn"] for t in tog))

    print()
    print("=" * 78)
    print("F. 点击交互（地震点 / 断层 弹窗）")
    print("=" * 78)
    # 点击一个地震圆点
    pop = pg.evaluate("""() => {
        const ps=[...document.querySelectorAll('.leaflet-marker-pane path')];
        const hit=ps.find(p=>p.getAttribute('fill') && p.getAttribute('fill')!=='none');
        if(!hit) return {note:'no quake path'};
        const r=hit.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, fill:hit.getAttribute('fill')};
    }""")
    if pop.get("x"):
        pg.mouse.click(pop["x"], pop["y"])
        pg.wait_for_timeout(800)
        ptxt = pg.evaluate("""() => {
            const p=document.querySelector('.leaflet-popup-content');
            return p ? p.textContent.trim().slice(0,200) : null; }""")
        print("   地震弹窗:", (ptxt or "(无弹窗)")[:160])
        check("点击地震点弹出详情", bool(ptxt))
        if ptxt:
            for k in ("震源深度", "距中心", "震级口径"):
                check("弹窗含「%s」" % k, k in ptxt)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)

    # 点击断层
    # v1.4.0 修：**不能点包围盒中心** —— 断层多是陡倾直线（合肥的郯庐系
    # 尤其明显），包围盒中心与线上点能差 4~12 px，而线宽只有 2~3 px，
    # 必然点空（实测合肥 9 段中 8 段的 bbox 中心都不在线上）。
    # 正确做法：用 SVG 的 getPointAtLength(L/2) 取**线上**的中点，
    # 再用 getScreenCTM 换成屏幕坐标。
    pop2 = pg.evaluate("""() => {
        const ps=[...document.querySelectorAll('.leaflet-overlay-pane path')];
        const hit=ps.find(p=>(p.getAttribute('stroke')||'')==='#ffae73');
        if(!hit || !hit.getTotalLength) return {note:'no fault path'};
        const L=hit.getTotalLength();
        const mid=hit.getPointAtLength(L/2);
        const m=hit.getScreenCTM();
        if(!m) return {note:'no CTM'};
        const sp=new DOMPoint(mid.x, mid.y).matrixTransform(m);
        return {x:sp.x, y:sp.y}; }""")
    if pop2.get("x"):
        pg.mouse.click(pop2["x"], pop2["y"])
        pg.wait_for_timeout(800)
        ptxt = pg.evaluate("""() => {
            const p=document.querySelector('.leaflet-popup-content');
            return p ? p.textContent.trim().slice(0,200) : null; }""")
        print("   断层弹窗:", (ptxt or "(无弹窗)")[:160])
        check("点击断层弹出详情", bool(ptxt))
        if ptxt:
            check("断层弹窗含「活动方式」", "活动方式" in ptxt)
            check("断层弹窗含「最新活动年代」", "最新活动年代" in ptxt)
        pg.keyboard.press("Escape")

    print()
    print("=" * 78)
    print("G. 搜索与导出")
    print("=" * 78)
    sea = pg.evaluate("""() => {
        const inp=document.querySelector('.searchrow input, input[type=search], #qSearch');
        if(!inp) return {note:'no search input'};
        const t=document.getElementById('qtable');
        const before=t?t.querySelectorAll('tbody tr').length:-1;
        inp.value='zzzz-不存在-9999';
        inp.dispatchEvent(new Event('input',{bubbles:true}));
        const after=t?t.querySelectorAll('tbody tr').length:-1;
        inp.value=''; inp.dispatchEvent(new Event('input',{bubbles:true}));
        const rest=t?t.querySelectorAll('tbody tr').length:-1;
        return {before, after, rest, id:inp.id}; }""")
    print("   搜索:", sea)
    if sea.get("after") is not None:
        check("搜索无匹配 -> 行数减少", sea["after"] <= sea["before"],
              "%s -> %s" % (sea["before"], sea["after"]))
        check("清空搜索 -> 行数恢复", sea["rest"] == sea["before"],
              "%s vs %s" % (sea["rest"], sea["before"]))

    dl = pg.evaluate("""() => {
        const btns=[...document.querySelectorAll('.dlbtn')];
        return {n:btns.length, txt:btns.map(b=>b.textContent.trim().slice(0,12))}; }""")
    print("   导出按钮:", dl)
    check("存在导出按钮", dl["n"] > 0, str(dl["txt"]))

    print()
    print("=" * 78)
    print("H. 控制台 / 网络")
    print("=" * 78)
    real = [e for e in console_err
            if "favicon" not in e.lower() and "net::" not in e
            and "Failed to load resource" not in e]
    for e in real[:8]:
        print("   [console]", e[:150])
    for e in page_err[:8]:
        print("   [pageerror]", e[:150])
    for u, s in bad_resp[:8]:
        print("   [%s] %s" % (s, u[:110]))
    check("无 JS 运行时错误", not real and not page_err,
          "console=%d pageerror=%d" % (len(real), len(page_err)))
    check("无 4xx/5xx 资源", not bad_resp, "%d 条" % len(bad_resp))

    pg.screenshot(path="qa_shot.png")
    b.close()

print()
print("=" * 78)
nf = sum(1 for _, ok, _ in results if not ok)
print("合计 %d 项，失败 %d 项" % (len(results), nf))
for n, ok, d in results:
    if not ok:
        print("   FAIL:", n, "|", d)
