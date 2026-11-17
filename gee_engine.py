# -*- coding: utf-8 -*-
"""
gee_engine.py - Mesin analisis KJA Waduk Jatiluhur (Fase 2, v2)

Alur: batas air (JRC GSW) -> topeng air (susut pantai) -> deteksi KJA (Sentinel-1, 14 hari)
-> buang eceng gondok (Sentinel-2, 45 hari) -> metrik T1 & T2 -> perubahan piksel
-> url_thumbnail untuk Gemini + layers_peta untuk Folium.
Kebutuhan: pip install earthengine-api folium
"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import ee

# ============================== KONSTANTA ==============================
PUSAT_WADUK = (107.38, -6.54)             # (lon, lat) perkiraan tengah waduk
KONFIG_DEFAULT = {                         # cermin tab `konfigurasi` di Google Sheets
    "kuota_petak_pemprov": 11306,
    "luas_petak_min_m2": 48.0,             # -> estimasi_petak_maks
    "luas_petak_tengah_m2": 50.0,          # -> estimasi_petak_tengah
    "luas_petak_maks_m2": 52.0,            # -> estimasi_petak_min
}
SKALA = 10                                 # meter
CRS = "EPSG:32748"                         # WGS84 / UTM zona 48S
LUAS_PIKSEL_M2 = SKALA ** 2

WARNA = {"t1": "00E5FF", "t2": "FF9100", "tetap": "FFD600", "bergeser": "D50000", "baru": "00C853"}
VIS_RGB = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 2500, "gamma": 1.3}
ESRI_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
LABEL_LAYER = {"tetap": "Tetap (kuning)", "bergeser": "Hilang/bergeser (merah)", "baru": "Baru (hijau)",
               "kja_t1": "KJA T1 (cyan)", "kja_t2": "KJA T2 (oranye)",
               "s2_t1": "Sentinel-2 RGB T1", "s2_t2": "Sentinel-2 RGB T2"}

_CACHE = {}
_EE_SIAP = False


# ============================ 1. AUTENTIKASI ============================
def inisialisasi_ee(kunci=None):
    """kunci: path credentials.json | dict (mis. dict(st.secrets['gee'])) | string JSON.
    Default: env GEE_CREDENTIALS atau 'credentials.json' di folder kerja."""
    global _EE_SIAP
    if _EE_SIAP:
        return
    if kunci is None:
        kunci = os.environ.get("GEE_CREDENTIALS", "credentials.json")
    if isinstance(kunci, dict):
        data = dict(kunci)
    elif isinstance(kunci, str) and kunci.lstrip().startswith("{"):
        data = json.loads(kunci)
    else:
        with open(kunci, encoding="utf-8") as f:
            data = json.load(f)
    kred = ee.ServiceAccountCredentials(data["client_email"], key_data=json.dumps(data))
    ee.Initialize(kred, project=data["project_id"])
    _EE_SIAP = True


# ============================ 2. KONFIGURASI PEMBAGI PETAK ============================
def _angka(v):
    """Angka dari Sheets: 50, '50,0', '48.5', '11.306', '1.234,5'."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


def baca_konfigurasi(konfigurasi=None):
    """konfigurasi: None | dict {parameter: nilai} | list-of-records dari gspread get_all_records()
    (kolom 'parameter' dan 'nilai'). Nilai yang tidak dikirim memakai KONFIG_DEFAULT."""
    cfg = dict(KONFIG_DEFAULT)
    if konfigurasi:
        if not isinstance(konfigurasi, dict):
            konfigurasi = {str(r.get("parameter", "")).strip(): r.get("nilai") for r in konfigurasi}
        for k in cfg:
            v = konfigurasi.get(k)
            if v not in (None, ""):
                cfg[k] = _angka(v)
    cfg["kuota_petak_pemprov"] = int(round(cfg["kuota_petak_pemprov"]))
    if (cfg["kuota_petak_pemprov"] <= 0 or
            not 0 < cfg["luas_petak_min_m2"] <= cfg["luas_petak_tengah_m2"] <= cfg["luas_petak_maks_m2"]):
        raise ValueError(f"Konfigurasi tidak valid (harus 0 < min <= tengah <= maks, kuota > 0): {cfg}")
    return cfg


