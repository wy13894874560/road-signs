#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量下载 Wikimedia Commons 上各国官方/公共领域道路标志 SVG（纯标准库，需要外网）。

用法（脚本在 tools/，输出目录指向交付包）：
    OUT=../.trae-html-share-packages/global-road-markings/official-road-signs

    python3 fetch_signs.py --out $OUT                  # 全量下载选中地区
    python3 fetch_signs.py --out $OUT --discover       # 只探测覆盖情况，写 coverage.json
    python3 fetch_signs.py --out $OUT --countries CN,US,DE,JP --limit 20
    python3 fetch_signs.py --out $OUT --region 欧洲
    python3 fetch_signs.py --out $OUT --delay 2        # 被 429 限流时加大间隔
    python3 fetch_signs.py --out $OUT --proxy http://127.0.0.1:7890

输出：
    <out>/assets/<国家代码>/<原始文件名>.svg
    <out>/manifest.json      （增量合并，可重复运行续传）
    <out>/coverage.json      （每个国家/地区解析到的分类与是否存在）
    <out>/fetch_log.txt

要点：
- 分类名不硬编码，启动时从 Commons 的 allcategories 实际清单解析，避免猜错。
- 单文件下载默认间隔 1 秒；遇到 429 会读 Retry-After 退避重试。
- 已下载文件跳过，manifest 增量合并，可随时中断重跑。
- 只抓 .svg；版权/许可以 Commons 文件页为准，CC-BY/CC-BY-SA 必须保留署名。
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://commons.wikimedia.org/w/api.php"
UA = "RoadSignLibrary/1.0 (road sign reference compilation; https://example.org/road-sign-library)"

OPENER = urllib.request.build_opener()


def set_proxy(proxy):
    """配置 HTTP/HTTPS 代理，如 http://127.0.0.1:7890。"""
    global OPENER
    if proxy:
        OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def _has_global_ipv6():
    try:
        import socket
        return socket.has_ipv6 and any(
            info[4][0].startswith("2") or info[4][0].startswith("3")
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6))
    except Exception:
        return False


def prefer_ipv4():
    """本机无全局 IPv6 时，避免 DNS 返回 AAAA 先连导致 Connection refused。"""
    if _has_global_ipv6():
        return
    import socket
    orig = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        infos = orig(host, port, family, type, proto, flags)
        v4 = [i for i in infos if i[0] == socket.AF_INET]
        return v4 or infos

    socket.getaddrinfo = ipv4_only

