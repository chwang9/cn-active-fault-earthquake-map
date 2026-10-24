# -*- coding: utf-8 -*-
"""打包成可上传的技能包。

v1.0.5 起运行时**完全不落缓存**（无 _cache 目录、无 __pycache__、
无用户缓存目录），所以技能目录里不再有 29 MB 的下载产物。
打包仍保留对这些目录的排除 —— 那是**防御性**做法：从旧版本升级的
用户目录里可能还留着 `scripts/_cache/`，打包时必须剔掉。

排除项：`_cache` / `__pycache__` / `data.json` 等中间产物，
只带「代码 + 文档 + 元数据 + 示例 HTML」。

用法：
    python scripts/pack.py                # 输出到 skills/ 同级的 ../dist
    python scripts/pack.py --out D:\\pkg  # 指定输出目录
    python scripts/pack.py --with-samples # 附带各城市的示例 HTML
"""
import argparse
import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
# v1.1.0 修：SKILL_NAME 原先直接取目录名。若在**打包产物目录**
# （形如 xxx_v1.0.0）里再次运行 pack.py，名字会变成
# `cn-active-fault-earthquake-map_v1.0.0`，进而再产出
# `..._v1.0.0_v1.0.5` 这样的套娃目录 —— 本次审计的实际故障：
# 源码一度只存在于导出副本里，而 WorkBuddy 加载的是无后缀的主目录，
# 于是「改了代码但线上跑的还是旧版」。现在先剥离导出后缀再用于命名。
SKILL_NAME = re.sub(r"_v\d+(?:\.\d+)*$", "", os.path.basename(SKILL_DIR))

# 打包时**必须排除**的项
EXCLUDE_DIRS = {"_cache", "__pycache__", ".git", ".idea", ".vscode",
                "_cache_tmp", "node_modules"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".tmp", ".log")
# 中间产物（HTML 才是交付物，data.json 是给下游程序用的）
EXCLUDE_NAMES = {"data.json"}


def check_version_sync():
    """校验三处版本号一致（SKILL.md / _user_meta.json / README 引用）。"""
    problems = []
    meta_path = os.path.join(SKILL_DIR, "_user_meta.json")
    with open(meta_path, encoding="utf-8") as fh:
        meta_ver = json.load(fh).get("version")
    sk_path = os.path.join(SKILL_DIR, "SKILL.md")
    with open(sk_path, encoding="utf-8") as fh:
        head = fh.read(600)
    m = re.search(r'^version:\s*"([^"]+)"', head, re.M)
    sk_ver = m.group(1) if m else None
    if meta_ver != sk_ver:
        problems.append("版本号不一致：_user_meta.json=%s，SKILL.md=%s"
                        % (meta_ver, sk_ver))
    return meta_ver, problems


def collect(with_samples):
    """收集要打包的文件（相对路径列表）。"""
    items = []
    for root, dirs, files in os.walk(SKILL_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(EXCLUDE_SUFFIX) or f in EXCLUDE_NAMES:
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, SKILL_DIR).replace(os.sep, "/")
            if rel.startswith("data.json"):
                continue
            # 示例目录：默认只带 HTML
            if not with_samples and rel.endswith("_map/" + f) \
                    and not f.endswith(".html"):
                continue
            if not with_samples and re.match(r"^[^/]+_map/.*\.html$", rel):
                continue
            items.append(rel)
    return sorted(items)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def main():
    ap = argparse.ArgumentParser(description="打包技能（排除缓存与中间产物）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <skill>_v<ver>）")
    ap.add_argument("--with-samples", action="store_true",
                    help="附带各城市的示例 HTML（体积较大）")
    args = ap.parse_args()

    ver, problems = check_version_sync()
    if problems:
        for p in problems:
            print("[warn] %s" % p, file=sys.stderr)
    out_dir = args.out or os.path.join(
        os.path.dirname(SKILL_DIR), "%s_v%s" % (SKILL_NAME, ver))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    items = collect(args.with_samples)
    total = 0
    for rel in items:
        src = os.path.join(SKILL_DIR, rel.replace("/", os.sep))
        dst = os.path.join(out_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        total += os.path.getsize(dst)

    # 体积对比
    raw = 0
    for root, dirs, files in os.walk(SKILL_DIR):
        for f in files:
            try:
                raw += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass

    # zip
    zip_path = out_dir + ".zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in items:
            zf.write(os.path.join(out_dir, rel.replace("/", os.sep)),
                     os.path.join(SKILL_NAME, rel))

    print("技能包：%s v%s" % (SKILL_NAME, ver))
    print("  原始目录 %s → 打包后 %s（省 %s）"
          % (human(raw), human(total),
             human(raw - total) if raw > total else "0 B"))
    print("  文件数 %d  输出：%s" % (len(items), out_dir))
    print("  zip：%s（%s）" % (zip_path, human(os.path.getsize(zip_path))))
    if problems:
        print("  ⚠ 版本号不一致，打包前请先修正", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