def _estimasi(luas_m2, cfg):
    tengah = luas_m2 / cfg["luas_petak_tengah_m2"]
    return {"estimasi_petak_min": int(round(luas_m2 / cfg["luas_petak_maks_m2"])),
            "estimasi_petak_tengah": int(round(tengah)),
            "estimasi_petak_maks": int(round(luas_m2 / cfg["luas_petak_min_m2"])),
            "persen_kuota": round(tengah / cfg["kuota_petak_pemprov"] * 100, 1)}


# ========================= 3. BATAS & TOPENG BADAN AIR =========================
def dapatkan_batas_waduk(ambang_occurrence=75, radius_penutupan_m=150,
                         radius_cari_m=6000, luas_min_km2=1.0, paksa_ulang=False):
    """Batas air Jatiluhur dari JRC GSW (occurrence > ambang). Di-cache per proses.
    Return: (geometri_batas, kotak_pembatas, info)."""
    kunci = (ambang_occurrence, radius_penutupan_m, radius_cari_m, luas_min_km2)
    if kunci in _CACHE and not paksa_ulang:
        return _CACHE[kunci]
    inisialisasi_ee()
    pusat = ee.Geometry.Point(list(PUSAT_WADUK))
    aoi = pusat.buffer(20000).bounds()
    occ = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0)
    air = occ.gt(ambang_occurrence)
    # Penutupan morfologi: menambal "lubang" di zona KJA padat yang occurrence-nya rendah
    tutup = (air.focalMax(radius_penutupan_m, "circle", "meters")
                .focalMin(radius_penutupan_m, "circle", "meters"))
    vek = tutup.selfMask().reduceToVectors(
        reducer=ee.Reducer.countEvery(), geometry=aoi, crs=CRS, scale=30,
        geometryType="polygon", eightConnected=True, maxPixels=1e10, tileScale=4)

    def tambah_luas(f):
        f = ee.Feature(f)
        return f.set("luas_km2", f.geometry().area(100).divide(1e6))

    kandidat = (vek.map(tambah_luas)
                .filterBounds(pusat.buffer(radius_cari_m))
                .filter(ee.Filter.gte("luas_km2", luas_min_km2)))
    if kandidat.size().getInfo() == 0:
        raise RuntimeError("Badan air tidak ditemukan di sekitar PUSAT_WADUK. "
                           "Cek koordinat, ambang_occurrence, atau radius_cari_m.")
    gabungan = kandidat.geometry(100).dissolve(100).simplify(20)
    batas = ee.Geometry(gabungan.getInfo(), None, False)
    kotak = ee.Geometry(gabungan.bounds().getInfo(), None, False)
    info = {"luas_air_ha": round(gabungan.area(100).getInfo() / 1e4, 1),
            "ambang_occurrence": ambang_occurrence, "radius_penutupan_m": radius_penutupan_m,
            "pusat_lon_lat": list(PUSAT_WADUK)}
    _CACHE[kunci] = (batas, kotak, info)
    return _CACHE[kunci]


def ambil_bbox_waduk():
    """(lat_min, lon_min, lat_maks, lon_maks) untuk fit_bounds Folium."""
    _, kotak, _ = dapatkan_batas_waduk()
    cincin = kotak.getInfo()["coordinates"][0]
    lon, lat = [c[0] for c in cincin], [c[1] for c in cincin]
    return min(lat), min(lon), max(lat), max(lon)