# ---------------------------------------------------------------------------
# 国家/地区表：代码 -> (中文名, 英文名, 地区, 额外的英文分类后缀)
# 分类由英文名 + 「SVG road signs in/of {名称}」在真实分类清单里匹配。
# 地区名用于页面三级导航：地区 -> 国家 -> 标识类型。
# ---------------------------------------------------------------------------
COUNTRIES = {
    # 东亚
    "CN": ("中国", "the PRC", "东亚", ["China", "the People's Republic of China"]),
    "JP": ("日本", "Japan", "东亚", []),
    "KR": ("韩国", "South Korea", "东亚", ["the Republic of Korea"]),
    "KP": ("朝鲜", "North Korea", "东亚", ["the Democratic People's Republic of Korea"]),
    "MN": ("蒙古", "Mongolia", "东亚", []),
    "HK": ("中国香港", "Hong Kong", "东亚", []),
    "MO": ("中国澳门", "Macau", "东亚", ["Macao"]),
    "TW": ("中国台湾", "Taiwan", "东亚", []),

    # 东南亚
    "SG": ("新加坡", "Singapore", "东南亚", []),
    "MY": ("马来西亚", "Malaysia", "东南亚", []),
    "TH": ("泰国", "Thailand", "东南亚", []),
    "VN": ("越南", "Vietnam", "东南亚", []),
    "ID": ("印度尼西亚", "Indonesia", "东南亚", []),
    "PH": ("菲律宾", "the Philippines", "东南亚", []),
    "KH": ("柬埔寨", "Cambodia", "东南亚", []),
    "LA": ("老挝", "Laos", "东南亚", []),
    "MM": ("缅甸", "Myanmar", "东南亚", ["Burma"]),
    "BN": ("文莱", "Brunei", "东南亚", []),

    # 南亚
    "IN": ("印度", "India", "南亚", []),
    "PK": ("巴基斯坦", "Pakistan", "南亚", []),
    "BD": ("孟加拉国", "Bangladesh", "南亚", []),
    "LK": ("斯里兰卡", "Sri Lanka", "南亚", []),
    "NP": ("尼泊尔", "Nepal", "南亚", []),
    "BT": ("不丹", "Bhutan", "南亚", []),
    "MV": ("马尔代夫", "the Maldives", "南亚", []),

    # 中亚与高加索
    "KZ": ("哈萨克斯坦", "Kazakhstan", "中亚与高加索", []),
    "UZ": ("乌兹别克斯坦", "Uzbekistan", "中亚与高加索", []),
    "KG": ("吉尔吉斯斯坦", "Kyrgyzstan", "中亚与高加索", []),
    "TJ": ("塔吉克斯坦", "Tajikistan", "中亚与高加索", []),
    "TM": ("土库曼斯坦", "Turkmenistan", "中亚与高加索", []),
    "AZ": ("阿塞拜疆", "Azerbaijan", "中亚与高加索", []),
    "GE": ("格鲁吉亚", "Georgia (country)", "中亚与高加索", []),
    "AM": ("亚美尼亚", "Armenia", "中亚与高加索", []),

    # 西亚
    "TR": ("土耳其", "Turkey", "西亚", ["Türkiye"]),
    "IL": ("以色列", "Israel", "西亚", []),
    "SA": ("沙特阿拉伯", "Saudi Arabia", "西亚", []),
    "AE": ("阿联酋", "the United Arab Emirates", "西亚", []),
    "QA": ("卡塔尔", "Qatar", "西亚", []),
    "KW": ("科威特", "Kuwait", "西亚", []),
    "BH": ("巴林", "Bahrain", "西亚", []),
    "OM": ("阿曼", "Oman", "西亚", []),
    "JO": ("约旦", "Jordan", "西亚", []),
    "LB": ("黎巴嫩", "Lebanon", "西亚", []),
    "IR": ("伊朗", "Iran", "西亚", []),
    "IQ": ("伊拉克", "Iraq", "西亚", []),
    "PS": ("巴勒斯坦", "Palestine", "西亚", []),

    # 欧洲
    "GB": ("英国", "the United Kingdom", "欧洲", ["the UK"]),
    "IE": ("爱尔兰", "Ireland", "欧洲", []),
    "FR": ("法国", "France", "欧洲", []),
    "DE": ("德国", "Germany", "欧洲", []),
    "IT": ("意大利", "Italy", "欧洲", []),
    "ES": ("西班牙", "Spain", "欧洲", []),
    "PT": ("葡萄牙", "Portugal", "欧洲", []),
    "NL": ("荷兰", "the Netherlands", "欧洲", []),
    "BE": ("比利时", "Belgium", "欧洲", []),
    "LU": ("卢森堡", "Luxembourg", "欧洲", []),
    "CH": ("瑞士", "Switzerland", "欧洲", []),
    "AT": ("奥地利", "Austria", "欧洲", []),
    "DK": ("丹麦", "Denmark", "欧洲", []),
    "SE": ("瑞典", "Sweden", "欧洲", []),
    "NO": ("挪威", "Norway", "欧洲", []),
    "FI": ("芬兰", "Finland", "欧洲", []),
    "IS": ("冰岛", "Iceland", "欧洲", []),
    "PL": ("波兰", "Poland", "欧洲", []),
    "CZ": ("捷克", "the Czech Republic", "欧洲", ["Czechia"]),
    "SK": ("斯洛伐克", "Slovakia", "欧洲", []),
    "HU": ("匈牙利", "Hungary", "欧洲", []),
    "RO": ("罗马尼亚", "Romania", "欧洲", []),
    "BG": ("保加利亚", "Bulgaria", "欧洲", []),
    "GR": ("希腊", "Greece", "欧洲", []),
    "HR": ("克罗地亚", "Croatia", "欧洲", []),
    "SI": ("斯洛文尼亚", "Slovenia", "欧洲", []),
    "RS": ("塞尔维亚", "Serbia", "欧洲", []),
    "BA": ("波黑", "Bosnia and Herzegovina", "欧洲", []),
    "ME": ("黑山", "Montenegro", "欧洲", []),
    "MK": ("北马其顿", "North Macedonia", "欧洲", []),
    "AL": ("阿尔巴尼亚", "Albania", "欧洲", []),
    "EE": ("爱沙尼亚", "Estonia", "欧洲", []),
    "LV": ("拉脱维亚", "Latvia", "欧洲", []),
    "LT": ("立陶宛", "Lithuania", "欧洲", []),
    "UA": ("乌克兰", "Ukraine", "欧洲", []),
    "BY": ("白俄罗斯", "Belarus", "欧洲", []),
    "MD": ("摩尔多瓦", "Moldova", "欧洲", []),
    "RU": ("俄罗斯", "Russia", "欧洲", []),
    "MT": ("马耳他", "Malta", "欧洲", []),
    "CY": ("塞浦路斯", "Cyprus", "欧洲", []),
    "AD": ("安道尔", "Andorra", "欧洲", []),
    "LI": ("列支敦士登", "Liechtenstein", "欧洲", []),
    "GI": ("直布罗陀", "Gibraltar", "欧洲", []),
    "GG": ("根西", "Guernsey", "欧洲", []),
    "JE": ("泽西", "Jersey", "欧洲", []),
    "FO": ("法罗群岛", "the Faroe Islands", "欧洲", []),
    "AX": ("奥兰群岛", "Åland", "欧洲", []),

    # 非洲
    "ZA": ("南非", "South Africa", "非洲", []),
    "EG": ("埃及", "Egypt", "非洲", []),
    "MA": ("摩洛哥", "Morocco", "非洲", []),
    "DZ": ("阿尔及利亚", "Algeria", "非洲", []),
    "TN": ("突尼斯", "Tunisia", "非洲", []),
    "LY": ("利比亚", "Libya", "非洲", []),
    "NG": ("尼日利亚", "Nigeria", "非洲", []),
    "KE": ("肯尼亚", "Kenya", "非洲", []),
    "GH": ("加纳", "Ghana", "非洲", []),
    "ET": ("埃塞俄比亚", "Ethiopia", "非洲", []),
    "TZ": ("坦桑尼亚", "Tanzania", "非洲", []),
    "UG": ("乌干达", "Uganda", "非洲", []),
    "SN": ("塞内加尔", "Senegal", "非洲", []),
    "CI": ("科特迪瓦", "Ivory Coast", "非洲", ["Côte d'Ivoire"]),
    "CM": ("喀麦隆", "Cameroon", "非洲", []),
    "AO": ("安哥拉", "Angola", "非洲", []),
    "MZ": ("莫桑比克", "Mozambique", "非洲", []),
    "ZW": ("津巴布韦", "Zimbabwe", "非洲", []),
    "BW": ("博茨瓦纳", "Botswana", "非洲", []),
    "NA": ("纳米比亚", "Namibia", "非洲", []),
    "MU": ("毛里求斯", "Mauritius", "非洲", []),
    "SC": ("塞舌尔", "Seychelles", "非洲", []),
    "LR": ("利比里亚", "Liberia", "非洲", []),
    "MW": ("马拉维", "Malawi", "非洲", []),
    "ML": ("马里", "Mali", "非洲", []),
    "MG": ("马达加斯加", "Madagascar", "非洲", []),
    "LS": ("莱索托", "Lesotho", "非洲", []),
    "SZ": ("斯威士兰", "Eswatini", "非洲", ["Swaziland"]),
    "KM": ("科摩罗", "the Comoros", "非洲", []),
    "SO": ("索马里", "Somalia", "非洲", []),
    "CD": ("刚果（金）", "the Democratic Republic of the Congo", "非洲", []),
    "ZM": ("赞比亚", "Zambia", "非洲", []),

    # 北美
    "US": ("美国", "the United States", "北美", ["the USA", "the United States of America"]),
    "CA": ("加拿大", "Canada", "北美", []),
    "MX": ("墨西哥", "Mexico", "北美", []),

    # 中美与加勒比
    "GT": ("危地马拉", "Guatemala", "中美与加勒比", []),
    "CR": ("哥斯达黎加", "Costa Rica", "中美与加勒比", []),
    "PA": ("巴拿马", "Panama", "中美与加勒比", []),
    "CU": ("古巴", "Cuba", "中美与加勒比", []),
    "DO": ("多米尼加", "the Dominican Republic", "中美与加勒比", []),
    "JM": ("牙买加", "Jamaica", "中美与加勒比", []),
    "TT": ("特立尼达和多巴哥", "Trinidad and Tobago", "中美与加勒比", []),
    "BS": ("巴哈马", "the Bahamas", "中美与加勒比", []),
    "BB": ("巴巴多斯", "Barbados", "中美与加勒比", []),
    "PR": ("波多黎各", "Puerto Rico", "中美与加勒比", []),
    "SV": ("萨尔瓦多", "El Salvador", "中美与加勒比", []),
    "HN": ("洪都拉斯", "Honduras", "中美与加勒比", []),
    "NI": ("尼加拉瓜", "Nicaragua", "中美与加勒比", []),

    # 南美
    "BR": ("巴西", "Brazil", "南美", []),
    "AR": ("阿根廷", "Argentina", "南美", []),
    "CL": ("智利", "Chile", "南美", []),
    "CO": ("哥伦比亚", "Colombia", "南美", []),
    "PE": ("秘鲁", "Peru", "南美", []),
    "VE": ("委内瑞拉", "Venezuela", "南美", []),
    "EC": ("厄瓜多尔", "Ecuador", "南美", []),
    "BO": ("玻利维亚", "Bolivia", "南美", []),
    "PY": ("巴拉圭", "Paraguay", "南美", []),
    "UY": ("乌拉圭", "Uruguay", "南美", []),
    "GY": ("圭亚那", "Guyana", "南美", []),
    "SR": ("苏里南", "Suriname", "南美", []),

    # 大洋洲
    "AU": ("澳大利亚", "Australia", "大洋洲", []),
    "NZ": ("新西兰", "New Zealand", "大洋洲", []),
    "FJ": ("斐济", "Fiji", "大洋洲", []),
    "PG": ("巴布亚新几内亚", "Papua New Guinea", "大洋洲", []),
    "NC": ("新喀里多尼亚", "New Caledonia", "大洋洲", []),
    "WS": ("萨摩亚", "Samoa", "大洋洲", []),
    "TO": ("汤加", "Tonga", "大洋洲", []),
    "VU": ("瓦努阿图", "Vanuatu", "大洋洲", []),
}

