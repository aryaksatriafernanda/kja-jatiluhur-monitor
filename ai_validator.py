"""
Fase 3 — Modul Validasi Multimodal KJA Waduk Jatiluhur
Menggunakan SDK resmi terbaru google-genai dengan model default gemini-3.8-flash
dan Structured Outputs (Pydantic JSON Schema).
"""

from __future__ import annotations

import json
import os
import random
import time
from io import BytesIO
from typing import Any, Literal

import requests
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field

MODEL_GEMINI = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
MODEL_CADANGAN = [
    m.strip()
    for m in os.environ.get("GEMINI_MODEL_CADANGAN", "gemini-4-hydrogen,gemini-2.5-flash").split(",")
    if m.strip()
]


SYSTEM_INSTRUCTION_KJA = """Anda adalah analis senior penginderaan jauh dan pengawasan sumber daya perairan Waduk Jatiluhur, Jawa Barat.

Konteks Hidrometeorologi & Operasional Waduk Jatiluhur yang WAJIB Anda pahami:
1. Musim Hujan Puncak (November hingga Maret): Waduk Jatiluhur mengalami intensitas angin kencang, riak gelombang permukaan air yang kasar, serta tutupan awan tebal dan bayangan awan. Kondisi ini kerap memicu lonjakan hamburan balik radar Sentinel-1 sementara (overestimation luasan deteksi di area perairan terbuka) meskipun jumlah petak KJA fisik tidak bertambah.
2. Sisa Pelampung Pasca-Penertiban: Pasca operasi penertiban/pembersihan KJA, sering terdapat sisa pelampung drum kosong, bambu, atau kerangka rakit tanpa jaring yang masih mengapung sementara dan memantulkan sinyal radar.
3. Aturan Anomali Perubahan Luas > 12%: Apabila metrik menunjukkan lonjakan perubahan luas antar-periode melebihi 12% dalam satu siklus, Anda WAJIB mengevaluasi secara kritis apakah penambahan tersebut terlihat sebagai klaster geometris petak KJA fisik yang nyata dan terstruktur rapi, ATAU sekadar indikasi gangguan cuaca musim hujan (riak gelombang/angin/awan) maupun sisa pelampung kosong. Jika terindikasi gangguan cuaca, awan tebal, tutupan eceng gondok, atau lonjakan > 12% tidak terkonfirmasi sebagai petak fisik baru yang jelas, wajib tetapkan status_validasi_ai = "Perlu dicek"."""


class CitraTidakTersedia(RuntimeError):
    """Gagal mengunduh komposit citra dari Earth Engine."""