def _topeng_air(batas, buffer_pantai_m):
    """Rasterisasi batas pada grid 10 m lalu susutkan dari bibir pantai (focalMin).
    Dilakukan di grid 10 m, bukan 30 m JRC, karena radius 15 m < 1 piksel JRC (tidak berefek)."""
    dasar = (ee.Image(0).paint(ee.FeatureCollection([ee.Feature(batas)]), 1)
             .reproject(crs=CRS, scale=SKALA))
    if buffer_pantai_m and buffer_pantai_m > 0:
        dasar = dasar.focalMin(buffer_pantai_m, "circle", "meters")
    return dasar.selfMask().rename("air")


# ============================ 4. TANGGAL, KOLEKSI, KOMPOSIT ============================
def _ke_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


def _periode(akhir, hari_s1, hari_s2):
    """Jendela mundur dari tanggal akhir (inklusif): S1 pendek, S2 panjang."""
    return {"akhir": akhir.isoformat(),
            "s1": ((akhir - timedelta(days=hari_s1 - 1)).isoformat(), akhir.isoformat()),
            "s2": ((akhir - timedelta(days=hari_s2 - 1)).isoformat(), akhir.isoformat())}


def _rentang(awal, akhir):
    return ee.Date(awal), ee.Date(akhir).advance(1, "day")      # akhir inklusif


def _tgl_terbaru(koleksi):
    mx = koleksi.aggregate_max("system:time_start")
    return ee.Algorithms.If(koleksi.size().gt(0), ee.Date(mx).format("YYYY-MM-dd"), "-")


def _koleksi_s1(kotak, d0, d1, orbit):
    k = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(kotak).filterDate(d0, d1)
         .filter(ee.Filter.eq("instrumentMode", "IW"))
         .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
         .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH")))
    if orbit:   # "ASCENDING" / "DESCENDING" / None (semua)
        k = k.filter(ee.Filter.eq("orbitProperties_pass", orbit))
    return k.select(["VV", "VH"])


def _koleksi_s2(kotak, d0, d1):
    s2 = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(kotak).filterDate(d0, d1)
    cs = (ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED")
          .filterBounds(kotak).filterDate(d0, d1))
    return s2.linkCollection(cs, ["cs_cdf"])


def _komposit_s1(koleksi, radius_median_m):
    return (koleksi.median().reproject(crs=CRS, scale=SKALA)
            .focalMedian(radius_median_m, "circle", "meters"))


def _komposit_s2(koleksi, cs_min, n_citra):
    if n_citra == 0:   # tidak ada citra: kembalikan citra tertopeng penuh
        return (ee.Image.constant([0, 0, 0, 0]).rename(["B2", "B3", "B4", "B8"])
                .updateMask(ee.Image.constant(0)))
    k = (koleksi.map(lambda im: im.updateMask(im.select("cs_cdf").gte(cs_min)))
         .select(["B2", "B3", "B4", "B8"]))
    return k.median().reproject(crs=CRS, scale=SKALA)


# ============================ 5. DETEKSI KJA ============================
def _deteksi(s1, s2, air_img, p):
    vv = s1.select("VV").gt(p["ambang_vv_db"])
    vh = s1.select("VH").gt(p["ambang_vh_db"])
    kuat = (vv.Or(vh) if p["aturan_s1"] == "atau" else vv.And(vh)).rename("kja")
    mentah = kuat.updateMask(air_img).selfMask()

    # Sentinel-2: piksel dipertahankan jika NDVI < ambang; selebihnya = vegetasi/eceng gondok
    ndvi = s2.normalizedDifference(["B8", "B4"])
    veg = ndvi.gte(p["ambang_ndvi"])
    if p["ambang_ndwi_min"] is not None:        # opsional: NDWI sangat negatif juga dianggap vegetasi
        veg = veg.Or(s2.normalizedDifference(["B3", "B8"]).lt(p["ambang_ndwi_min"]))
    eceng = veg.unmask(0).rename("eceng")
    s2_valid = s2.select("B8").mask().unmask(0).rename("s2_valid")

    bersih0 = mentah.updateMask(eceng.Not()).selfMask()
    ukuran = bersih0.connectedPixelCount(128, True)             # buang bercak kecil/noise
    bersih = bersih0.updateMask(ukuran.gte(p["min_piksel_klaster"])).selfMask()
    return {"mentah": mentah, "bersih": bersih, "eceng": eceng, "s2_valid": s2_valid}


