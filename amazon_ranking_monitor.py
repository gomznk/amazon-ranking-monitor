#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amazon_ranking_monitor.py
Amazon売れ筋ランキングを監視し、指定商品がTop10入りしたらAsanaタスクを作成する
"""

import csv
import json
import os
import re
import sys
import time
import random
from datetime import datetime

import requests
from bs4 import BeautifulSoup


# ── 設定 ──────────────────────────────────────────────
def _get_asana_token() -> str:
    return os.environ["ASANA_TOKEN"]


def _get_assignee_gid(token: str, email: str) -> str:
    res = requests.get(
        f"https://app.asana.com/api/1.0/users/{email}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()["data"]["gid"]


ASANA_TOKEN   = _get_asana_token()
PROJECT_GID   = os.environ["PROJECT_GID"]
SECTION_GID   = os.environ["SECTION_GID"]
ASSIGNEE_EMAIL = os.environ["ASSIGNEE_EMAIL"]
RANK_THRESHOLD = 10

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CSV_FILE   = os.path.join(BASE_DIR, "ASIN カテゴリ.csv")
DATA_FILE  = os.path.join(BASE_DIR, "seen_rankings.json")

ASSIGNEE_GID = _get_assignee_gid(ASANA_TOKEN, ASSIGNEE_EMAIL)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── 状態管理 ──────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── CSV読み込み ───────────────────────────────────────
def load_products() -> list[dict]:
    products = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asin = row.get("子ASIN", "").strip()
            if asin:
                products.append({
                    "name":     row.get("商品名", "").strip(),
                    "asin":     asin,
                    "category": row.get("カテゴリ", "").strip(),
                })
    return products


# ── Amazon スクレイピング ─────────────────────────────
def fetch_ranking(asin: str, target_category: str) -> tuple[int | None, str | None, str | None]:
    """
    指定ASINのAmazon売れ筋ランキングと商品名を取得する。
    target_category が指定されている場合はそのカテゴリの順位を優先。
    戻り値: (rank, category_name, amazon_title) or (None, None, None)
    """
    url = f"https://www.amazon.co.jp/dp/{asin}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] {asin} ページ取得失敗: {e}", file=sys.stderr)
        return None, None, None

    # CAPTCHAページ検出
    if "robot check" in res.text.lower() or "captcha" in res.text.lower():
        print(f"[WARN] {asin} CAPTCHA検出 - スキップ", file=sys.stderr)
        return None, None, None

    soup = BeautifulSoup(res.text, "html.parser")

    # 商品名を取得
    title_el = soup.find(id="productTitle")
    amazon_title = title_el.get_text(strip=True) if title_el else None

    all_ranks = _parse_sales_ranks(soup)

    if not all_ranks:
        print(f"[INFO] {asin} ランキング情報なし")
        return None, None, amazon_title

    # カテゴリ指定あり: 部分一致で探す
    if target_category:
        for r in all_ranks:
            if target_category in r["category"] or r["category"] in target_category:
                return r["rank"], r["category"], amazon_title

    # カテゴリ未指定 or 一致なし: 最上位のランクを返す
    best = min(all_ranks, key=lambda x: x["rank"])
    return best["rank"], best["category"], amazon_title


def _parse_sales_ranks(soup: BeautifulSoup) -> list[dict]:
    """ページ内の全売れ筋ランキング情報を抽出する"""
    rank_text = ""

    # パターン1: 旧レイアウト #SalesRank
    el = soup.find(id="SalesRank")
    if el:
        rank_text = el.get_text(separator=" ")

    # パターン2: 詳細バレット (新レイアウト)
    if not rank_text:
        for bid in ("detailBullets_feature_div", "detailBulletsWrapper_feature_div"):
            bullets = soup.find(id=bid)
            if bullets:
                for bold in bullets.find_all("span", class_="a-text-bold"):
                    if "売れ筋ランキング" in bold.get_text():
                        container = bold.find_parent("li") or bold.find_parent("div")
                        if container:
                            rank_text = container.get_text(separator=" ")
                        break
                if rank_text:
                    break

    # パターン3: productDetails テーブル
    if not rank_text:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                th = row.find(["th", "td"])
                if th and "売れ筋ランキング" in th.get_text():
                    td = row.find("td")
                    if td:
                        rank_text = td.get_text(separator=" ")
                    break
            if rank_text:
                break

    if not rank_text:
        return []

    return _extract_ranks_from_text(rank_text)


def _extract_ranks_from_text(text: str) -> list[dict]:
    """テキストから (rank, category) ペアを抽出する"""
    ranks = []
    seen = set()

    # "( ...ランキングを見る )" のような括弧内ノイズを除去
    text = re.sub(r'\([^)]{0,100}(?:ランキングを見る|売れ筋)[^)]{0,100}\)', '', text)

    def add(rank: int, cat: str) -> None:
        cat = cat.strip().rstrip("・ 　")
        # 括弧残滓やノイズ文字列を除外
        if (cat and len(cat) <= 40
                and '(' not in cat and ')' not in cat
                and 'を見る' not in cat
                and (rank, cat) not in seen):
            ranks.append({"rank": rank, "category": cat})
            seen.add((rank, cat))

    # パターンA: "カテゴリ名 - X,XXX位" (Amazon JPの標準形式)
    for m in re.finditer(
        r'([^\d\n\r]{2,40}?)\s*-\s*([\d,]{1,10})位',
        text,
    ):
        cat = re.sub(r'Amazon\s*売れ筋ランキング[:\s：]*', '', m.group(1)).strip()
        rank = int(m.group(2).replace(",", ""))
        if cat:
            add(rank, cat)

    # パターンB: "X位 ― カテゴリ名" / "X位 - カテゴリ名" (旧レイアウト)
    for m in re.finditer(
        r'([\d,]{1,10})位\s*[―\-]\s*([^\d\n（(#]{2,40}?)(?:\s*[\(（]|\s*\d|\s*$|\n)',
        text,
    ):
        rank = int(m.group(1).replace(",", ""))
        add(rank, m.group(2))

    # パターンC: "#X in カテゴリ名" (英語レイアウトの場合)
    for m in re.finditer(r'#([\d,]{1,10})\s+(?:in\s+)([^\n\r#]{2,40})', text):
        rank = int(m.group(1).replace(",", ""))
        add(rank, m.group(2))

    # パターンD: "カテゴリ の中で X位"
    for m in re.finditer(r'([^\n\r\d]{2,30}?)\s*の中で\s*([\d,]{1,10})位', text):
        cat = re.sub(r'Amazon\s*売れ筋ランキング[:\s：]*', '', m.group(1)).strip()
        rank = int(m.group(2).replace(",", ""))
        if cat:
            add(rank, cat)

    return ranks


# ── Asana 通知 ───────────────────────────────────────
def post_to_asana(product: dict, rank: int, category: str, amazon_title: str | None) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    display_name = amazon_title or product["name"]
    name = f"【Amazon売れ筋 Top{RANK_THRESHOLD}入賞】{display_name} #{rank}位"
    notes = (
        f"商品名　　: {display_name}\n"
        f"ASIN　　　: {product['asin']}\n"
        f"カテゴリ　: {category}\n"
        f"順位　　　: {rank}位\n"
        f"確認日時　: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"商品URL　 : https://www.amazon.co.jp/dp/{product['asin']}"
    )

    headers = {
        "Authorization": f"Bearer {ASANA_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    res = requests.post(
        "https://app.asana.com/api/1.0/tasks",
        headers=headers,
        json={"data": {
            "name":     name,
            "notes":    notes,
            "projects": [PROJECT_GID],
            "assignee": ASSIGNEE_GID,
            "due_on":   today,
        }},
        timeout=15,
    )
    res.raise_for_status()
    task_gid = res.json()["data"]["gid"]

    requests.post(
        f"https://app.asana.com/api/1.0/sections/{SECTION_GID}/addTask",
        headers=headers,
        json={"data": {"task": task_gid}},
        timeout=15,
    ).raise_for_status()

    print(f"[OK] Asana通知: {product['name']} #{rank}位 ({category})")


# ── メイン処理 ───────────────────────────────────────
def main() -> None:
    products = load_products()
    state = load_state()
    posted = 0

    for i, product in enumerate(products):
        asin = product["asin"]

        # リクエスト間に待機（Amazonのレート制限対策）
        if i > 0:
            time.sleep(random.uniform(3.0, 6.0))

        print(f"[INFO] チェック中: {product['name']} ({asin})")

        rank, category, amazon_title = fetch_ranking(asin, product["category"])
        display_name = amazon_title or product["name"]

        prev = state.get(asin, {})
        was_in_top = prev.get("in_top10", False)
        now_in_top = rank is not None and rank <= RANK_THRESHOLD

        # 状態を更新
        state[asin] = {
            "in_top10":     now_in_top,
            "rank":         rank,
            "category":     category,
            "amazon_title": amazon_title,
            "checked_at":   datetime.now().isoformat(timespec="seconds"),
        }

        if now_in_top and not was_in_top:
            # Top10に新たに入った → 通知
            try:
                post_to_asana(product, rank, category, amazon_title)
                posted += 1
            except Exception as e:
                print(f"[ERROR] Asana投稿失敗 ({asin}): {e}", file=sys.stderr)
        elif now_in_top:
            print(f"[INFO] {display_name} #{rank}位 - 継続中（通知済み）")
        elif rank is not None:
            print(f"[INFO] {display_name} #{rank}位 - Top{RANK_THRESHOLD}外")
        else:
            print(f"[INFO] {display_name} - ランキング取得不可")

    save_state(state)

    if posted == 0:
        print(f"[INFO] 新規Top{RANK_THRESHOLD}入賞なし")
    else:
        print(f"[INFO] {posted}件のTop{RANK_THRESHOLD}入賞を通知しました")


if __name__ == "__main__":
    main()
