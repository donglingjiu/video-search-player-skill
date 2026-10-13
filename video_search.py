#!/usr/bin/env python3
"""
video_search.py — 视频资源搜索 & 节点测速脚本

功能：
  1. 通过百度搜索在各主流平台（B站/爱奇艺/优酷/腾讯视频/芒果TV）搜索视频
  2. 提取各平台播放页 URL
  3. 对 URL 做 HTTP HEAD 测速，检测可达性与延迟
  4. 输出 JSON 结果列表

用法：
  python3 video_search.py --query "庆余年 第二季" [--limit 10]
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Optional
from urllib.parse import quote_plus, urlparse

try:
    import httpx
except ImportError:
    print(json.dumps({"error": "缺少依赖：httpx。请执行 pip install httpx"}))
    sys.exit(1)

try:
    from fake_useragent import UserAgent
    UA = UserAgent()
    def get_ua(): return UA.random
except Exception:
    def get_ua(): return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

# 目标平台及其域名关键词
PLATFORMS = {
    "B站":      ["bilibili.com/video", "bilibili.com/bangumi"],
    "爱奇艺":   ["iqiyi.com/v_", "iqiyi.com/a_"],
    "优酷":     ["youku.com/v_show", "youku.com/video"],
    "腾讯视频": ["v.qq.com/x/cover", "v.qq.com/detail"],
    "芒果TV":   ["mgtv.com/b/", "mgtv.com/h/"],
}

QUALITY_KEYWORDS = ["4K", "超高清", "1080P", "超清", "高清", "720P", "标清", "480P"]
LOGIN_KEYWORDS   = ["需要登录", "登录后观看", "开通会员", "VIP专享"]


def detect_platform(url: str) -> str:
    for name, patterns in PLATFORMS.items():
        for p in patterns:
            if p in url:
                return name
    domain = urlparse(url).netloc
    return domain or "未知平台"


def detect_quality(title: str, snippet: str) -> str:
    text = (title + " " + snippet).upper()
    for q in QUALITY_KEYWORDS:
        if q.upper() in text:
            return q
    return "未知"


def detect_login_required(snippet: str) -> bool:
    for kw in LOGIN_KEYWORDS:
        if kw in snippet:
            return True
    return False


async def baidu_search(query: str, limit: int, client: httpx.AsyncClient):
    """通过百度搜索获取各平台视频链接"""
    results = []
    pages = max(1, (limit + 9) // 10)
    for pn in range(pages):
        url = f"https://www.baidu.com/s?wd={quote_plus(query + ' 在线观看')}&pn={pn * 10}"
        try:
            resp = await client.get(url, headers={
                "User-Agent": get_ua(),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, timeout=10)
            text = resp.text
            # 简单正则提取百度结果中的真实 URL
            import re
            raw_links = re.findall(r'href="(https?://[^"]+)"', text)
            for link in raw_links:
                for name, patterns in PLATFORMS.items():
                    if any(p in link for p in patterns):
                        # 提取 title 片段（title 在链接附近的文本，做简单截取）
                        pos = text.find(link)
                        snippet = text[max(0, pos-200):pos+200] if pos != -1 else ""
                        snippet_clean = re.sub(r'<[^>]+>', ' ', snippet)[:200]
                        results.append({
                            "platform": name,
                            "url": link,
                            "title": query,  # 搜索词作为初始 title
                            "quality": detect_quality(query, snippet_clean),
                            "login_required": detect_login_required(snippet_clean),
                            "snippet": snippet_clean.strip(),
                        })
                        break
        except Exception as e:
            pass
        await asyncio.sleep(0.5)
    # 去重（按 url）
    seen = set()
    deduped = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    return deduped[:limit]


async def test_node(url: str, client: httpx.AsyncClient) -> dict:
    """测试单个节点的可达性与延迟（毫秒）"""
    start = time.monotonic()
    try:
        resp = await client.head(url, headers={"User-Agent": get_ua()},
                                  timeout=8, follow_redirects=True)
        elapsed = int((time.monotonic() - start) * 1000)
        ok = resp.status_code < 400
        return {"reachable": ok, "latency_ms": elapsed, "status_code": resp.status_code}
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return {"reachable": False, "latency_ms": elapsed, "status_code": 0, "error": str(e)[:80]}


async def main(query: str, limit: int):
    async with httpx.AsyncClient(verify=False) as client:
        # 1. 搜索
        items = await baidu_search(query, limit, client)

        if not items:
            print(json.dumps({"query": query, "total": 0, "results": [],
                              "message": "未找到相关视频资源，建议直接前往各平台搜索"}, ensure_ascii=False, indent=2))
            return

        # 2. 并发测速（最多同时 8 个）
        sem = asyncio.Semaphore(8)
        async def probe(item):
            async with sem:
                node = await test_node(item["url"], client)
                item.update(node)
                return item

        results = await asyncio.gather(*[probe(item) for item in items])

        # 3. 排序：可达 > 延迟低 > 画质高
        quality_order = {q: i for i, q in enumerate(QUALITY_KEYWORDS)}
        results.sort(key=lambda x: (
            0 if x.get("reachable") else 1,
            x.get("latency_ms", 9999),
            quality_order.get(x.get("quality", ""), 99),
        ))

        output = {
            "query": query,
            "total": len(results),
            "results": results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="视频资源搜索与节点测速")
    parser.add_argument("--query", "-q", required=True, help="搜索关键词，如 '庆余年 第二季'")
    parser.add_argument("--limit", "-l", type=int, default=10, help="最多返回结果数（默认 10）")
    args = parser.parse_args()
    asyncio.run(main(args.query, args.limit))