# ============================ 6. METRIK PER PERIODE ============================
def _ringkas_klaster(bersih, batas, metode, maks_detail):
    vek = bersih.reduceToVectors(
        reducer=ee.Reducer.countEvery(), geometry=batas, crs=CRS, scale=SKALA,
        geometryType="polygon", eightConnected=True, maxPixels=1e10, tileScale=4)

    def fitur(f):
        f = ee.Feature(f)
        c = f.geometry().centroid(10).coordinates()
        return ee.Feature(None, {"lon": c.get(0), "lat": c.get(1),
                                 "luas_m2": ee.Number(f.get("count")).multiply(LUAS_PIKSEL_M2)})

    teratas = vek.map(fitur).sort("luas_m2", False).limit(maks_detail)
    ringkas = {"n_vek": vek.size(),
               "detail": teratas.toList(maks_detail).map(lambda f: ee.Feature(f).toDictionary())}
    if metode == "connectedComponents":
        lab = bersih.connectedComponents(ee.Kernel.square(1), 256)
        ringkas["n_cc"] = (lab.select("labels").reduceRegion(
            ee.Reducer.countDistinct(), batas, SKALA, crs=CRS, maxPixels=1e10, tileScale=4)
            .get("labels"))
    return ee.Dictionary(ringkas)


def _metrik(det, batas, air_img, n_s1, n_s2, cfg, metode, maks_detail):
    luas = ee.Image.pixelArea()
    susun = ee.Image.cat([
        luas.updateMask(air_img).rename("air"),
        luas.updateMask(det["mentah"]).rename("mentah"),
        luas.updateMask(det["mentah"].updateMask(det["eceng"])).rename("eceng"),
        luas.updateMask(det["bersih"]).rename("bersih"),
        luas.updateMask(air_img).updateMask(det["s2_valid"]).rename("s2_valid"),
    ])
    paket = ee.Dictionary({
        "luas": susun.reduceRegion(ee.Reducer.sum(), batas, SKALA, crs=CRS,
                                   maxPixels=1e10, tileScale=4),
        "klaster": _ringkas_klaster(det["bersih"], batas, metode, maks_detail),
    }).getInfo()

    L = {k: (paket["luas"].get(k) or 0.0) for k in ("air", "mentah", "eceng", "bersih", "s2_valid")}
    kl = paket["klaster"]
    n_klaster = int(kl["n_cc"]) if kl.get("n_cc") is not None else int(kl["n_vek"])
    m = {"jumlah_citra_s1": n_s1, "jumlah_citra_s2": n_s2,
         "cakupan_s2_persen": round(L["s2_valid"] / L["air"] * 100, 1) if L["air"] else 0.0,
         "luas_air_efektif_ha": round(L["air"] / 1e4, 1),
         "luas_mentah_ha": round(L["mentah"] / 1e4, 2),          # hanya filter S1
         "luas_eceng_dibuang_ha": round(L["eceng"] / 1e4, 2),    # dibuang oleh S2
         "luas_bersih_ha": round(L["bersih"] / 1e4, 2),          # hasil akhir
         "luas_bersih_m2": round(L["bersih"]),
         "jumlah_klaster": n_klaster, "metode_klaster": metode}
    if kl.get("n_cc") is not None:
        m["jumlah_klaster_vektor"] = int(kl["n_vek"])
    est = _estimasi(L["bersih"], cfg)
    m.update(est)
    m["selisih_kuota_petak"] = est["estimasi_petak_tengah"] - cfg["kuota_petak_pemprov"]
    m["status_kuota"] = "MELEBIHI KUOTA" if m["selisih_kuota_petak"] > 0 else "DI BAWAH KUOTA"
    m["detail_klaster"] = [{"id": i + 1, "lon": round(d["lon"], 6), "lat": round(d["lat"], 6),
                            "luas_m2": round(d["luas_m2"]),
                            "estimasi_petak": int(round(d["luas_m2"] / cfg["luas_petak_tengah_m2"]))}
                           for i, d in enumerate(kl["detail"])]
    return m