REGION_ORDER = ["东亚", "东南亚", "南亚", "中亚与高加索", "西亚", "欧洲",
                "非洲", "北美", "中美与加勒比", "南美", "大洋洲"]

CAT_PREFIXES = ["SVG road signs in ", "SVG road signs of "]


def log(msg, fp=None):
    print(msg)
    if fp:
        fp.write(msg + "\n")
        fp.flush()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def api(params, retries=6):
    params = dict(params)
    params["format"] = "json"
    params["formatversion"] = "2"
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with OPENER.open(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "").isdigit() else 15 * (attempt + 1)
                time.sleep(min(wait, 120))
            else:
                time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def safe_name(title):
    name = title.replace("File:", "").replace("/", "_")
    name = re.sub(r'[\\:*?"<>|]', "_", name)
    return name[:180]


def build_category_index(logfp):
    """从 Commons 真实分类清单建立索引，替代硬编码猜测。"""
    cats = set()
    for prefix in CAT_PREFIXES:
        cont = {}
        while True:
            params = {"action": "query", "list": "allcategories",
                      "acprefix": prefix, "aclimit": "500"}
            params.update(cont)
            data = api(params)
            for c in data.get("query", {}).get("allcategories", []):
                cats.add(c.get("category", ""))
            if "continue" in data:
                cont = data["continue"]
                time.sleep(0.5)
            else:
                break
    log(f"分类索引: {len(cats)} 个 SVG road signs 分类", logfp)
    return cats


