#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按文件名回查 Commons 元数据（描述、许可、作者、分类），用于补齐缺失记录。

联网环境（GitHub Actions）：
    python3 tools/gallery/enrich_categories.py fetch --titles titles.json --out meta.json

本地合并（把结果写回 manifest，并补建缺失条目）：
    python3 tools/gallery/enrich_categories.py merge --meta meta.json \
        --manifest <pkg>/official-road-signs/manifest.json \
        --assets <pkg>/official-road-signs/assets

抓取带检查点，中断后重跑会自动跳过已完成的标题。
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://commons.wikimedia.org/w/api.php"
UA = "RoadSignLibrary/1.0 (metadata backfill; road sign reference compilation)"


def api(params, retries=5):
    """用 POST 提交查询，避免长文件名把 URL 撑过限制。"""
    params = dict(params)
    params.update({"format": "json", "formatversion": "2"})
    body = urllib.parse.urlencode(params).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 15)
                print(f"  429，等待 {wait}s")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            print(f"  重试 {attempt + 1}/{retries}: {type(e).__name__} {e}")
            time.sleep(5 * (attempt + 1))
    return {}


def strip_html(value, limit=400):
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch(titles, out, delay):
    done = {}
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                done = json.load(f)
        except Exception:
            done = {}
    pending = [t for t in titles if t not in done]
    print(f"待抓取 {len(pending)} / 总计 {len(titles)}", flush=True)
    for i in range(0, len(pending), 50):
        batch = pending[i:i + 50]
        data = api({
            "action": "query",
            "titles": "|".join("File:" + t for t in batch),
            "prop": "categories|imageinfo",
            "iiprop": "url|extmetadata",
            "clshow": "!hidden",
            "cllimit": "500",
        })
        for page in data.get("query", {}).get("pages", []):
            title = (page.get("title") or "").replace("File:", "", 1)
            if not title:
                continue
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            done[title] = {
                "c": [c.get("title", "")[9:] for c in page.get("categories") or []],
                "d": strip_html((meta.get("ImageDescription") or {}).get("value")),
                "l": strip_html((meta.get("LicenseShortName") or {}).get("value"), 80),
                "a": strip_html((meta.get("Artist") or {}).get("value"), 200),
                "cr": strip_html((meta.get("Credit") or {}).get("value"), 200),
                "s": info.get("descriptionurl", ""),
                "u": info.get("url", ""),
                "m": bool(page.get("missing")),
            }
        for t in batch:
            done.setdefault(t, {"c": [], "missing": True})
        if i % 500 == 0:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False)
            print(f"  进度 {min(i + 50, len(pending))}/{len(pending)}", flush=True)
        time.sleep(delay)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False)
    print(f"完成，共 {len(done)} 条", flush=True)


def merge(meta_path, manifest_path, assets_dir):
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    with open(manifest_path, encoding="utf-8") as f:
        rows = json.load(f)

    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from fetch_signs import COUNTRIES

    index = {(r.get("country"), r.get("title")): r for r in rows}
    added = filled = cats = 0

    # 1) 已有条目补分类与缺失字段
    for (code, title), row in index.items():
        info = meta.get(title)
        if not info or info.get("m"):
            continue
        if info.get("c"):
            row["categories"] = info["c"]
            cats += 1
        for key, src in (("description", "d"), ("license", "l"),
                         ("artist", "a"), ("credit", "cr"),
                         ("source", "s"), ("original_url", "u")):
            if not row.get(key) and info.get(src):
                row[key] = info[src]
                filled += 1

    # 2) 磁盘上存在但 manifest 缺失的文件，补建条目
    if assets_dir and os.path.isdir(assets_dir):
        for code in sorted(os.listdir(assets_dir)):
            folder = os.path.join(assets_dir, code)
            if not os.path.isdir(folder) or code not in COUNTRIES:
                continue
            cn, _en, region, _extra = COUNTRIES[code]
            for name in sorted(os.listdir(folder)):
                if not name.lower().endswith(".svg"):
                    continue
                if (code, name) in index:
                    continue
                info = meta.get(name) or {}
                if info.get("m"):
                    continue
                rows.append({
                    "country": code,
                    "country_cn": cn,
                    "region": region,
                    "file": "assets/%s/%s" % (code, name),
                    "title": name,
                    "categories": info.get("c") or [],
                    "description": info.get("d", ""),
                    "license": info.get("l", ""),
                    "artist": info.get("a", ""),
                    "credit": info.get("cr", ""),
                    "source": info.get("s", ""),
                    "original_url": info.get("u", ""),
                })
                added += 1

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"manifest {len(rows)} 条：补建 {added}，补分类 {cats}，补字段 {filled}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--titles", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--delay", type=float, default=0.4)

    m = sub.add_parser("merge")
    m.add_argument("--meta", required=True)
    m.add_argument("--manifest", required=True)
    m.add_argument("--assets", default="")

    args = ap.parse_args()
    if args.cmd == "fetch":
        with open(args.titles, encoding="utf-8") as fp:
            fetch(json.load(fp), args.out, args.delay)
    else:
        merge(args.meta, args.manifest, args.assets)


if __name__ == "__main__":
    main()