# ============================ 7. PERUBAHAN T1 vs T2 ============================
def _indikasi_pola(hilang_ha, baru_ha, luas1_ha):
    u = max(0.05 * luas1_ha, 1.0)    # ambang "bermakna": 5% luas T1 atau minimal 1 ha
    if hilang_ha >= u and baru_ha >= u:
        if min(hilang_ha, baru_ha) / max(hilang_ha, baru_ha) >= 0.5:
            return "Indikasi pergeseran/relokasi (luas hilang dan luas baru sebanding)"
        return ("Indikasi penambahan dominan disertai pergeseran" if baru_ha > hilang_ha
                else "Indikasi pengurangan dominan disertai pergeseran")
    if baru_ha >= u:
        return "Indikasi penambahan klaster/petak baru"
    if hilang_ha >= u:
        return "Indikasi pengurangan/penarikan KJA"
    return "Relatif stabil"


def _perubahan(b1, b2, air_img, batas, tol_m, luas1_ha, luas2_ha, cfg):
    tol = max(tol_m, SKALA)                       # toleransi geser/jitter antar-komposit
    k1, k2 = b1.unmask(0), b2.unmask(0)
    k1d = k1.focalMax(tol, "circle", "meters")
    k2d = k2.focalMax(tol, "circle", "meters")
    imgs = {"tetap": k2.And(k1d).updateMask(air_img).selfMask(),               # ada di T2 & sudah ada di T1
            "bergeser": k1.And(k2d.Not()).updateMask(air_img).selfMask(),      # ada di T1, tak ada di T2
            "baru": k2.And(k1d.Not()).updateMask(air_img).selfMask()}          # ada di T2, tak ada di T1

    luas = ee.Image.pixelArea()
    susun = ee.Image.cat([luas.updateMask(im).rename(k) for k, im in imgs.items()])
    r = susun.reduceRegion(ee.Reducer.sum(), batas, SKALA, crs=CRS,
                           maxPixels=1e10, tileScale=4).getInfo()
    m2 = {k: (r.get(k) or 0.0) for k in imgs}
    ha = {k: round(v / 1e4, 2) for k, v in m2.items()}
    out = {"piksel_tetap": round(m2["tetap"] / LUAS_PIKSEL_M2), "luas_tetap_ha": ha["tetap"],
           "piksel_hilang_bergeser": round(m2["bergeser"] / LUAS_PIKSEL_M2),
           "luas_hilang_bergeser_ha": ha["bergeser"],
           "piksel_baru": round(m2["baru"] / LUAS_PIKSEL_M2), "luas_baru_ha": ha["baru"],
           "selisih_luas_ha": round(luas2_ha - luas1_ha, 2),
           "selisih_petak_tengah": int(round((luas2_ha - luas1_ha) * 1e4 / cfg["luas_petak_tengah_m2"])),
           "persen_hilang_dari_t1": round(ha["bergeser"] / luas1_ha * 100, 1) if luas1_ha else 0.0,
           "persen_baru_dari_t2": round(ha["baru"] / luas2_ha * 100, 1) if luas2_ha else 0.0,
           "toleransi_geser_m": tol,
           "indikasi_pola": _indikasi_pola(ha["bergeser"], ha["baru"], luas1_ha)}
    return out, imgs