def resolve_category(code, index):
    cn, en, region, extra = COUNTRIES[code]
    names = [en] + list(extra)
    for prefix in CAT_PREFIXES:
        for name in names:
            if prefix + name in index:
                return "Category:" + prefix + name
    return None


class Downloader:
    def __init__(self, out, limit, depth, delay, logfp, manifest=None):
        self.out = out
        self.limit = limit
        self.depth = depth
        self.delay = delay
        self.logfp = logfp
        self.manifest = manifest or []
        self.seen = {(m.get("country"), m.get("title")) for m in self.manifest}
        self.visited = set()
        self.count = 0
        self.fail = 0
        self.bytes = 0
        self.blocked = 0

    def download(self, url, path):
        if os.path.exists(path) and os.path.getsize(path) > 100:
            self.bytes += os.path.getsize(path)
            return True
        tmp = path + ".part"
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with OPENER.open(req, timeout=90) as r:
                    data = r.read()
                if len(data) < 50:
                    raise ValueError("empty payload")
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                self.bytes += len(data)
                if self.delay:
                    time.sleep(self.delay)
                self.blocked = 0
                return True
            except urllib.error.HTTPError as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                if e.code == 429:
                    self.blocked += 1
                    ra = e.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.replace(".", "").isdigit() else 20 * (attempt + 1)
                    log(f"    ~ 429 限流，等待 {min(wait, 180):.0f}s", self.logfp)
                    time.sleep(min(wait, 180))
                else:
                    log(f"    ! HTTP {e.code}: {os.path.basename(path)}", self.logfp)
                    time.sleep(3 * (attempt + 1))
            except Exception as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                log(f"    ! download failed: {os.path.basename(path)} ({e})", self.logfp)
                time.sleep(3 * (attempt + 1))
        self.fail += 1
        return False

    def record(self, clean, code, cn, region, dest, info, already=False):
        meta = info.get("extmetadata") or {}
        self.manifest.append({
            "country": code,
            "country_cn": cn,
            "region": region,
            "file": os.path.relpath(dest, self.out).replace(os.sep, "/"),
            "title": clean,
            "description": strip_html((meta.get("ImageDescription") or {}).get("value", "")),
            "license": (meta.get("LicenseShortName") or {}).get("value", ""),
            "artist": strip_html((meta.get("Artist") or {}).get("value", "")),
            "credit": strip_html((meta.get("Credit") or {}).get("value", "")),
            "source": info.get("descriptionurl", ""),
            "original_url": info.get("url", ""),
            "recovered": already,
        })

    def walk(self, cat, code, cn, region, depth=None):
        depth = self.depth if depth is None else depth
        if cat in self.visited:
            return
        self.visited.add(cat)
        cont = {}
        got = 0
        while True:
            if self.limit and got >= self.limit:
                return
            params = {
                "action": "query",
                "generator": "categorymembers",
                "gcmtitle": cat,
                "gcmtype": "file|subcat",
                "gcmlimit": "500",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
            }
            params.update(cont)
            data = api(params)
            pages = data.get("query", {}).get("pages", [])
            for p in pages:
                if self.limit and got >= self.limit:
                    return
                title = p.get("title", "")
                if title.startswith("Category:"):
                    if depth > 0:
                        self.walk(title, code, cn, region, depth - 1)
                    continue
                if not title.lower().endswith(".svg"):
                    continue
                clean = title.replace("File:", "")
                if (code, clean) in self.seen:
                    continue
                info = (p.get("imageinfo") or [{}])[0]
                url = info.get("url")
                if not url:
                    continue
                fname = safe_name(title)
                dest_dir = os.path.join(self.out, "assets", code)
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, fname)
                if os.path.exists(dest) and os.path.getsize(dest) > 100:
                    # 上次中断时已落盘，但 manifest 未写入，直接补记录
                    self.bytes += os.path.getsize(dest)
                    got += 1
                    self.seen.add((code, clean))
                    self.record(clean, code, cn, region, dest, info, already=True)
                    continue
                if self.download(url, dest):
                    self.count += 1
                    got += 1
                    self.seen.add((code, clean))
                    self.record(clean, code, cn, region, dest, info)
                if self.count % 25 == 0 and self.count:
                    log(f"    ... {self.count} files", self.logfp)
            if "continue" in data:
                cont = data["continue"]
                time.sleep(0.5)
            else:
                break

    def run_country(self, code, category):
        cn = COUNTRIES[code][0]
        region = COUNTRIES[code][2]
        before = self.count
        if not category:
            log("  - 无可用分类", self.logfp)
            return 0
        log(f"  category: {category}", self.logfp)
        try:
            self.walk(category, code, cn, region)
        except Exception as e:
            log(f"  ! category failed: {e}", self.logfp)
        return self.count - before


