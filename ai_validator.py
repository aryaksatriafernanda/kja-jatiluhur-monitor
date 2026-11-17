"""
Fase 3 — Modul Validasi Multimodal KJA Waduk Jatiluhur
Menggunakan SDK resmi terbaru google-genai dengan model gemini-2.5-flash
dan Structured Outputs (Pydantic JSON Schema).
"""

from __future__ import annotations

import json
import os
import time
from io import BytesIO
from typing import Any, Literal

import requests
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field


class ValidasiKJASchema(BaseModel):
    """Skema JSON pasti untuk hasil validasi multimodal KJA oleh Gemini."""

    status_validasi_ai: Literal["Valid", "Perlu dicek"] = Field(
        description=(
            'Wajib bernilai "Valid" apabila citra menunjukkan susunan petak KJA '
            'yang jelas dan bersih, atau "Perlu dicek" apabila terdapat gangguan '
            "awan tebal, kabut pekat, pantulan matahari ekstrem, atau tutupan "
            "eceng gondok yang mengaburkan batas KJA."
        )
    )
    analisis_pola: str = Field(
        description=(
            "Ringkasan 2 sampai 3 kalimat dalam Bahasa Indonesia formal mengenai "
            "pergeseran spasial antar-periode T1 ke T2 serta perbandingan "
            "estimasi jumlah petak terhadap batas kuota Pemprov Jawa Barat "
            "(11.306 petak)."
        )
    )
    rekomendasi_pengawasan: list[str] = Field(
        min_length=3,
        max_length=3,
        description=(
            "Daftar tepat 3 poin tindakan pengawasan lapangan yang spesifik, "
            "terukur, dan praktis untuk petugas pengawas Waduk Jatiluhur."
        ),
    )


def _resolve_gemini_api_key(explicit_api_key: str | None = None) -> str:
    """Mengambil GEMINI_API_KEY dari argumen, environment variable, atau st.secrets."""
    if explicit_api_key and explicit_api_key.strip():
        return explicit_api_key.strip()

    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key

    try:
        import streamlit as st  # type: ignore

        secret_key = st.secrets.get("GEMINI_API_KEY", "")
        if isinstance(secret_key, str) and secret_key.strip():
            return secret_key.strip()
    except Exception:
        pass

    raise RuntimeError(
        "GEMINI_API_KEY tidak ditemukan. Atur di environment variable atau st.secrets['GEMINI_API_KEY']."
    )


def _unduh_komposit_citra(url_thumbnail: str, timeout_detik: int = 120) -> tuple[bytes, str]:
    """Mengunduh gambar komposit dari url_thumbnail dan menormalkannya ke JPEG/PNG."""
    if not url_thumbnail or not url_thumbnail.strip():
        raise ValueError("url_thumbnail tidak boleh kosong.")

    response = requests.get(
        url_thumbnail.strip(),
        timeout=timeout_detik,
        headers={"User-Agent": "Jatiluhur-KJA-Monitor/1.0"},
    )
    response.raise_for_status()

    raw_bytes = response.content
    if not raw_bytes:
        raise ValueError("Konten gambar dari url_thumbnail kosong.")

    # Verifikasi dan konversi ke JPEG berkualitas tinggi agar kompatibel dengan Gemini multimodal
    with Image.open(BytesIO(raw_bytes)) as img:
        if img.mode in ("RGBA", "LA", "P"):
            rgb_img = img.convert("RGB")
        elif img.mode != "RGB":
            rgb_img = img.convert("RGB")
        else:
            rgb_img = img

        buffer = BytesIO()
        rgb_img.save(buffer, format="JPEG", quality=92)
        return buffer.getvalue(), "image/jpeg"