# ============================ 8. THUMBNAIL & LAYER PETA ============================
def _thumbnail(s1, s2, det, imgs, batas, wilayah, dimensi, lengkap):
    garis = (ee.Image().byte().paint(ee.FeatureCollection([ee.Feature(batas)]), 1, 1)
             .visualize(min=0, max=1, palette=["FFFFFF", "FFFFFF"]))

    # Pastikan wilayah dikonversi ke koordinat GeoJSON murni agar tidak ditolak getThumbURL
    region_coords = wilayah.bounds().getInfo()['coordinates'] if hasattr(wilayah, 'bounds') else wilayah

    def latar(i):
        return s1[i].select(["VV"]).visualize(min=-25, max=0, palette=["0f172a", "334155", "94a3b8"])

    def biner(img, warna):
        return img.selfMask().visualize(palette=[warna])

    def url(citra):
        return citra.clip(wilayah).getThumbURL({
            "region": region_coords,
            "dimensions": 512,
            "format": "png"
        })

    f2 = latar(1)
    out = {"kja_t2": url(f2.blend(biner(det[1]["bersih"], WARNA["t2"])))}   # untuk Gemini
    if lengkap:
        out["latar_t2"] = url(f2)
        out["kja_t1"] = url(latar(0).blend(biner(det[0]["bersih"], WARNA["t1"])))
        perub = f2
        for k in ("tetap", "bergeser", "baru"):
            perub = perub.blend(biner(imgs[k], WARNA[k]))
        out["perubahan"] = url(perub)
    return out


def _layers(s2, det, imgs):
    def biner(warna):
        return {"min": 0, "max": 1, "palette": ["000000", warna]}
    return {
        "tetap": {"image": imgs["tetap"], "vis_params": biner(WARNA["tetap"])},
        "bergeser": {"image": imgs["bergeser"], "vis_params": biner(WARNA["bergeser"])},
        "baru": {"image": imgs["baru"], "vis_params": biner(WARNA["baru"])},
        "kja_t1": {"image": det[0]["bersih"], "vis_params": biner(WARNA["t1"])},
        "kja_t2": {"image": det[1]["bersih"], "vis_params": biner(WARNA["t2"])},
        "s2_t1": {"image": s2[0], "vis_params": dict(VIS_RGB)},
        "s2_t2": {"image": s2[1], "vis_params": dict(VIS_RGB)},
    }


