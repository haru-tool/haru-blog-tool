import streamlit as st
import requests
import pandas as pd
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from openai import OpenAI
import itertools
import jwt
import os

# ============================================================
# 初期設定
# ============================================================
st.set_page_config(page_title="Haru Blog Tool", layout="wide")


# ============================================================
# Firebase Config 読み込み（static/firebase_config.json）
# ============================================================
def load_firebase_config():
    # Streamlit Cloud では、リポジトリのルートがカレントディレクトリになる想定
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "static", "firebase_config.json")

    if not os.path.exists(config_path):
        st.error("❌ firebase_config.json が見つかりません")
        return None

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


firebase_config = load_firebase_config()


# ============================================================
# Firebase Auth（Googleログイン）
# ============================================================
def show_login_screen():
    st.markdown("### 🔐 Google ログイン")
    st.markdown("ログインして Haru Blog Tool を利用してください。")

    if firebase_config is None:
        st.error("Firebase 設定が読み込めていません。static/firebase_config.json を確認してください。")
        return

    st.info("下のボタンをクリックして Google ログインページ（別タブ）が開きます。")

    # 🔥 「static/auth.html」（先頭の / を付けない）の相対パスでリンク
    st.link_button("Google でログイン", "static/auth.html")


# ============================================================
# JWT 検証（Firebase ID Token）
# ============================================================
def verify_firebase_token(id_token: str | None):
    if not id_token:
        return None
    try:
        decoded = jwt.decode(
            id_token,
            options={"verify_signature": False},
            algorithms=["RS256"],
        )
        return decoded
    except Exception:
        return None


# ============================================================
# WordPress 投稿の取得
# ============================================================
def fetch_wp_posts(wp_url, wp_user, wp_pass):
    try:
        all_posts = []
        page = 1
        per_page = 100

        while True:
            api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/posts"
            params = {
                "per_page": per_page,
                "page": page,
                "orderby": "modified",
                "order": "desc",
                "_fields": "id,title,slug,link,status,date,modified,categories,tags,content",
            }

            r = requests.get(api_url, params=params, auth=(wp_user, wp_pass))

            if r.status_code == 400:
                break
            if r.status_code != 200:
                st.error(f"❌ WP取得エラー: {r.status_code} {r.text}")
                return None

            posts = r.json()
            if not posts:
                break

            all_posts.extend(posts)

            total_pages = r.headers.get("X-WP-TotalPages")
            if total_pages is None or page >= int(total_pages):
                break

            page += 1

        def strip_html(html):
            import re
            return re.sub(r"<[^>]+>", "", html or "").strip()

        rows = []
        for p in all_posts:
            content_html = p.get("content", {}).get("rendered", "")
            content_text = strip_html(content_html)
            char_count = len(content_text)

            rows.append({
                "記事ID": p.get("id"),
                "タイトル": p.get("title", {}).get("rendered", ""),
                "スラッグ": p.get("slug", ""),
                "URL": p.get("link", ""),
                "ステータス": p.get("status", ""),
                "公開日": p.get("date", ""),
                "最終更新日": p.get("modified", ""),
                "文字数": char_count
            })

        return pd.DataFrame(rows)

    except Exception as e:
        st.error(f"例外発生: {e}")
        return None


# ============================================================
# Google Sheets 書き込み
# ============================================================
def write_to_sheets(df, sheet_id, worksheet_name):
    try:
        creds_dict = st.secrets["google_service_account"]
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(credentials)
        sh = gc.open_by_key(sheet_id)

        try:
            ws = sh.worksheet(worksheet_name)
        except:
            ws = sh.add_worksheet(title=worksheet_name, rows="2000", cols="30")

        ws.clear()
        ws.update("A1", [df.columns.tolist()] + df.astype(str).values.tolist())
        return True

    except Exception as e:
        st.error(f"Sheets 書き込みエラー: {e}")
        return False


# ============================================================
# SNS CSV生成
# ============================================================
def generate_sns_schedule(df, days, tone, api_key):
    time_slots = ["09:00", "12:00", "20:00"]
    today = datetime.today().date()
    records = []

    client = OpenAI(api_key=api_key) if api_key else None
    post_iter = itertools.cycle(df.itertuples(index=False))

    for d in range(days):
        date = today + timedelta(days=d)
        for t in time_slots:
            p = next(post_iter)

            if client:
                tone_text = "丁寧で落ち着いたトーン" if tone == "丁寧" else "カジュアルで親しみやすいトーン"
                prompt = f"""
記事タイトル: {p.タイトル}
URL: {p.URL}
トーン: {tone_text}
自然な紹介文＋3つのハッシュタグ
"""
                try:
                    res = client.chat.completions.create(
                        model="gpt-4.1-mini",
                        messages=[
                            {"role": "system", "content": "あなたは優秀なSNSライターです"},
                            {"role": "user", "content": prompt},
                        ],
                    )
                    text = res.choices[0].message.content.strip()
                except Exception:
                    text = f"[AI生成エラー] {p.タイトル}"
            else:
                text = f"{p.タイトル}\n{p.URL}"

            records.append({
                "datetime": f"{date} {t}",
                "title": p.タイトル,
                "url": p.URL,
                "text": text,
            })

    return pd.DataFrame(records)


# ============================================================
# メインアプリ
# ============================================================
def show_main_app(user):
    st.sidebar.success(f"ログイン中: {user.get('email', 'ユーザー')}")

    st.title("Haru Blog Tool")

    tab1, tab2, tab3 = st.tabs(["① WP取得", "② Sheets出力", "③ SNS CSV"])

    with tab1:
        st.subheader("WordPress 投稿取得")

        wp_url = st.text_input("WordPress URL")
        wp_user = st.text_input("WPユーザー名")
        wp_pass = st.text_input("WPアプリケーションパスワード", type="password")

        if st.button("投稿を取得する"):
            df = fetch_wp_posts(wp_url, wp_user, wp_pass)
            if df is not None:
                st.session_state.posts = df
                st.success("取得成功！")
                st.dataframe(df)

    with tab2:
        st.subheader("Google Sheets 出力")

        if "posts" not in st.session_state:
            st.info("❗ まず投稿を取得してください")
        else:
            sheet_id = st.text_input("スプレッドシートID")
            worksheet = st.text_input("ワークシート名", "WP_Posts")

            if st.button("Sheetsに書き込む"):
                ok = write_to_sheets(st.session_state.posts, sheet_id, worksheet)
                if ok:
                    st.success("Sheets 書き込み成功！")

    with tab3:
        st.subheader("SNS CSV生成")

        if "posts" not in st.session_state:
            st.info("❗ まず投稿を取得してください")
        else:
            days = st.number_input("生成日数", min_value=1, max_value=365, value=30)
            tone = st.radio("トーン", ["丁寧", "カジュアル"])
            api_key = st.text_input("OpenAI API Key（任意）", type="password")

            if st.button("CSV生成"):
                df_csv = generate_sns_schedule(st.session_state.posts, days, tone, api_key)
                st.download_button(
                    "CSVをダウンロード",
                    df_csv.to_csv(index=False).encode("utf-8-sig"),
                    "sns.csv"
                )


# ============================================================
# 認証フロー判定
# ============================================================
# st.query_params は Dict ライクなオブジェクト。to_dict() 経由で文字列にしておく
params = st.query_params.to_dict()
token = params.get("token")  # 文字列 or None

if not token:
    show_login_screen()
else:
    user = verify_firebase_token(token)
    if not user:
        show_login_screen()
    else:
        show_main_app(user)