def _bangun_prompt_validasi(
    metrik: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> str:
    """Menyusun prompt analisis spasial dan visual dengan seluruh konteks metrik dan detail."""
    minggu_id = metrik.get("minggu_id", "-")
    tgl_s1 = metrik.get("tgl_citra_s1", "-")
    tgl_s2 = metrik.get("tgl_citra_s2", "-")
    jumlah_klaster = metrik.get("jumlah_klaster", 0)
    luas_bersih_ha = metrik.get("luas_bersih_ha", 0.0)
    petak_min = metrik.get("estimasi_petak_min", 0)
    petak_tengah = metrik.get("estimasi_petak_tengah", 0)
    petak_maks = metrik.get("estimasi_petak_maks", 0)
    persen_kuota = metrik.get("persen_kuota", 0.0)

    detail_data = detail or {}
    periode = detail_data.get("periode", {})
    t1_win = periode.get("t1", {}).get("s1")
    t2_win = periode.get("t2", {}).get("s1")
    rentang_t1 = f"{t1_win[0]} s.d. {t1_win[1]}" if isinstance(t1_win, (list, tuple)) and len(t1_win) == 2 else "14 hari sebelumnya"
    rentang_t2 = f"{t2_win[0]} s.d. {t2_win[1]}" if isinstance(t2_win, (list, tuple)) and len(t2_win) == 2 else "14 hari terakhir"

    perubahan = detail_data.get("perubahan", {})
    t2_info = detail_data.get("t2", {})
    peringatan = detail_data.get("peringatan", [])

    metrik_tambahan = {
        k: v
        for k, v in metrik.items()
        if k
        not in {
            "minggu_id",
            "tgl_citra_s1",
            "tgl_citra_s2",
            "jumlah_klaster",
            "luas_bersih_ha",
            "estimasi_petak_min",
            "estimasi_petak_tengah",
            "estimasi_petak_maks",
            "persen_kuota",
        }
    }

    blok_detail: list[str] = []
    if perubahan:
        blok_detail.append(
            "Perubahan Spasial T1 ke T2: "
            f"tetap={perubahan.get('luas_tetap_ha', 0)} Ha, "
            f"hilang/bergeser={perubahan.get('luas_hilang_bergeser_ha', 0)} Ha, "
            f"baru={perubahan.get('luas_baru_ha', 0)} Ha, "
            f"selisih_luas={perubahan.get('selisih_luas_ha', 0)} Ha, "
            f"selisih_petak={perubahan.get('selisih_petak_tengah', 0)} petak, "
            f"indikasi_pola=\"{perubahan.get('indikasi_pola', '-')}\"."
        )
    if t2_info:
        blok_detail.append(
            "Kualitas & Penapisan S2 (T2): "
            f"jumlah_citra_s1={t2_info.get('jumlah_citra_s1', '-')}, "
            f"jumlah_citra_s2={t2_info.get('jumlah_citra_s2', '-')}, "
            f"cakupan_s2_bebas_awan={t2_info.get('cakupan_s2_persen', '-')}% , "
            f"luas_eceng_dibuang={t2_info.get('luas_eceng_dibuang_ha', '-')} Ha."
        )
    if peringatan:
        blok_detail.append(f"Peringatan Kualitas Citra dari Mesin Satelit: {'; '.join(map(str, peringatan))}")
    if metrik_tambahan:
        blok_detail.append(f"Metrik tambahan: {json.dumps(metrik_tambahan, ensure_ascii=False)}")

    info_ekstra = ("\n- " + "\n- ".join(blok_detail)) if blok_detail else ""

    return f"""Anda adalah analis penginderaan jauh dan pengawasan sumber daya perairan Waduk Jatiluhur.
Tugas Anda adalah melakukan validasi silang visual terhadap citra satelit komposit area Keramba Jaring Apung (KJA) terbaru (dengan overlay KJA T2 berwarna oranye #FF9100) dan mencocokkannya dengan metrik kuantitatif siklus pemantauan 14 hari berikut:

- ID Periode / Minggu: {minggu_id}
- Rentang Pembandingan: Periode Lalu (T1) = {rentang_t1} vs. Terbaru (T2) = {rentang_t2}
- Tanggal Akuisisi Radar Sentinel-1 Terbaru pada T2 (tgl_citra_s1): {tgl_s1}
- Tanggal Akuisisi Optik Sentinel-2 Terbaru pada T2 (tgl_citra_s2, "-" berarti kosong): {tgl_s2}
- Jumlah Klaster KJA Terdeteksi: {jumlah_klaster} klaster
- Luas Bersih Terkalibrasi: {luas_bersih_ha} Ha
- Estimasi Jumlah Petak (Rentang): {petak_min} s.d. {petak_maks} petak (Nilai Tengah: {petak_tengah} petak)
- Persentase Terhadap Kuota Pemprov Jabar (11.306 petak): {persen_kuota}%
- Referensi Kalibrasi Lapangan 2026: 49.807 petak (249,04 Ha), asumsi luas petak 48-52 m² (tengah 50 m²).{info_ekstra}

Instruksi Penilaian Wajib:
1. status_validasi_ai:
   - Pilih "Valid" jika citra memperlihatkan badan air dan susunan geometris KJA secara cukup bersih tanpa tutupan awan tebal atau hamparan eceng gondok yang menutupi klaster utama.
   - Pilih "Perlu dicek" jika terlihat awan tebal, bayangan awan pekat, haze berat, ledakan eceng gondok (gulma air berwarna hijau terang menempel pada rakit KJA), atau terdapat peringatan citra S2 kosong/parsial yang berisiko menyebabkan eceng gondok tidak tersaring sempurna.
2. analisis_pola:
   - Tulis tepat 2-3 kalimat padat yang menjelaskan interpretasi visual citra, dinamika pergeseran spasial antar-periode (T1 {rentang_t1} ke T2 {rentang_t2}, dengan akuisisi S1 {tgl_s1} dan S2 {tgl_s2}), serta perbandingan langsung estimasi nilai tengah ({petak_tengah} petak) terhadap batas kuota Pemprov Jabar sebesar 11.306 petak ({persen_kuota}%).
3. rekomendasi_pengawasan:
   - Berikan tepat 3 poin rekomendasi aksi lapangan yang konkret, spesifik, dan siap dieksekusi oleh petugas patroli Waduk Jatiluhur (misalnya verifikasi zona padat, pengecekan gulma/awan, atau penertiban klaster baru).
"""


def validasi_kja_dengan_ai(
    url_thumbnail: str,
    metrik: dict[str, Any],
    api_key: str | None = None,
    max_retries: int = 3,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Mengunduh gambar komposit satelit dari url_thumbnail dan mengirimkannya bersama
    angka metrik (serta detail opsional dari gee_engine.py) ke Gemini API (gemini-2.5-flash)
    menggunakan Structured Outputs.

    Returns:
        dict dengan tepat 3 key:
        - status_validasi_ai: "Valid" | "Perlu dicek"
        - analisis_pola: str (2-3 kalimat)
        - rekomendasi_pengawasan: list[str] (3 poin tindakan)
    """
    resolved_key = _resolve_gemini_api_key(api_key)
    image_bytes, mime_type = _unduh_komposit_citra(url_thumbnail)
    prompt = _bangun_prompt_validasi(metrik, detail=detail)

    client = genai.Client(api_key=resolved_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ValidasiKJASchema,
        temperature=0.2,
    )

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[image_part, prompt],
                config=config,
            )

            parsed: ValidasiKJASchema | None = getattr(response, "parsed", None)
            if parsed is not None:
                hasil = parsed.model_dump()
            else:
                raw_text = (response.text or "").strip()
                if not raw_text:
                    raise ValueError("Respons Gemini API kosong.")
                validated = ValidasiKJASchema.model_validate_json(raw_text)
                hasil = validated.model_dump()

            # Pastikan kepatuhan format akhir
            status = hasil["status_validasi_ai"]
            if status not in ("Valid", "Perlu dicek"):
                status = "Perlu dicek"

            rekomendasi = [str(item).strip() for item in hasil["rekomendasi_pengawasan"] if str(item).strip()]
            while len(rekomendasi) < 3:
                rekomendasi.append(
                    "Lakukan patroli verifikasi langsung pada klaster KJA berkepadatan tinggi di lapangan."
                )

            return {
                "status_validasi_ai": status,
                "analisis_pola": str(hasil["analisis_pola"]).strip(),
                "rekomendasi_pengawasan": rekomendasi[:3],
            }
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(1.5 * (2**attempt))

    raise RuntimeError(f"Gagal melakukan validasi AI setelah {max_retries} percobaan: {last_error}")