# ============================ 9. FUNGSI UTAMA (KONTRAK BAKU) ============================
def jalankan_analisis_mingguan(tgl_akhir=None, *, konfigurasi=None,
                               jendela_s1_hari=14, jendela_s2_hari=45, jarak_t1_hari=14,
                               ambang_vv_db=-14.5, ambang_vh_db=-21.0, aturan_s1="atau",
                               ambang_ndvi=0.35, ambang_ndwi_min=None, cs_min=0.60,
                               min_piksel_klaster=5, radius_median_m=15, toleransi_geser_m=20,
                               buffer_pantai_m=15, orbit=None, metode_klaster="vektor",
                               maks_detail_klaster=300, dimensi_thumb=1600, wilayah_thumb=None,
                               sertakan_detail=False, kredensial=None):
    """
    T2 = jendela mundur dari `tgl_akhir` (default: hari ini); T1 = jendela yang berakhir
    `jarak_t1_hari` hari sebelumnya (default 14 -> dua jendela 14 hari berurutan, tidak tumpang tindih).
    S1: komposit `jendela_s1_hari` hari. S2: komposit `jendela_s2_hari` hari ke belakang.
    konfigurasi: dict / list-of-records tab `konfigurasi` Google Sheets (opsional).

    Return (kontrak baku): {"metrik": {...}, "url_thumbnail": str, "layers_peta": {...}}
    plus "detail" jika sertakan_detail=True.
    """
    t0 = time.time()
    inisialisasi_ee(kredensial)
    cfg = baca_konfigurasi(konfigurasi)
    if aturan_s1 not in ("atau", "dan"):
        raise ValueError("aturan_s1 harus 'atau' atau 'dan'.")
    p = {"ambang_vv_db": ambang_vv_db, "ambang_vh_db": ambang_vh_db, "aturan_s1": aturan_s1,
         "ambang_ndvi": ambang_ndvi, "ambang_ndwi_min": ambang_ndwi_min,
         "min_piksel_klaster": min_piksel_klaster}

    akhir2 = _ke_date(tgl_akhir) if tgl_akhir else date.today()
    akhir1 = akhir2 - timedelta(days=jarak_t1_hari)
    per = [_periode(akhir1, jendela_s1_hari, jendela_s2_hari),
           _periode(akhir2, jendela_s1_hari, jendela_s2_hari)]

    batas, kotak, info_waduk = dapatkan_batas_waduk()
    air_img = _topeng_air(batas, buffer_pantai_m)

    s1k = [_koleksi_s1(kotak, *_rentang(*pr["s1"]), orbit) for pr in per]
    s2k = [_koleksi_s2(kotak, *_rentang(*pr["s2"])) for pr in per]
    n = ee.Dictionary({
        "s1_0": s1k[0].size(), "s1_1": s1k[1].size(),
        "s2_0": s2k[0].size(), "s2_1": s2k[1].size(),
        "tgl_s1_0": _tgl_terbaru(s1k[0]), "tgl_s1_1": _tgl_terbaru(s1k[1]),
        "tgl_s2_0": _tgl_terbaru(s2k[0]), "tgl_s2_1": _tgl_terbaru(s2k[1]),
    }).getInfo()
    for i, nama in enumerate(("T1", "T2")):
        if n[f"s1_{i}"] == 0:
            raise ValueError(f"Tidak ada citra Sentinel-1 untuk {nama} "
                             f"({per[i]['s1'][0]} s/d {per[i]['s1'][1]}). Perlebar jendela_s1_hari.")

    s1 = [_komposit_s1(k, radius_median_m) for k in s1k]
    s2 = [_komposit_s2(k, cs_min, n[f"s2_{i}"]) for i, k in enumerate(s2k)]
    det = [_deteksi(s1[i], s2[i], air_img, p) for i in (0, 1)]
    m = [_metrik(det[i], batas, air_img, n[f"s1_{i}"], n[f"s2_{i}"], cfg,
                 metode_klaster, maks_detail_klaster) for i in (0, 1)]
    perubahan, imgs = _perubahan(det[0]["bersih"], det[1]["bersih"], air_img, batas,
                                 toleransi_geser_m, m[0]["luas_bersih_ha"], m[1]["luas_bersih_ha"], cfg)
    perubahan["selisih_klaster"] = m[1]["jumlah_klaster"] - m[0]["jumlah_klaster"]
    thumb = _thumbnail(s1, s2, det, imgs, batas, wilayah_thumb or kotak, dimensi_thumb, sertakan_detail)

    iso = akhir2.isocalendar()
    hasil = {
        "metrik": {"minggu_id": f"{iso[0]}-W{iso[1]:02d}",
                   "tgl_citra_s1": n["tgl_s1_1"],      # tanggal citra S1 terbaru dalam jendela T2
                   "tgl_citra_s2": n["tgl_s2_1"],      # tanggal citra S2 terbaru dalam jendela T2 ("-" = kosong)
                   "jumlah_klaster": int(m[1]["jumlah_klaster"]),
                   "luas_bersih_ha": float(m[1]["luas_bersih_ha"]),
                   "estimasi_petak_min": int(m[1]["estimasi_petak_min"]),
                   "estimasi_petak_tengah": int(m[1]["estimasi_petak_tengah"]),
                   "estimasi_petak_maks": int(m[1]["estimasi_petak_maks"]),
                   "persen_kuota": float(m[1]["persen_kuota"])},
        "url_thumbnail": thumb["kja_t2"],
        "layers_peta": _layers(s2, det, imgs),
    }
    if sertakan_detail:
        peringatan = []
        for i, nama in enumerate(("T1", "T2")):
            if m[i]["jumlah_citra_s1"] < 2:
                peringatan.append(f"{nama}: hanya {m[i]['jumlah_citra_s1']} citra Sentinel-1 dalam jendela.")
            if m[i]["jumlah_citra_s2"] == 0:
                peringatan.append(f"{nama}: tidak ada citra Sentinel-2, eceng gondok TIDAK tersaring.")
            elif m[i]["cakupan_s2_persen"] < 50:
                peringatan.append(f"{nama}: cakupan Sentinel-2 bebas awan hanya {m[i]['cakupan_s2_persen']}% "
                                  "area air; penyaringan eceng gondok parsial.")
        hasil["detail"] = {
            "periode": {"t1": per[0], "t2": per[1]},
            "t1": m[0], "t2": m[1], "perubahan": perubahan, "peringatan": peringatan,
            "url_thumbnail_lain": {k: v for k, v in thumb.items() if k != "kja_t2"},
            "konfigurasi_dipakai": cfg, "waduk": info_waduk,
            "parameter": {**p, "jendela_s1_hari": jendela_s1_hari, "jendela_s2_hari": jendela_s2_hari,
                          "jarak_t1_hari": jarak_t1_hari, "cs_min": cs_min,
                          "radius_median_m": radius_median_m, "buffer_pantai_m": buffer_pantai_m,
                          "orbit": orbit},
            "durasi_detik": round(time.time() - t0, 1),
        }
    return hasil