def probe(logfp):
    """先探一次 API，链路被墙时立刻给出明确提示，避免逐个文件空转。"""
    s = time.time()
    try:
        api({"action": "query", "meta": "siteinfo"}, retries=1)
        log(f"链路探测: Commons API 可用（{time.time() - s:.1f}s）", logfp)
        return True
    except Exception as e:
        log(f"链路探测失败（{time.time() - s:.1f}s）: {e}", logfp)
        log("Commons 当前不可达。若 TCP 能连通但 TLS 握手超时，通常是 SNI 阻断，换域名无效，需要代理或其他网络。", logfp)
        return False


def selected_codes(args):
    if args.countries:
        codes = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
        return [c for c in codes if c in COUNTRIES]
    codes = list(COUNTRIES)
    if args.region:
        regions = [r.strip() for r in args.region.split(",") if r.strip()]
        codes = [c for c in codes if COUNTRIES[c][2] in regions]
    return sorted(codes, key=lambda c: REGION_ORDER.index(COUNTRIES[c][2]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./official-road-signs")
    ap.add_argument("--countries", default="", help="逗号分隔，如 CN,US,DE；默认全部")
    ap.add_argument("--region", default="", help="按地区筛选，如 欧洲 或 欧洲,北美")
    ap.add_argument("--limit", type=int, default=0, help="每国最多文件数；0=不限（默认）")
    ap.add_argument("--depth", type=int, default=3, help="分类递归深度，默认 3")
    ap.add_argument("--delay", type=float, default=1.0, help="每个文件下载后的间隔秒数，默认 1.0")
    ap.add_argument("--discover", action="store_true", help="只探测分类覆盖，不下载")
    ap.add_argument("--proxy", default="", help="HTTP 代理，如 http://127.0.0.1:7890")
    ap.add_argument("--wait", type=int, default=0, help="链路不通时每隔 N 秒重试，直到连通；0=不等待")
    args = ap.parse_args()

    set_proxy(args.proxy)
    prefer_ipv4()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)
    logfp = open(os.path.join(out, "fetch_log.txt"), "a", encoding="utf-8")

    codes = selected_codes(args)
    if not codes:
        log("没有匹配的国家/地区代码", logfp)
        logfp.close()
        return

    while not probe(logfp):
        if not args.wait:
            logfp.close()
            return
        log(f"{args.wait}s 后重试…", logfp)
        time.sleep(args.wait)

    index = build_category_index(logfp)
    resolved = {code: resolve_category(code, index) for code in codes}

    if args.discover:
        rows = [{"code": c, "cn": COUNTRIES[c][0], "region": COUNTRIES[c][2],
                 "category": resolved[c], "exists": bool(resolved[c])} for c in codes]
        with open(os.path.join(out, "coverage.json"), "w", encoding="utf-8") as f:
            json.dump({"countries": rows}, f, ensure_ascii=False, indent=2)
        ok = [r for r in rows if r["exists"]]
        log(f"\n可用: {len(ok)}/{len(rows)}", logfp)
        for region in REGION_ORDER:
            group = [r for r in ok if r["region"] == region]
            if group:
                log(f"  {region} {len(group)}: " + "、".join(r["cn"] for r in group), logfp)
        missing = [r["cn"] for r in rows if not r["exists"]]
        if missing:
            log(f"\n暂无 SVG 分类（{len(missing)}）: " + "、".join(missing), logfp)
        logfp.close()
        return

    manifest_path = os.path.join(out, "manifest.json")
    manifest = load_json(manifest_path, [])
    if not isinstance(manifest, list):
        manifest = []
    log(f"已有 manifest: {len(manifest)} 条，继续增量下载", logfp)

    dl = Downloader(out, args.limit, args.depth, args.delay, logfp, manifest)
    log(f"目标: {len(codes)} 个国家/地区，深度 {args.depth}，每国上限 {args.limit or '不限'}，间隔 {args.delay}s", logfp)

    for i, code in enumerate(codes, 1):
        cn = COUNTRIES[code][0]
        log(f"\n[{i}/{len(codes)}] == {code} {cn}", logfp)
        n = dl.run_country(code, resolved[code])
        log(f"  +{n}", logfp)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(dl.manifest, f, ensure_ascii=False, indent=2)

    log(f"\nDONE: 新增 {dl.count} 个 SVG，失败 {dl.fail}，manifest 共 {len(dl.manifest)} 条", logfp)
    log(f"体积: {dl.bytes / 1024 / 1024:.1f} MB", logfp)
    log(f"output: {out}", logfp)
    logfp.close()


if __name__ == "__main__":
    main()
