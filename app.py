"""
Fase 4 — Aplikasi Web Dasbor & Database KJA Waduk Jatiluhur
Estetika: Modern Clean Data Dashboard (Gaya ITC Trade Centre)
Fitur:
- Inisialisasi GEE & Google Sheets otomatis dari st.secrets["gcp_service_account"]
- Dukungan ?embed=true untuk portal Google Apps Script (100vh tanpa scroll ganda)
- Mode Publik (Read-Only) default & Mode Admin terproteksi st.secrets["ADMIN_PASSWORD"]
- Peta interaktif Esri.WorldImagery (tanpa CARTO) + kontrol layer & legenda perubahan
- Kartu KPI, grafik tren historis (Plotly), & tabel riwayat yang beradaptasi dinamis
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import ee
import folium
import gspread
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
from streamlit_folium import st_folium

import gee_engine
from ai_validator import validasi_kja_dengan_ai

# ---------------------------------------------------------------------------
# Konstanta Sistem Terkalibrasi (Sensus Lapangan 2026: 49.807 petak / 249,04 Ha)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    "kuota_petak_pemprov": 11306,
    "luas_petak_min_m2": 48.0,
    "luas_petak_tengah_m2": 50.0,
    "luas_petak_maks_m2": 52.0,
    "tahun_kalibrasi": 2026,
    "sensus_petak": 49807,
    "sensus_luas_ha": 249.04,
}

SPREADSHEET_DEFAULT_NAME = "Monitoring_KJA_Jatiluhur"
TAB_KONFIGURASI = "konfigurasi"
TAB_RIWAYAT = "riwayat_mingguan"

# Fallback header 24 kolom (A1:X1) hanya digunakan jika tab riwayat_mingguan benar-benar kosong
STANDARD_RIWAYAT_HEADERS = [
    "minggu_id",
    "t1_awal",
    "t1_akhir",
    "t2_awal",
    "t2_akhir",
    "tgl_citra_s1",
    "tgl_citra_s2",
    "jumlah_klaster",
    "luas_bersih_ha",
    "estimasi_petak_min",
    "estimasi_petak_tengah",
    "estimasi_petak_maks",
    "persen_kuota",
    "klaster_tetap",
    "klaster_baru",
    "klaster_hilang",
    "klaster_bergeser",
    "delta_luas_ha",
    "delta_petak",
    "status_validasi_ai",
    "ringkasan_ai",
    "rekomendasi_ai",
    "url_peta",
    "dicatat_pada",
]

CORE_KPI_KEYS = {
    "minggu_id",
    "t1_awal",
    "t1_akhir",
    "t2_awal",
    "t2_akhir",
    "tgl_citra_s1",
    "tgl_citra_s2",
    "jumlah_klaster",
    "luas_bersih_ha",
    "estimasi_petak_min",
    "estimasi_petak_tengah",
    "estimasi_petak_maks",
    "persen_kuota",
}

NON_KPI_TEXT_KEYS = {
    "status_validasi_ai",
    "ringkasan_ai",
    "rekomendasi_ai",
    "url_peta",
    "dicatat_pada",
    "analisis_pola",
    "rekomendasi_1",
    "rekomendasi_2",
    "rekomendasi_3",
    "url_thumbnail",
    "waktu_analisis_wib",
    "dijalankan_oleh",
    "indikasi_pola",
    "status_kuota",
}

WIB_TZ = timezone(timedelta(hours=7))

# Koordinat tengah Waduk Jatiluhur
JATILUHUR_CENTER = [-6.5445, 107.3765]

# ---------------------------------------------------------------------------
# Konfigurasi Halaman & Gaya CSS Modern Clean Data Dashboard
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Pemantauan KJA Waduk Jatiluhur",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def is_embed_mode() -> bool:
    """Mendeteksi parameter query ?embed=true secara aman di Streamlit terbaru."""
    val = st.query_params.get("embed", "false")
    if isinstance(val, list):
        val = val[0] if val else "false"
    return str(val).strip().lower() in ("true", "1", "yes")


def inject_modern_clean_css(embed: bool) -> None:
    """Menyuntikkan tema Modern Clean Data Dashboard (gaya ITC Trade Centre)."""
    top_padding = "0.85rem" if embed else "1.5rem"
    hide_chrome = (
        """
        header[data-testid="stHeader"],
        div[data-testid="stToolbar"],
        div[data-testid="stDecoration"],
        #MainMenu,
        footer {
            display: none !important;
        }
        """
        if embed
        else """
        header[data-testid="stHeader"] {
            background: transparent;
        }
        #MainMenu, footer {
            visibility: hidden;
        }
        """
    )

    css_block = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
:root {{
  --bg-page: #F8FAFC;
  --bg-card: #FFFFFF;
  --border-subtle: #E2E8F0;
  --text-dark: #0F172A;
  --text-muted: #64748B;
  --accent-blue: #2563EB;
  --accent-blue-hover: #1D4ED8;
}}
{hide_chrome}
html, body, .stApp {{
  color-scheme: light !important;
  background-color: var(--bg-page) !important;
  font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
  color: var(--text-dark) !important;
}}
.block-container {{
  max-width: 1380px;
  padding-top: {top_padding} !important;
  padding-bottom: 2.25rem !important;
  padding-left: 1.5rem !important;
  padding-right: 1.5rem !important;
}}
.itc-topbar-title {{
  font-size: 17px;
  font-weight: 700;
  color: var(--text-dark);
  letter-spacing: -0.02em;
  margin: 0;
  line-height: 1.3;
}}
.itc-topbar-sub {{
  font-size: 12.5px;
  color: var(--text-muted);
  margin: 2px 0 0 0;
}}
/* Paksa tampilan terang bersih pada input, select, dan label form Streamlit */
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label,
.stMarkdown p,
.stMarkdown h1,
.stMarkdown h2,
.stMarkdown h3,
.stMarkdown h4 {{
  color: var(--text-dark) !important;
}}
[data-baseweb="input"],
[data-baseweb="base-input"],
[data-baseweb="select"] > div {{
  background-color: #FFFFFF !important;
  border-color: #CBD5E1 !important;
  color: #0F172A !important;
  border-radius: 8px !important;
}}
[data-baseweb="input"] input,
[data-baseweb="base-input"] input,
input[type="password"],
input[type="text"],
input[type="number"] {{
  background-color: #FFFFFF !important;
  color: #0F172A !important;
  -webkit-text-fill-color: #0F172A !important;
}}
[data-testid="stForm"] {{
  border: 1px solid var(--border-subtle) !important;
  border-radius: 10px !important;
  background-color: #F8FAFC !important;
  padding: 16px !important;
}}
/* Tombol pil biru lembut (#2563EB) untuk tombol primary maupun form submit */
div[data-testid="stButton"] > button[kind="primary"],
div[data-testid="stFormSubmitButton"] > button,
div[data-testid="stFormSubmitButton"] > button[kind="primary"],
div[data-testid="stFormSubmitButton"] > button[kind="secondaryFormSubmit"] {{
  background-color: var(--accent-blue) !important;
  border: 1px solid var(--accent-blue) !important;
  color: #FFFFFF !important;
  border-radius: 9999px !important;
  font-weight: 600 !important;
  font-size: 13px !important;
  padding: 0.45rem 1.1rem !important;
  box-shadow: none !important;
  transition: background-color 150ms ease;
}}
div[data-testid="stButton"] > button[kind="primary"]:hover,
div[data-testid="stFormSubmitButton"] > button:hover {{
  background-color: var(--accent-blue-hover) !important;
  border-color: var(--accent-blue-hover) !important;
}}
div[data-testid="stButton"] > button[kind="secondary"] {{
  background-color: var(--bg-card) !important;
  border: 1px solid var(--border-subtle) !important;
  color: var(--text-dark) !important;
  border-radius: 9999px !important;
  font-weight: 600 !important;
  font-size: 13px !important;
  padding: 0.45rem 1.05rem !important;
}}
.itc-card {{
  background-color: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 18px 20px;
  margin-bottom: 16px;
}}
.itc-kpi-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}}
.itc-kpi-card {{
  background-color: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 6px;
}}
.itc-kpi-label {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  line-height: 1.35;
}}
.itc-kpi-value {{
  font-family: 'JetBrains Mono', 'Plus Jakarta Sans', monospace;
  font-variant-numeric: tabular-nums;
  font-size: 24px;
  font-weight: 600;
  color: var(--text-dark);
  letter-spacing: -0.02em;
  line-height: 1.2;
}}
.itc-kpi-meta {{
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.4;
}}
.status-badge {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 9999px;
  font-size: 12px;
  font-weight: 600;
  width: fit-content;
}}
.status-valid {{
  background-color: #ECFDF5;
  color: #15803D;
  border: 1px solid #BBF7D0;
}}
.status-check {{
  background-color: #FFFBEB;
  color: #B45309;
  border: 1px solid #FDE68A;
}}
.itc-section-title {{
  font-size: 15px;
  font-weight: 700;
  color: var(--text-dark);
  margin: 0 0 4px 0;
}}
.itc-section-desc {{
  font-size: 12.5px;
  color: var(--text-muted);
  margin: 0 0 14px 0;
}}
.map-legend {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  padding: 10px 14px;
  background-color: #F8FAFC;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  margin-top: 10px;
  font-size: 12.5px;
  color: var(--text-dark);
}}
.legend-item {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 500;
}}
.legend-swatch {{
  width: 12px;
  height: 12px;
  border-radius: 3px;
  display: inline-block;
  border: 1px solid rgba(15, 23, 42, 0.2);
}}
div[data-testid="stVerticalBlockBorderWrapper"] {{
  background-color: var(--bg-card);
  border: 1px solid var(--border-subtle) !important;
  border-radius: 12px !important;
  padding: 16px 18px !important;
}}
</style>
"""
    st.markdown(css_block, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Autentikasi Terpusat GEE & Google Sheets dari st.secrets["gcp_service_account"]
# ---------------------------------------------------------------------------
def _get_service_account_dict() -> dict[str, Any]:
    """Membaca kredensial gcp_service_account dari st.secrets."""
    if "gcp_service_account" not in st.secrets:
        raise KeyError("st.secrets['gcp_service_account'] belum dikonfigurasi.")
    raw = dict(st.secrets["gcp_service_account"])
    if "private_key" in raw and isinstance(raw["private_key"], str):
        raw["private_key"] = raw["private_key"].replace("\\n", "\n")
    return raw


@st.cache_resource(show_spinner=False)
def init_earth_engine() -> tuple[bool, str]:
    """Menginisialisasi Google Earth Engine otomatis dari st.secrets['gcp_service_account']."""
    try:
        sa_info = _get_service_account_dict()
        gee_engine.inisialisasi_ee(sa_info)
        return True, "Google Earth Engine terhubung."
    except Exception as exc:
        return False, f"Inisialisasi GEE gagal: {exc}"


@st.cache_resource(show_spinner=False)
def get_gspread_client() -> gspread.Client:
    """Membuat client gspread terautentikasi dari st.secrets['gcp_service_account']."""
    sa_info = _get_service_account_dict()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def _open_spreadsheet() -> gspread.Spreadsheet:
    client = get_gspread_client()
    sheet_name = st.secrets.get("GSHEETS_SPREADSHEET_NAME", SPREADSHEET_DEFAULT_NAME)
    return client.open(str(sheet_name))


def _ensure_worksheet(
    spreadsheet: gspread.Spreadsheet,
    title: str,
    headers: list[str],
    default_rows: list[list[Any]] | None = None,
) -> gspread.Worksheet:
    """Memastikan worksheet ada dan memiliki header standar otomatis jika masih kosong."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(20, len(headers) + 2))

    existing_values = ws.get_all_values()
    if not existing_values:
        ws.append_row(headers, value_input_option="USER_ENTERED")
        if default_rows:
            ws.append_rows(default_rows, value_input_option="USER_ENTERED")
    return ws


def _parse_id_float(val: Any) -> float:
    """Mengurai angka format internasional atau Indonesia ('11.306', '49.807', '249,04', '1.234,5')."""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(" ", "")
    if not s:
        raise ValueError("Kosong")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


@st.cache_data(ttl=120, show_spinner=False)
def muat_konfigurasi_sheets() -> tuple[dict[str, Any], str | None]:
    """Membaca konfigurasi dinamis dari tab 'konfigurasi' di Google Sheets."""
    config = dict(DEFAULT_CONFIG)
    try:
        ss = _open_spreadsheet()
        default_rows = [[k, v] for k, v in DEFAULT_CONFIG.items()]
        ws = _ensure_worksheet(ss, TAB_KONFIGURASI, ["parameter", "nilai"], default_rows)
        records = ws.get_all_records()
        recorded_params: set[str] = set()
        for row in records:
            param = str(row.get("parameter", "")).strip()
            val = row.get("nilai")
            if param:
                recorded_params.add(param)
            if param and val not in (None, ""):
                try:
                    num_val = _parse_id_float(val)
                    if num_val.is_integer() and param in (
                        "kuota_petak_pemprov",
                        "tahun_kalibrasi",
                        "sensus_petak",
                        "sensus_petak_2026",
                    ):
                        config[param] = int(num_val)
                    else:
                        config[param] = num_val
                except (TypeError, ValueError):
                    config[param] = val

        # Dukungan kompatibilitas mundur jika sheet masih memakai kunci lama 2026
        if "sensus_petak_2026" in config and "sensus_petak" not in recorded_params:
            config["sensus_petak"] = config["sensus_petak_2026"]
        if "sensus_luas_ha_2026" in config and "sensus_luas_ha" not in recorded_params:
            config["sensus_luas_ha"] = config["sensus_luas_ha_2026"]
        return config, None
    except Exception as exc:
        return config, f"Menggunakan konfigurasi baku lokal ({exc})"


def perbarui_konfigurasi_sensus(
    tahun_kalibrasi: int,
    sensus_petak: int,
    sensus_luas_ha: float,
) -> dict[str, Any]:
    """
    Menghitung ulang rasio luas efektif per petak ((luas_ha * 10000) / jumlah_petak)
    dengan margin proporsional +-4%, lalu memperbarui tab 'konfigurasi' di Google Sheets.
    """
    if sensus_petak <= 0 or sensus_luas_ha <= 0:
        raise ValueError("Jumlah petak aktual dan luas areal aktual harus lebih besar dari 0.")

    luas_tengah = round((sensus_luas_ha * 10000.0) / sensus_petak, 2)
    luas_min = round(luas_tengah * 0.96, 2)
    luas_maks = round(luas_tengah * 1.04, 2)

    updates: dict[str, Any] = {
        "luas_petak_min_m2": luas_min,
        "luas_petak_tengah_m2": luas_tengah,
        "luas_petak_maks_m2": luas_maks,
        "tahun_kalibrasi": int(tahun_kalibrasi),
        "sensus_petak": int(sensus_petak),
        "sensus_luas_ha": round(float(sensus_luas_ha), 2),
    }

    ss = _open_spreadsheet()
    default_rows = [[k, v] for k, v in DEFAULT_CONFIG.items()]
    ws = _ensure_worksheet(ss, TAB_KONFIGURASI, ["parameter", "nilai"], default_rows)

    all_values = ws.get_all_values()
    param_row_map: dict[str, int] = {}
    for row_idx, row_vals in enumerate(all_values[1:], start=2):
        if row_vals and str(row_vals[0]).strip():
            param_row_map[str(row_vals[0]).strip()] = row_idx

    rows_to_append: list[list[Any]] = []
    for key, val in updates.items():
        if key in param_row_map:
            ws.update_cell(param_row_map[key], 2, val)
        else:
            rows_to_append.append([key, val])

    if rows_to_append:
        ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    muat_konfigurasi_sheets.clear()
    return updates


@st.cache_data(ttl=60, show_spinner=False)
def muat_riwayat_sheets() -> tuple[pd.DataFrame, str | None]:
    """Membaca seluruh baris riwayat dari tab 'riwayat_mingguan' sesuai header baris 1 eksisting."""
    try:
        ss = _open_spreadsheet()
        ws = _ensure_worksheet(ss, TAB_RIWAYAT, STANDARD_RIWAYAT_HEADERS)
        headers = [str(h).strip() for h in ws.row_values(1) if str(h).strip()]
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=headers or STANDARD_RIWAYAT_HEADERS), None
        df = pd.DataFrame(records)
        return df, None
    except Exception as exc:
        return pd.DataFrame(columns=STANDARD_RIWAYAT_HEADERS), f"Gagal membaca Google Sheets: {exc}"


def _to_float_safe(val: Any, default: float = 0.0) -> float:
    try:
        if val in (None, ""):
            return default
        return _parse_id_float(val)
    except (TypeError, ValueError):
        return default


def _to_int_safe(val: Any, default: int = 0) -> int:
    try:
        if val in (None, ""):
            return default
        return int(round(_parse_id_float(val)))
    except (TypeError, ValueError):
        return default


def simpan_hasil_ke_sheets(
    metrik: dict[str, Any],
    hasil_ai: dict[str, Any],
    url_thumbnail: str,
    detail: dict[str, Any] | None = None,
    t1_awal: str = "",
    t1_akhir: str = "",
    t2_awal: str = "",
    t2_akhir: str = "",
) -> None:
    """
    Menyimpan hasil analisis ke tab 'riwayat_mingguan' dengan membaca header baris 1 eksisting
    (sheet.row_values(1)) TANPA menimpa atau mengubah baris header 1.
    """
    ss = _open_spreadsheet()
    ws = _ensure_worksheet(ss, TAB_RIWAYAT, STANDARD_RIWAYAT_HEADERS)

    # Baca daftar header eksisting pada baris 1 (mis. 24 kolom A1:X1) tanpa memodifikasinya
    sheet_headers = ws.row_values(1)
    if not sheet_headers:
        sheet_headers = list(STANDARD_RIWAYAT_HEADERS)
        ws.append_row(sheet_headers, value_input_option="USER_ENTERED")

    # Ekstrak rentang periode dan metrik perubahan langsung dari gee_engine.py (sertakan_detail=True)
    detail_data = detail or {}
    periode = detail_data.get("periode", {})
    t1_s1 = periode.get("t1", {}).get("s1")
    t2_s1 = periode.get("t2", {}).get("s1")
    if isinstance(t1_s1, (list, tuple)) and len(t1_s1) == 2:
        t1_awal = t1_awal or str(t1_s1[0])
        t1_akhir = t1_akhir or str(t1_s1[1])
    if isinstance(t2_s1, (list, tuple)) and len(t2_s1) == 2:
        t2_awal = t2_awal or str(t2_s1[0])
        t2_akhir = t2_akhir or str(t2_s1[1])

    perubahan = detail_data.get("perubahan", {})
    t2_info = detail_data.get("t2", {})

    # Baca baris sebelumnya sebagai cadangan delta jika detail perubahan tidak tersedia
    prev_records = ws.get_all_records()
    prev_row: dict[str, Any] = prev_records[-1] if prev_records else {}

    curr_luas = _to_float_safe(metrik.get("luas_bersih_ha"), 0.0)
    curr_petak = _to_int_safe(metrik.get("estimasi_petak_tengah"), 0)

    if prev_row:
        prev_luas = _to_float_safe(prev_row.get("luas_bersih_ha"), curr_luas)
        prev_petak = _to_int_safe(prev_row.get("estimasi_petak_tengah"), curr_petak)
        auto_delta_luas = round(curr_luas - prev_luas, 2)
        auto_delta_petak = int(curr_petak - prev_petak)
    else:
        auto_delta_luas = 0.0
        auto_delta_petak = 0

    delta_luas_val = perubahan.get("selisih_luas_ha", metrik.get("delta_luas_ha", auto_delta_luas))
    delta_petak_val = perubahan.get("selisih_petak_tengah", metrik.get("delta_petak", auto_delta_petak))

    rekomendasi_list = [
        str(item).strip()
        for item in hasil_ai.get("rekomendasi_pengawasan", [])
        if str(item).strip()
    ]
    rekomendasi_gabungan = "\n".join(
        f"{idx}. {poin}" for idx, poin in enumerate(rekomendasi_list, start=1)
    )
    waktu_wib = datetime.now(WIB_TZ).strftime("%Y-%m-%d %H:%M:%S WIB")

    row_payload: dict[str, Any] = {
        "t1_awal": t1_awal,
        "t1_akhir": t1_akhir,
        "t2_awal": t2_awal,
        "t2_akhir": t2_akhir,
        **metrik,
        "klaster_tetap": perubahan.get("klaster_tetap", metrik.get("klaster_tetap", 0)),
        "klaster_baru": perubahan.get("klaster_baru", metrik.get("klaster_baru", 0)),
        "klaster_hilang": perubahan.get("klaster_hilang", metrik.get("klaster_hilang", 0)),
        "klaster_bergeser": perubahan.get("klaster_bergeser", metrik.get("klaster_bergeser", 0)),
        "delta_luas_ha": delta_luas_val,
        "delta_petak": delta_petak_val,
        "luas_tetap_ha": perubahan.get("luas_tetap_ha", 0.0),
        "luas_hilang_bergeser_ha": perubahan.get("luas_hilang_bergeser_ha", 0.0),
        "luas_baru_ha": perubahan.get("luas_baru_ha", 0.0),
        "indikasi_pola": perubahan.get("indikasi_pola", ""),
        "cakupan_s2_persen": t2_info.get("cakupan_s2_persen", ""),
        "luas_eceng_dibuang_ha": t2_info.get("luas_eceng_dibuang_ha", ""),
        "selisih_kuota_petak": t2_info.get("selisih_kuota_petak", ""),
        "status_kuota": t2_info.get("status_kuota", ""),
        "status_validasi_ai": hasil_ai.get("status_validasi_ai", "Perlu dicek"),
        "ringkasan_ai": hasil_ai.get("analisis_pola", ""),
        "rekomendasi_ai": rekomendasi_gabungan,
        "url_peta": url_thumbnail,
        "dicatat_pada": waktu_wib,
        # Alias kompatibilitas jika ada header alternatif
        "analisis_pola": hasil_ai.get("analisis_pola", ""),
        "url_thumbnail": url_thumbnail,
        "waktu_analisis_wib": waktu_wib,
        "dijalankan_oleh": "Admin Pengawas",
    }

    normalized_payload = {str(k).strip().lower(): v for k, v in row_payload.items()}
    ordered_row = [
        normalized_payload.get(str(col_name).strip().lower(), "")
        for col_name in sheet_headers
    ]

    ws.append_row(ordered_row, value_input_option="USER_ENTERED")
    muat_riwayat_sheets.clear()


# ---------------------------------------------------------------------------
# Peta Interaktif Esri.WorldImagery + Kontrol Layer Earth Engine
# ---------------------------------------------------------------------------
def _extract_ee_layer_entry(layers_peta: dict[str, Any], key: str) -> tuple[Any, dict[str, Any]]:
    """
    Mengekstrak (ee.Image, vis_params) secara fleksibel baik jika layers_peta
    berbentuk {'tetap': {'image': img, 'vis_params': {...}}} maupun
    {'tetap': img, 'vis_params': {'tetap': {...}}}.
    """
    entry = layers_peta.get(key)
    if entry is None:
        return None, {}

    if isinstance(entry, dict):
        img = entry.get("image") or entry.get("ee_image") or entry.get("layer")
        vis = entry.get("vis_params") or entry.get("vis") or {}
        return img, vis

    vis_map = layers_peta.get("vis_params", {})
    vis = vis_map.get(key, {}) if isinstance(vis_map, dict) else {}
    return entry, vis


def _add_ee_tile_layer(
    fmap: folium.Map,
    ee_image: Any,
    vis_params: dict[str, Any],
    name: str,
    show: bool = True,
) -> None:
    """Menambahkan ee.Image sebagai Folium TileLayer via getMapId."""
    if ee_image is None:
        return
    map_id_dict = ee_image.getMapId(vis_params or {})
    folium.TileLayer(
        tiles=map_id_dict["tile_fetcher"].url_format,
        attr="Google Earth Engine / Copernicus Sentinel-2",
        name=name,
        overlay=True,
        control=True,
        show=show,
    ).add_to(fmap)


def bangun_peta_interaktif(
    layers_peta: dict[str, Any] | None,
    gee_ok: bool = False,
) -> folium.Map:
    """Membangun peta Folium berbasemap Esri.WorldImagery selaras dengan warna gee_engine.py."""
    fmap = folium.Map(
        location=JATILUHUR_CENTER,
        zoom_start=13,
        tiles=None,
        control_scale=True,
    )

    # Basemap wajib: Esri.WorldImagery (bukan CARTO)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
        name="Esri.WorldImagery",
        overlay=False,
        control=True,
        show=True,
    ).add_to(fmap)

    if layers_peta:
        layer_specs = [
            ("s2_t1", "Sentinel-2 RGB (T1)", False, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 2500, "gamma": 1.3}),
            ("s2_t2", "Sentinel-2 RGB (T2)", False, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 2500, "gamma": 1.3}),
            ("kja_t1", "KJA Periode Lalu (T1 - Cyan)", False, {"min": 0, "max": 1, "palette": ["000000", "00E5FF"]}),
            ("kja_t2", "KJA Terbaru (T2 - Oranye)", False, {"min": 0, "max": 1, "palette": ["000000", "FF9100"]}),
            ("tetap", "Perubahan: KJA Tetap (Kuning)", True, {"min": 0, "max": 1, "palette": ["000000", "FFD600"]}),
            ("bergeser", "Perubahan: KJA Hilang/Bergeser (Merah)", True, {"min": 0, "max": 1, "palette": ["000000", "D50000"]}),
            ("baru", "Perubahan: KJA Baru (Hijau)", True, {"min": 0, "max": 1, "palette": ["000000", "00C853"]}),
        ]
        for key, label, default_show, fallback_vis in layer_specs:
            ee_img, vis = _extract_ee_layer_entry(layers_peta, key)
            if ee_img is not None:
                try:
                    _add_ee_tile_layer(
                        fmap=fmap,
                        ee_image=ee_img,
                        vis_params=vis or fallback_vis,
                        name=label,
                        show=default_show,
                    )
                except Exception as exc:
                    st.warning(f"Layer '{label}' tidak dapat dirender: {exc}")

    if gee_ok:
        try:
            s, w, n, e = gee_engine.ambil_bbox_waduk()
            fmap.fit_bounds([[s, w], [n, e]])
        except Exception:
            pass

    folium.LayerControl(collapsed=False, position="topright").add_to(fmap)
    return fmap


# ---------------------------------------------------------------------------
# Komponen Rendering Dinamis (KPI Cards, Metodologi, AI Card, Grafik & Tabel)
# ---------------------------------------------------------------------------
def _fmt_int(val: Any) -> str:
    try:
        return f"{int(round(float(val))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(val)


def _fmt_float(val: Any, decimals: int = 2) -> str:
    try:
        return f"{float(val):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(val)


def _humanize_key(key: str) -> str:
    return key.replace("_", " ").strip().title()


def render_kpi_dinamis(
    metrik: dict[str, Any],
    status_ai: str,
    kuota_pemprov: int,
    detail: dict[str, Any] | None = None,
) -> None:
    """Merender kartu KPI putih bersih secara dinamis mengikuti seluruh output metrik & detail."""
    minggu_id = metrik.get("minggu_id", "-")
    tgl_s1 = metrik.get("tgl_citra_s1", "-")
    tgl_s2 = metrik.get("tgl_citra_s2", "-")
    klaster = metrik.get("jumlah_klaster", 0)
    luas_ha = metrik.get("luas_bersih_ha", 0.0)
    petak_min = metrik.get("estimasi_petak_min", 0)
    petak_tengah = metrik.get("estimasi_petak_tengah", 0)
    petak_maks = metrik.get("estimasi_petak_maks", 0)
    persen_kuota = metrik.get("persen_kuota", 0.0)

    detail_data = detail or {}
    periode = detail_data.get("periode", {})
    t1_s1 = periode.get("t1", {}).get("s1")
    t2_s1 = periode.get("t2", {}).get("s1")
    t1_awal = t1_s1[0] if isinstance(t1_s1, (list, tuple)) and len(t1_s1) == 2 else metrik.get("t1_awal", "")
    t1_akhir = t1_s1[1] if isinstance(t1_s1, (list, tuple)) and len(t1_s1) == 2 else metrik.get("t1_akhir", "")
    t2_awal = t2_s1[0] if isinstance(t2_s1, (list, tuple)) and len(t2_s1) == 2 else metrik.get("t2_awal", "")
    t2_akhir = t2_s1[1] if isinstance(t2_s1, (list, tuple)) and len(t2_s1) == 2 else metrik.get("t2_akhir", "")

    if t1_awal and t1_akhir and t2_awal and t2_akhir:
        periode_line = f"Periode Lalu (T1): {t1_awal} s.d. {t1_akhir} &bull; Terbaru (T2): {t2_awal} s.d. {t2_akhir}<br/>"
    else:
        periode_line = ""

    perubahan = detail_data.get("perubahan", {})
    delta_petak = perubahan.get("selisih_petak_tengah", metrik.get("delta_petak"))
    delta_luas = perubahan.get("selisih_luas_ha", metrik.get("delta_luas_ha"))
    delta_petak_txt = (
        f" ({'+' if _to_int_safe(delta_petak) >= 0 else ''}{_fmt_int(delta_petak)})"
        if delta_petak not in (None, "")
        else ""
    )
    delta_luas_txt = (
        f" ({'+' if _to_float_safe(delta_luas) >= 0 else ''}{_fmt_float(delta_luas, 2)} Ha)"
        if delta_luas not in (None, "")
        else ""
    )

    status_class = "status-valid" if status_ai == "Valid" else "status-check"
    status_label = status_ai if status_ai in ("Valid", "Perlu dicek") else "Belum divalidasi AI"

    cards_html = [
        f'<div class="itc-kpi-card"><div class="itc-kpi-label">Periode &amp; Akuisisi Satelit</div><div class="itc-kpi-value">{minggu_id}</div><div class="itc-kpi-meta">{periode_line}Radar S1 (T2): {tgl_s1} &bull; Optik S2 (T2): {tgl_s2}</div></div>',
        f'<div class="itc-kpi-card"><div class="itc-kpi-label">Estimasi Petak (Nilai Tengah)</div><div class="itc-kpi-value">{_fmt_int(petak_tengah)}</div><div class="itc-kpi-meta">Rentang: {_fmt_int(petak_min)} s.d. {_fmt_int(petak_maks)} petak{delta_petak_txt}</div></div>',
        f'<div class="itc-kpi-card"><div class="itc-kpi-label">Beban Terhadap Kuota Pemprov</div><div class="itc-kpi-value">{_fmt_float(persen_kuota, 1)}%</div><div class="itc-kpi-meta">Batas kuota: {_fmt_int(kuota_pemprov)} petak</div></div>',
        f'<div class="itc-kpi-card"><div class="itc-kpi-label">Luas Bersih &amp; Klaster KJA</div><div class="itc-kpi-value">{_fmt_float(luas_ha, 2)} Ha</div><div class="itc-kpi-meta">{_fmt_int(klaster)} klaster spasial{delta_luas_txt}</div></div>',
        f'<div class="itc-kpi-card"><div class="itc-kpi-label">Validasi Multimodal AI</div><div><span class="status-badge {status_class}">{status_label}</span></div><div class="itc-kpi-meta">Pemeriksaan awan &amp; eceng gondok (Gemini)</div></div>',
    ]

    # Tambahkan kartu KPI otomatis jika sheet memiliki metrik numerik tambahan di luar kartu inti
    skip_auto_keys = CORE_KPI_KEYS | NON_KPI_TEXT_KEYS | {"delta_petak", "delta_luas_ha"}
    for key, value in metrik.items():
        if key in skip_auto_keys or value in (None, ""):
            continue
        formatted = _fmt_float(value, 2) if isinstance(value, float) else (_fmt_int(value) if isinstance(value, int) else str(value))
        cards_html.append(
            f'<div class="itc-kpi-card"><div class="itc-kpi-label">{_humanize_key(key)}</div><div class="itc-kpi-value">{formatted}</div><div class="itc-kpi-meta">Metrik pemantauan terpantau</div></div>'
        )

    st.markdown(f'<div class="itc-kpi-grid">{"".join(cards_html)}</div>', unsafe_allow_html=True)


def render_disclaimer_metodologi(config: dict[str, Any]) -> None:
    """Menampilkan kotak informasi metodologi terkalibrasi dinamis + penjelasan T1 & T2."""
    tahun_kalibrasi = int(config.get("tahun_kalibrasi", 2026))
    sensus_petak = _fmt_int(config.get("sensus_petak", config.get("sensus_petak_2026", 49807)))
    sensus_ha = _fmt_float(config.get("sensus_luas_ha", config.get("sensus_luas_ha_2026", 249.04)), 2)
    luas_min = _fmt_float(config.get("luas_petak_min_m2", 48.0), 2)
    luas_tengah = _fmt_float(config.get("luas_petak_tengah_m2", 50.0), 2)
    luas_maks = _fmt_float(config.get("luas_petak_maks_m2", 52.0), 2)

    html_disclaimer = (
        '<div class="itc-card" style="padding: 14px 18px;">'
        '<div style="font-size: 13px; font-weight: 700; color: #0F172A; margin-bottom: 4px;">'
        'Catatan Metodologi &amp; Kalibrasi Penginderaan Jauh'
        '</div>'
        '<div style="font-size: 12.5px; color: #64748B; line-height: 1.55;">'
        'Angka jumlah petak dan luasan KJA merupakan hasil estimasi fusi satelit '
        '<strong>Sentinel-1 SAR (radar 14 hari)</strong> dan <strong>Sentinel-2 optik (penapisan eceng gondok 45 hari)</strong> '
        f'yang telah terkalibrasi terhadap data sensus lapangan tahun <strong>{tahun_kalibrasi}</strong> sebesar '
        f'<strong>{sensus_petak} petak ({sensus_ha} Ha)</strong> '
        f'dengan tingkat akurasi empiris ~99% (asumsi luasan efektif per petak {luas_min}&ndash;{luas_maks} m&sup2;, nilai tengah {luas_tengah} m&sup2;). '
        'Pada setiap perbandingan siklus, <strong>T1 adalah kondisi periode sebelumnya (14 hari lalu)</strong> dan '
        '<strong>T2 adalah kondisi pemantauan terbaru</strong>. Data ini berfungsi sebagai '
        '<strong>sistem peringatan dini (early warning system)</strong> untuk mendukung verifikasi patroli lapangan pengawas Waduk Jatiluhur.'
        '</div>'
        '</div>'
    )
    st.markdown(html_disclaimer, unsafe_allow_html=True)


def render_grafik_historis(df: pd.DataFrame, kuota_pemprov: int) -> None:
    """Merender grafik tren historis Plotly secara dinamis berdasarkan kolom numerik riwayat."""
    if df.empty or "minggu_id" not in df.columns:
        st.info("Belum ada data historis pada tab riwayat_mingguan untuk ditampilkan pada grafik.")
        return

    excluded_cols = {"minggu_id"} | NON_KPI_TEXT_KEYS
    numeric_cols = [
        c
        for c in df.columns
        if c not in excluded_cols
        and pd.to_numeric(df[c], errors="coerce").notna().any()
    ]

    default_metric = "estimasi_petak_tengah" if "estimasi_petak_tengah" in numeric_cols else (numeric_cols[0] if numeric_cols else None)
    if not default_metric:
        st.info("Kolom metrik numerik belum tersedia untuk visualisasi tren.")
        return

    selected_metric = st.selectbox(
        "Pilih metrik tren historis",
        options=numeric_cols,
        index=numeric_cols.index(default_metric),
        format_func=_humanize_key,
    )

    plot_df = df.copy()
    plot_df[selected_metric] = pd.to_numeric(plot_df[selected_metric], errors="coerce")

    fig = go.Figure()

    # Tampilkan pita rentang min-maks jika sedang melihat estimasi_petak_tengah
    if selected_metric == "estimasi_petak_tengah" and {"estimasi_petak_min", "estimasi_petak_maks"}.issubset(plot_df.columns):
        y_min = pd.to_numeric(plot_df["estimasi_petak_min"], errors="coerce")
        y_maks = pd.to_numeric(plot_df["estimasi_petak_maks"], errors="coerce")
        fig.add_trace(
            go.Scatter(
                x=plot_df["minggu_id"],
                y=y_maks,
                mode="lines",
                line={"width": 0},
                showlegend=False,
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=plot_df["minggu_id"],
                y=y_min,
                mode="lines",
                line={"width": 0},
                fill="tonexty",
                fillcolor="rgba(37, 99, 235, 0.12)",
                name="Rentang Min-Maks Petak",
            )
        )

    fig.add_trace(
        go.Scatter(
            x=plot_df["minggu_id"],
            y=plot_df[selected_metric],
            mode="lines+markers",
            name=_humanize_key(selected_metric),
            line={"color": "#2563EB", "width": 2.5},
            marker={"size": 7, "color": "#2563EB"},
        )
    )

    if selected_metric in ("estimasi_petak_tengah", "estimasi_petak_min", "estimasi_petak_maks"):
        fig.add_hline(
            y=kuota_pemprov,
            line_dash="dash",
            line_color="#DC2626",
            annotation_text=f"Kuota Pemprov Jabar ({_fmt_int(kuota_pemprov)} petak)",
            annotation_position="top left",
            annotation_font_color="#DC2626",
        )

    fig.update_layout(
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font={"family": "Plus Jakarta Sans, sans-serif", "color": "#0F172A", "size": 12},
        margin={"l": 12, "r": 12, "t": 28, "b": 12},
        height=340,
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
        xaxis={"gridcolor": "#F1F5F9", "title": "Periode Pemantauan"},
        yaxis={"gridcolor": "#E2E8F0", "title": _humanize_key(selected_metric)},
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# State & Alur Aplikasi Utama
# ---------------------------------------------------------------------------
def main() -> None:
    embed = is_embed_mode()
    inject_modern_clean_css(embed)

    if "is_admin" not in st.session_state:
        st.session_state["is_admin"] = False
    if "show_admin_login" not in st.session_state:
        st.session_state["show_admin_login"] = False
    if "latest_run_result" not in st.session_state:
        st.session_state["latest_run_result"] = None

    gee_ok, gee_msg = init_earth_engine()
    config, config_warn = muat_konfigurasi_sheets()
    df_riwayat, sheets_err = muat_riwayat_sheets()
    kuota_pemprov = int(config.get("kuota_petak_pemprov", 11306))

    # Top bar putih minimalis terpadu (judul di kiri, kontrol Mode Admin biru lembut #2563EB di kanan)
    with st.container(border=True):
        col_title, col_admin = st.columns([5.2, 1.5], vertical_alignment="center")
        with col_title:
            mode_text = "Mode Admin Aktif" if st.session_state["is_admin"] else "Akses Publik (Read-Only)"
            st.markdown(
                f'<div><div class="itc-topbar-title">Pemantauan Keramba Jaring Apung (KJA) Waduk Jatiluhur</div>'
                f'<div class="itc-topbar-sub">Pemantauan Satelit Siklus 14 Hari &middot; {mode_text}</div></div>',
                unsafe_allow_html=True,
            )

        with col_admin:
            if st.session_state["is_admin"]:
                if st.button("Keluar Mode Admin", type="secondary", use_container_width=True):
                    st.session_state["is_admin"] = False
                    st.session_state["show_admin_login"] = False
                    st.rerun()
            else:
                if st.button("Mode Admin", type="primary", use_container_width=True):
                    st.session_state["show_admin_login"] = not st.session_state["show_admin_login"]

    # Panel Login Mode Admin (menggunakan st.secrets["ADMIN_PASSWORD"])
    if st.session_state["show_admin_login"] and not st.session_state["is_admin"]:
        with st.container(border=True):
            st.markdown("#### Autentikasi Mode Admin")
            with st.form("admin_login_form", clear_on_submit=True):
                pwd_input = st.text_input("Masukkan kata sandi admin", type="password")
                submitted = st.form_submit_button("Buka Akses Admin", type="primary")
                if submitted:
                    expected_pwd = str(st.secrets.get("ADMIN_PASSWORD", ""))
                    if expected_pwd and hmac.compare_digest(pwd_input, expected_pwd):
                        st.session_state["is_admin"] = True
                        st.session_state["show_admin_login"] = False
                        st.rerun()
                    else:
                        st.error("Kata sandi admin tidak valid atau ADMIN_PASSWORD belum dikonfigurasi.")

    if config_warn:
        st.caption(config_warn)
    if sheets_err:
        st.warning(sheets_err)

    # Panel Eksekusi Admin (hanya muncul bila Mode Admin aktif)
    if st.session_state["is_admin"]:
        with st.container(border=True):
            st.markdown('<div class="itc-section-title">Panel Eksekusi Analisis Berkala (Siklus 14 Hari)</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="itc-section-desc">Menjalankan mesin pemetaan Google Earth Engine (gee_engine.py), validasi visual Gemini 2.5 Flash, dan penyimpanan otomatis ke Google Sheets.</div>',
                unsafe_allow_html=True,
            )

            today = date.today()
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                tgl_akhir_t2 = st.date_input("Tgl Akhir Periode Terbaru (T2)", value=today)
            with c2:
                jendela_s1_hari = st.number_input("Jendela Radar S1 (hari)", min_value=7, max_value=30, value=14, step=1)
            with c3:
                jendela_s2_hari = st.number_input("Jendela Optik S2 (hari)", min_value=14, max_value=90, value=45, step=1)
            with c4:
                jarak_t1_hari = st.number_input("Jarak Periode Lalu T1 (hari)", min_value=7, max_value=30, value=14, step=1)

            t2_akhir_dt = tgl_akhir_t2
            t2_awal_dt = t2_akhir_dt - timedelta(days=int(jendela_s1_hari) - 1)
            t1_akhir_dt = t2_akhir_dt - timedelta(days=int(jarak_t1_hari))
            t1_awal_dt = t1_akhir_dt - timedelta(days=int(jendela_s1_hari) - 1)
            st.caption(
                f"Rentang radar T1 (Periode Lalu): {t1_awal_dt.isoformat()} s.d. {t1_akhir_dt.isoformat()} • "
                f"T2 (Terbaru): {t2_awal_dt.isoformat()} s.d. {t2_akhir_dt.isoformat()}"
            )

            if not gee_ok:
                st.error(gee_msg)

            run_clicked = st.button(
                "Jalankan Analisis & Simpan ke Database",
                type="primary",
                disabled=not gee_ok,
            )

            if run_clicked:
                with st.status("Memproses pipeline pemantauan KJA Waduk Jatiluhur...", expanded=True) as status_box:
                    try:
                        st.write("1/3 Menjalankan mesin pemetaan satelit (gee_engine.py)...")
                        hasil_gee = gee_engine.jalankan_analisis_mingguan(
                            tgl_akhir=tgl_akhir_t2.isoformat(),
                            konfigurasi=config,
                            jendela_s1_hari=int(jendela_s1_hari),
                            jendela_s2_hari=int(jendela_s2_hari),
                            jarak_t1_hari=int(jarak_t1_hari),
                            sertakan_detail=True,
                            kredensial=_get_service_account_dict(),
                        )

                        metrik = hasil_gee["metrik"]
                        url_thumbnail = hasil_gee["url_thumbnail"]
                        layers_peta = hasil_gee.get("layers_peta")
                        detail_gee = hasil_gee.get("detail", {})

                        st.write("2/3 Memvalidasi citra komposit dengan Gemini 2.5 Flash (ai_validator.py)...")
                        hasil_ai = validasi_kja_dengan_ai(
                            url_thumbnail,
                            metrik,
                            detail=detail_gee,
                            izinkan_tanpa_citra=False,
                        )

                        st.write("3/3 Menyimpan hasil metrik, detail perubahan & validasi ke Google Sheets (24 kolom)...")
                        simpan_hasil_ke_sheets(
                            metrik=metrik,
                            hasil_ai=hasil_ai,
                            url_thumbnail=url_thumbnail,
                            detail=detail_gee,
                            t1_awal=t1_awal_dt.isoformat(),
                            t1_akhir=t1_akhir_dt.isoformat(),
                            t2_awal=t2_awal_dt.isoformat(),
                            t2_akhir=t2_akhir_dt.isoformat(),
                        )

                        st.session_state["latest_run_result"] = {
                            "metrik": metrik,
                            "url_thumbnail": url_thumbnail,
                            "layers_peta": layers_peta,
                            "detail": detail_gee,
                            "ai": hasil_ai,
                        }
                        status_box.update(label="Analisis selesai dan tersimpan ke database.", state="complete")
                        st.rerun()
                    except Exception as exc:
                        status_box.update(label="Terjadi kendala saat menjalankan analisis.", state="error")
                        st.error(f"Detail error: {exc}")

        # Form Kalibrasi & Pembaruan Data Sensus Lapangan (Tahun 2027 dst.)
        with st.container(border=True):
            st.markdown('<div class="itc-section-title">Kalibrasi &amp; Pembaruan Data Sensus Lapangan</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="itc-section-desc">Perbarui parameter acuan sensus lapangan tahun terbaru (mis. 2027) untuk menghitung ulang rasio luas efektif per petak secara otomatis (margin proporsional &plusmn;4%).</div>',
                unsafe_allow_html=True,
            )

            with st.form("sensus_calibration_form"):
                sc1, sc2, sc3 = st.columns(3)
                with sc1:
                    tahun_input = st.number_input(
                        "Tahun Sensus",
                        min_value=2024,
                        max_value=2100,
                        value=int(config.get("tahun_kalibrasi", 2026)),
                        step=1,
                    )
                with sc2:
                    petak_input = st.number_input(
                        "Jumlah Petak Aktual",
                        min_value=1,
                        value=int(config.get("sensus_petak", config.get("sensus_petak_2026", 49807))),
                        step=100,
                    )
                with sc3:
                    luas_input = st.number_input(
                        "Luas Areal Aktual (Ha)",
                        min_value=0.1,
                        value=float(config.get("sensus_luas_ha", config.get("sensus_luas_ha_2026", 249.04))),
                        step=0.5,
                        format="%.2f",
                    )

                preview_tengah = round((float(luas_input) * 10000.0) / max(int(petak_input), 1), 2)
                preview_min = round(preview_tengah * 0.96, 2)
                preview_maks = round(preview_tengah * 1.04, 2)
                st.caption(
                    f"Pratinjau rasio luas efektif baru: tengah {preview_tengah:.2f} m²/petak "
                    f"(rentang min {preview_min:.2f} m² s.d. maks {preview_maks:.2f} m²)."
                )

                save_sensus = st.form_submit_button("Simpan Kalibrasi Sensus ke Konfigurasi", type="primary")
                if save_sensus:
                    try:
                        updated = perbarui_konfigurasi_sensus(
                            tahun_kalibrasi=int(tahun_input),
                            sensus_petak=int(petak_input),
                            sensus_luas_ha=float(luas_input),
                        )
                        st.success(
                            f"Kalibrasi sensus tahun {updated['tahun_kalibrasi']} berhasil disimpan "
                            f"({updated['luas_petak_tengah_m2']} m²/petak)."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Gagal menyimpan pembaruan sensus ke tab konfigurasi: {exc}")

    # Menentukan sumber data periode aktif untuk ditampilkan (hasil run terbaru atau baris terakhir Google Sheets)
    active_run = st.session_state.get("latest_run_result")
    latest_sheet_row: dict[str, Any] = df_riwayat.iloc[-1].to_dict() if not df_riwayat.empty else {}

    if active_run:
        active_metrik = active_run["metrik"]
        active_ai = active_run["ai"]
        active_thumb = active_run["url_thumbnail"]
        active_layers = active_run.get("layers_peta")
        active_detail = active_run.get("detail")
    elif latest_sheet_row:
        active_metrik = {
            k: v
            for k, v in latest_sheet_row.items()
            if k not in NON_KPI_TEXT_KEYS
        }
        raw_rekomendasi_str = str(
            latest_sheet_row.get("rekomendasi_ai")
            or "\n".join(
                str(latest_sheet_row.get(k, "")).strip()
                for k in ("rekomendasi_1", "rekomendasi_2", "rekomendasi_3")
                if str(latest_sheet_row.get(k, "")).strip()
            )
        )
        rekomendasi_parsed = [
            line.lstrip("0123456789. ").strip()
            for line in raw_rekomendasi_str.splitlines()
            if line.strip()
        ]
        active_ai = {
            "status_validasi_ai": latest_sheet_row.get("status_validasi_ai", "Perlu dicek"),
            "analisis_pola": latest_sheet_row.get(
                "ringkasan_ai",
                latest_sheet_row.get("analisis_pola", "Belum tersedia analisis AI."),
            ),
            "rekomendasi_pengawasan": rekomendasi_parsed,
        }
        active_thumb = str(
            latest_sheet_row.get("url_peta")
            or latest_sheet_row.get("url_thumbnail")
            or ""
        )
        active_layers = None
        active_detail = None
    else:
        active_metrik = {
            "minggu_id": "Belum ada data",
            "tgl_citra_s1": "-",
            "tgl_citra_s2": "-",
            "jumlah_klaster": 0,
            "luas_bersih_ha": 0.0,
            "estimasi_petak_min": 0,
            "estimasi_petak_tengah": 0,
            "estimasi_petak_maks": 0,
            "persen_kuota": 0.0,
        }
        active_ai = {
            "status_validasi_ai": "Perlu dicek",
            "analisis_pola": "Belum ada siklus analisis yang tersimpan di Google Sheets. Aktifkan Mode Admin untuk menjalankan siklus pertama.",
            "rekomendasi_pengawasan": [
                "Masuk ke Mode Admin dan pilih Tanggal Akhir T2 serta jendela analisis.",
                "Klik tombol Jalankan Analisis & Simpan ke Database.",
                "Verifikasi hasil estimasi petak terhadap kuota 11.306 petak.",
            ],
        }
        active_thumb = ""
        active_layers = None
        active_detail = None

    # 1. Kartu Metrik KPI Dinamis + Disclaimer Metodologi + Peringatan Kualitas Citra (bila ada)
    render_kpi_dinamis(
        active_metrik,
        active_ai.get("status_validasi_ai", "Perlu dicek"),
        kuota_pemprov,
        detail=active_detail,
    )
    render_disclaimer_metodologi(config)

    if active_detail:
        peringatan_list = active_detail.get("peringatan", [])
        perubahan_info = active_detail.get("perubahan", {})
        t2_detail = active_detail.get("t2", {})

        if peringatan_list:
            st.warning("Peringatan Kualitas Citra Satelit: " + " | ".join(map(str, peringatan_list)))

        if perubahan_info:
            with st.container(border=True):
                st.markdown('<div class="itc-section-title">Ringkasan Perubahan Spasial &amp; Penapisan S2 (T1 ke T2)</div>', unsafe_allow_html=True)
                p_cols = st.columns(5)
                p_cols[0].metric("Luas Tetap", f"{_fmt_float(perubahan_info.get('luas_tetap_ha', 0), 2)} Ha")
                p_cols[1].metric("Hilang / Bergeser", f"{_fmt_float(perubahan_info.get('luas_hilang_bergeser_ha', 0), 2)} Ha")
                p_cols[2].metric("Luas Baru", f"{_fmt_float(perubahan_info.get('luas_baru_ha', 0), 2)} Ha")
                p_cols[3].metric("Cakupan S2 Bebas Awan", f"{_fmt_float(t2_detail.get('cakupan_s2_persen', 0), 1)}%")
                p_cols[4].metric("Eceng Gondok Dibuang", f"{_fmt_float(t2_detail.get('luas_eceng_dibuang_ha', 0), 2)} Ha")
                if perubahan_info.get("indikasi_pola"):
                    st.caption(f"Indikasi pola otomatis: {perubahan_info['indikasi_pola']}")

    # 2. Area Peta Interaktif (Esri.WorldImagery) & Hasil Validasi Multimodal AI
    col_map, col_ai = st.columns([1.45, 1], gap="medium")

    with col_map:
        with st.container(border=True):
            st.markdown('<div class="itc-section-title">Peta Spasial Perubahan KJA (Esri.WorldImagery)</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="itc-section-desc">Gunakan kontrol layer di kanan atas peta untuk menampilkan Sentinel-2 RGB (T1/T2), KJA Periode Lalu (T1), KJA Terbaru (T2), dan zona perubahan.</div>',
                unsafe_allow_html=True,
            )

            fmap = bangun_peta_interaktif(active_layers, gee_ok=gee_ok)
            st_folium(fmap, width=None, height=460, returned_objects=[])

            st.markdown(
                '<div class="map-legend">'
                '<span class="legend-item"><span class="legend-swatch" style="background:#FFD600;"></span>KJA Tetap (Kuning)</span>'
                '<span class="legend-item"><span class="legend-swatch" style="background:#D50000;"></span>KJA Hilang/Bergeser (Merah)</span>'
                '<span class="legend-item"><span class="legend-swatch" style="background:#00C853;"></span>KJA Baru (Hijau)</span>'
                '<span class="legend-item"><span class="legend-swatch" style="background:#00E5FF;"></span>KJA Periode Lalu (T1 - Cyan)</span>'
                '<span class="legend-item"><span class="legend-swatch" style="background:#FF9100;"></span>KJA Terbaru (T2 - Oranye)</span>'
                '</div>',
                unsafe_allow_html=True,
            )

    with col_ai:
        with st.container(border=True):
            st.markdown('<div class="itc-section-title">Ringkasan Validasi Multimodal AI</div>', unsafe_allow_html=True)
            status_val = active_ai.get("status_validasi_ai", "Perlu dicek")
            badge_cls = "status-valid" if status_val == "Valid" else "status-check"
            st.markdown(
                f'<div style="margin-bottom: 12px;"><span class="status-badge {badge_cls}">Status Citra: {status_val}</span></div>',
                unsafe_allow_html=True,
            )

            if active_thumb:
                st.image(active_thumb, caption="Citra Komposit Satelit Area KJA Terbaru", use_container_width=True)

            st.markdown("**Analisis Pola Spasial & Kuota**")
            st.write(active_ai.get("analisis_pola", "-"))

            st.markdown("**3 Rekomendasi Pengawasan Lapangan**")
            rekomendasi_list = [r for r in active_ai.get("rekomendasi_pengawasan", []) if str(r).strip()]
            for idx, rek in enumerate(rekomendasi_list, start=1):
                st.markdown(f"{idx}. {rek}")

    # 3. Grafik Tren Historis (Plotly) & Tabel Riwayat Dinamis
    with st.container(border=True):
        st.markdown('<div class="itc-section-title">Tren Historis Siklus 14 Hari</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="itc-section-desc">Menyesuaikan otomatis terhadap seluruh kolom metrik numerik yang tercatat pada tab riwayat_mingguan.</div>',
            unsafe_allow_html=True,
        )
        render_grafik_historis(df_riwayat, kuota_pemprov)

    with st.container(border=True):
        st.markdown('<div class="itc-section-title">Tabel Riwayat Pemantauan Mingguan</div>', unsafe_allow_html=True)
        if df_riwayat.empty:
            st.info("Belum ada baris riwayat di tab riwayat_mingguan.")
        else:
            st.dataframe(df_riwayat, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