# ============================ 10. UTILITAS (AI & FOLIUM) ============================
def ringkasan_json(hasil, sertakan_klaster=False):
    """JSON murni (tanpa objek ee) untuk prompt Gemini atau simpan ke Sheets."""
    isi = {k: v for k, v in hasil.items() if k != "layers_peta"}
    if "detail" in isi and not sertakan_klaster:
        d = dict(isi["detail"])
        for t in ("t1", "t2"):
            d[t] = {k: v for k, v in d[t].items() if k != "detail_klaster"}
        isi["detail"] = d
    return json.dumps(isi, ensure_ascii=False, indent=2, default=str)


def tambah_layer_folium(peta, layer, nama, tampil=True):
    import folium
    mid = ee.Image(layer["image"]).getMapId(layer["vis_params"])
    folium.raster_layers.TileLayer(
        tiles=mid["tile_fetcher"].url_format, attr="Google Earth Engine",
        name=nama, overlay=True, control=True, show=tampil).add_to(peta)


def buat_peta_folium(hasil, zoom=12, tinggi=650):
    import folium
    peta = folium.Map(location=[PUSAT_WADUK[1], PUSAT_WADUK[0]], zoom_start=zoom,
                      tiles=None, height=tinggi)
    folium.TileLayer(tiles=ESRI_URL, attr="Esri, Maxar, Earthstar Geographics",
                     name="Citra satelit (Esri)", overlay=False, show=True).add_to(peta)
    for kunci, layer in hasil["layers_peta"].items():
        tambah_layer_folium(peta, layer, LABEL_LAYER.get(kunci, kunci),
                            tampil=kunci in ("tetap", "bergeser", "baru"))
    s, w, n, e = ambil_bbox_waduk()
    peta.fit_bounds([[s, w], [n, e]])
    folium.LayerControl(collapsed=False).add_to(peta)
    return peta


# ============================ UJI COBA ============================
if __name__ == "__main__":
    if not os.path.exists("credentials.json"):
        try:
            from google.colab import files
            files.upload()                      # unggah credentials.json di Colab
        except ImportError:
            raise SystemExit("credentials.json tidak ditemukan di folder kerja.")

    hasil = jalankan_analisis_mingguan("2026-11-15", sertakan_detail=True)
    print(json.dumps(hasil["metrik"], indent=2, ensure_ascii=False))
    print("url_thumbnail:", hasil["url_thumbnail"])
    for pesan in hasil["detail"]["peringatan"]:
        print("PERINGATAN:", pesan)
    print("Perubahan:", json.dumps(hasil["detail"]["perubahan"], ensure_ascii=False))

    peta = buat_peta_folium(hasil)
    try:
        get_ipython()                           # Colab / Jupyter
        from IPython.display import display
        display(peta)
    except NameError:
        peta.save("peta_kja.html")
        print("Peta disimpan ke peta_kja.html")