class ValidasiKJASchema(BaseModel):
    """Skema JSON pasti untuk hasil validasi multimodal KJA oleh Gemini."""

    status_validasi_ai: Literal["Valid", "Perlu dicek"] = Field(
        description=(
            'Wajib bernilai "Valid" apabila citra menunjukkan susunan geometris petak KJA '
            'yang jelas tanpa indikasi gangguan cuaca musim hujan maupun lonjakan anomali, '
            'atau "Perlu dicek" apabila terdapat gangguan awan tebal, kabut pekat, '
            "riak gelombang angin kencang musim hujan (November-Maret), tutupan eceng gondok, "
            "atau lonjakan luas > 12% yang terindikasi sebagai overestimation sementara."
        )
    )
    analisis_pola: str = Field(
        description=(
            "Ringkasan 2 sampai 3 kalimat dalam Bahasa Indonesia formal mengenai "
            "pergeseran spasial antar-periode T1 ke T2, evaluasi apakah ada indikasi "
            "gangguan musim hujan/pelampung sisa jika terjadi lonjakan > 12%, serta "
            "perbandingan estimasi jumlah petak terhadap batas kuota Pemprov Jawa Barat "
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


def _ringkas_error_gee(response: requests.Response) -> str:
    """Mengambil pesan error asli dari respons GEE tanpa menelannya."""
    try:
        pesan = response.json().get("error", {}).get("message") or response.text
    except Exception:
        pesan = response.text
    return (pesan or "").strip()[:500]


def _unduh_komposit_citra(
    url_thumbnail: str,
    timeout_koneksi: int = 15,
    timeout_baca: int = 240,
    max_retries: int = 4,
) -> tuple[bytes, str]:
    """Mengunduh gambar komposit dari url_thumbnail (dengan retry) dan menormalkannya ke JPEG."""
    if not url_thumbnail or not url_thumbnail.strip():
        raise ValueError("url_thumbnail tidak boleh kosong.")

    url = url_thumbnail.strip()
    raw_bytes: bytes | None = None
    last_error = "tidak diketahui"

    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                timeout=(timeout_koneksi, timeout_baca),
                headers={"User-Agent": "Jatiluhur-KJA-Monitor/1.0"},
            )
            if response.ok and response.content:
                raw_bytes = response.content
                break

            detail = _ringkas_error_gee(response)
            last_error = f"HTTP {response.status_code}: {detail}"
            gagal_komputasi = response.status_code == 400 and any(
                k in detail.lower() for k in ("timed out", "timeout", "computation", "memory")
            )
            bisa_diulang = response.status_code in (429, 500, 502, 503, 504) or gagal_komputasi
            if not bisa_diulang:
                break
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < max_retries - 1:
            time.sleep(2.5 * (2**attempt))

    if not raw_bytes:
        raise CitraTidakTersedia(f"Gagal mengunduh citra komposit GEE. {last_error}")

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            rgb_img = img if img.mode == "RGB" else img.convert("RGB")
            buffer = BytesIO()
            rgb_img.save(buffer, format="JPEG", quality=92)
            return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        raise CitraTidakTersedia(f"Respons GEE bukan gambar valid: {exc}") from exc


def _bangun_prompt_validasi(
    metrik: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> str:
    """Menyusun prompt analisis spasial dan visual dengan konteks metrik, detail, & musim hujan."""
    minggu_id = metrik.get("minggu_id", "-")
    tgl_s1 = metrik.get("tgl_citra_s1", "-")
    tgl_s2 = metrik.get("tgl_citra_s2", "-")
    jumlah_klaster = metrik.get("jumlah_klaster", 0)
    luas_bersih_ha = float(metrik.get("luas_bersih_ha", 0.0) or 0.0)
    petak_min = metrik.get("estimasi_petak_min", 0)
    petak_tengah = metrik.get("estimasi_petak_tengah", 0)
    petak_maks = metrik.get("estimasi_petak_maks", 0)
    persen_kuota = metrik.get("persen_kuota", 0.0)

    detail_data = detail or {}
    periode = detail_data.get("periode", {})
    t1_win = periode.get("t1", {}).get("s1")
    t2_win = periode.get("t2", {}).get("s1")
    rentang_t1 = (
        f"{t1_win[0]} s.d. {t1_win[1]}"
        if isinstance(t1_win, (list, tuple)) and len(t1_win) == 2
        else "periode pembanding sebelumnya"
    )
    rentang_t2 = (
        f"{t2_win[0]} s.d. {t2_win[1]}"
        if isinstance(t2_win, (list, tuple)) and len(t2_win) == 2
        else "periode pengamatan terbaru"
    )

    perubahan = detail_data.get("perubahan", {})
    t1_info = detail_data.get("t1", {})
    t2_info = detail_data.get("t2", {})
    peringatan = detail_data.get("peringatan", [])

    # Hitung persentase perubahan luas antar-periode untuk konteks evaluasi anomali > 12%
    persen_perubahan_luas: float | None = None
    try:
        luas_t1_raw = t1_info.get("luas_bersih_ha")
        if luas_t1_raw is not None and float(luas_t1_raw) > 0:
            luas_t1_val = float(luas_t1_raw)
            luas_t2_val = float(t2_info.get("luas_bersih_ha", luas_bersih_ha))
            persen_perubahan_luas = round(((luas_t2_val - luas_t1_val) / luas_t1_val) * 100.0, 2)
        elif metrik.get("delta_luas_ha") is not None:
            delta_val = float(metrik.get("delta_luas_ha"))
            luas_prev = luas_bersih_ha - delta_val
            if luas_prev > 0:
                persen_perubahan_luas = round((delta_val / luas_prev) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        persen_perubahan_luas = None

    # Deteksi indikasi periode puncak musim hujan (bulan November s.d. Maret)
    musim_hujan_aktif = False
    for tgl_kandidat in (str(tgl_s1), str(metrik.get("t2_akhir", ""))):
        if len(tgl_kandidat) >= 7 and tgl_kandidat[4] == "-":
            try:
                bulan = int(tgl_kandidat[5:7])
                if bulan in (11, 12, 1, 2, 3):
                    musim_hujan_aktif = True
                    break
            except ValueError:
                pass

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
    if persen_perubahan_luas is not None:
        blok_detail.append(
            f"Persentase Perubahan Luas Antar-Periode (T1 -> T2): {persen_perubahan_luas:+.2f}% "
            "(Ambang kewajaran fisik satu siklus: ±12%)."
        )
    if t2_info:
        blok_detail.append(
            "Kualitas & Penapisan S2 (T2): "
            f"jumlah_citra_s1={t2_info.get('jumlah_citra_s1', '-')}, "
            f"jumlah_citra_s2={t2_info.get('jumlah_citra_s2', '-')}, "
            f"cakupan_s2_bebas_awan={t2_info.get('cakupan_s2_persen', '-')}%, "
            f"luas_eceng_dibuang={t2_info.get('luas_eceng_dibuang_ha', '-')} Ha."
        )
    if peringatan:
        blok_detail.append(
            f"Peringatan Kualitas Citra dari Mesin Satelit: {'; '.join(map(str, peringatan))}"
        )
    if metrik_tambahan:
        blok_detail.append(f"Metrik tambahan: {json.dumps(metrik_tambahan, ensure_ascii=False)}")

    info_ekstra = ("\n- " + "\n- ".join(blok_detail)) if blok_detail else ""
    catatan_musim = (
        "YA (Periode berada pada puncak musim hujan November–Maret: waspadai riak gelombang angin kencang, awan tebal, dan sisa pelampung kosong pasca-penertiban)"
        if musim_hujan_aktif
        else "Di luar puncak musim hujan Nov–Mar (tetap periksa sisa pelampung mengapung atau gangguan awan/eceng gondok)"
    )

    return f"""Evaluasi citra satelit komposit area Waduk Jatiluhur (lapisan radar Sentinel-1 + optik RGB Sentinel-2 + garis batas waduk putih + overlay deteksi KJA T2 oranye #FF9100) bersama metrik kuantitatif berikut:

- ID Periode / Minggu: {minggu_id}
- Rentang Pembandingan: Periode Lalu (T1) = {rentang_t1} vs. Terbaru (T2) = {rentang_t2}
- Tanggal Akuisisi Radar Sentinel-1 Terbaru pada T2 (tgl_citra_s1): {tgl_s1}
- Tanggal Akuisisi Optik Sentinel-2 Terbaru pada T2 (tgl_citra_s2, "-" berarti kosong): {tgl_s2}
- Status Musim Hidrometeorologi: {catatan_musim}
- Jumlah Klaster KJA Terdeteksi: {jumlah_klaster} klaster
- Luas Bersih Terkalibrasi: {luas_bersih_ha} Ha
- Estimasi Jumlah Petak (Rentang): {petak_min} s.d. {petak_maks} petak (Nilai Tengah: {petak_tengah} petak)
- Persentase Terhadap Kuota Pemprov Jabar (11.306 petak): {persen_kuota}%
- Referensi Kalibrasi Lapangan 2026: 49.807 petak (249,04 Ha), asumsi luas petak 48-52 m² (tengah 50 m²).{info_ekstra}

Instruksi Penilaian Wajib:
1. status_validasi_ai:
   - Pilih "Valid" jika citra memperlihatkan badan air dan susunan geometris petak KJA secara bersih, terstruktur, dan bebas gangguan awan tebal, eceng gondok masif, maupun lonjakan anomali cuaca (> 12%).
   - Pilih "Perlu dicek" jika terlihat awan tebal/bayangan awan pekat, haze berat, hamparan eceng gondok menempel pada KJA, peringatan cakupan S2 parsial/kosong, ATAU apabila terjadi lonjakan perubahan luas > 12% dalam satu siklus yang terindikasi sebagai gangguan riak gelombang/angin puncak musim hujan (November–Maret) maupun sisa pelampung kosong pasca-penertiban.
2. analisis_pola:
   - Tulis tepat 2-3 kalimat padat yang menjelaskan interpretasi visual citra, dinamika pergeseran spasial antar-periode (T1 {rentang_t1} ke T2 {rentang_t2}, dengan akuisisi S1 {tgl_s1} dan S2 {tgl_s2}), penilaian apakah perubahan (khususnya bila > 12%) merupakan penambahan petak fisik nyata atau gangguan gelombang musim hujan/sisa pelampung mengapung, serta perbandingan langsung estimasi nilai tengah ({petak_tengah} petak) terhadap batas kuota Pemprov Jabar sebesar 11.306 petak ({persen_kuota}%).
3. rekomendasi_pengawasan:
   - Berikan tepat 3 poin rekomendasi aksi lapangan yang konkret, spesifik, dan siap dieksekusi oleh petugas patroli Waduk Jatiluhur (termasuk verifikasi sisa pelampung kosong pasca-penertiban atau konfirmasi visual saat indikasi gangguan cuaca musim hujan).
"""


def validasi_kja_dengan_ai(
    url_thumbnail: str,
    metrik: dict[str, Any],
    api_key: str | None = None,
    max_retries: int = 3,
    detail: dict[str, Any] | None = None,
    izinkan_tanpa_citra: bool = False,
) -> dict[str, Any]:
    """
    Mengunduh komposit satelit dari url_thumbnail (radar S1 + optik S2 + KJA oranye)
    dan mengirimkannya ke Gemini API (default gemini-3.8-flash) dengan Structured Outputs
    serta system instruction kontekstual musim hujan Waduk Jatiluhur (Nov-Mar).
    Mengangkat error asli secara eksplisit apabila unduhan citra atau panggilan Gemini gagal.
    """
    resolved_key = _resolve_gemini_api_key(api_key)
    prompt = _bangun_prompt_validasi(metrik, detail=detail)

    image_part = None
    try:
        image_bytes, mime_type = _unduh_komposit_citra(url_thumbnail)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    except CitraTidakTersedia:
        if not izinkan_tanpa_citra:
            raise
        prompt += (
            "\n\nCATATAN PENTING: citra komposit TIDAK dapat diunduh sehingga tidak ada "
            "gambar yang dilampirkan. Analisis hanya berdasarkan metrik di atas, "
            'wajib pilih status_validasi_ai = "Perlu dicek", dan salah satu rekomendasi '
            "harus berupa verifikasi visual ulang secara manual."
        )

    client = genai.Client(api_key=resolved_key)
    contents = [image_part, prompt] if image_part is not None else [prompt]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION_KJA,
        response_mime_type="application/json",
        response_schema=ValidasiKJASchema,
        temperature=0.2,
    )

    daftar_model = [MODEL_GEMINI, *[m for m in MODEL_CADANGAN if m != MODEL_GEMINI]]
    last_error: Exception | None = None
    model_terakhir = MODEL_GEMINI

    for model_dipakai in daftar_model:
        model_terakhir = model_dipakai
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_dipakai,
                    contents=contents,
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

                status = hasil["status_validasi_ai"]
                if status not in ("Valid", "Perlu dicek"):
                    status = "Perlu dicek"
                if image_part is None:
                    status = "Perlu dicek"

                rekomendasi = [
                    str(item).strip()
                    for item in hasil["rekomendasi_pengawasan"]
                    if str(item).strip()
                ]
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
                kode = getattr(exc, "code", None)
                if kode in (400, 401, 403):
                    raise RuntimeError(
                        f"Gagal melakukan validasi AI (model={model_dipakai}): {exc}"
                    ) from exc
                if kode == 404:
                    break  # model tidak tersedia di API key ini; langsung coba model cadangan berikutnya
                if attempt < max_retries - 1:
                    time.sleep(2.0 * (2**attempt) + random.uniform(0, 1.5))

    raise RuntimeError(
        f"Gagal melakukan validasi AI (model terakhir={model_terakhir}) setelah mencoba {daftar_model}: {last_error}"
    )
