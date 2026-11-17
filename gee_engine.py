# -*- coding: utf-8 -*-
import json
import os
import re
from datetime import date, datetime, timedelta
import ee

PUSAT_WADUK = (107.38, -6.54)
KONFIG_DEFAULT = {
    "kuota_petak_pemprov": 11306,
    "luas_petak_min_m2": 48.0,
    "luas_petak_tengah_m2": 50.0,
    "luas_petak_maks_m2": 52.0,
}
SKALA = 10
CRS = "EPSG:32748"
LUAS_PIKSEL_M2 = SKALA ** 2
WARNA = {"t1": "00E5FF", "t2": "FF9100", "tetap": "FFD600", "bergeser": "D50000", "baru": "00C853"}
VIS_RGB = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 2500, "gamma": 1.3}
_CACHE = {}
_EE_SIAP = False

def inisialisasi_ee(kunci=None):
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

def baca_konfigurasi(konfigurasi=None):
    cfg = dict(KONFIG_DEFAULT)
    if konfigurasi:
        if not isinstance(konfigurasi, dict):
            konfigurasi = {str(r.get("parameter", "")).strip(): r.get("nilai") for r in konfigurasi}
        for k in cfg:
            v = konfigurasi.get(k)
            if v not in (None, ""):
                s = str(v).strip().replace(",", ".")
                cfg[k] = float(s)
    cfg["kuota_petak_pemprov"] = int(round(cfg["kuota_petak_pemprov"]))
    return cfg

def dapatkan_batas_waduk():
    if _CACHE:
        return _CACHE["batas"], _CACHE["kotak"]
    inisialisasi_ee()
    pusat = ee.Geometry.Point(list(PUSAT_WADUK))
    aoi = pusat.buffer(20000).bounds()
    occ = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0)
    air = occ.gt(80)
    tutup = air.focalMax(150, "circle", "meters").focalMin(150, "circle", "meters")
    vek = tutup.selfMask().reduceToVectors(
        reducer=ee.Reducer.countEvery(), geometry=aoi, crs=CRS, scale=30,
        geometryType="polygon", eightConnected=True, maxPixels=1e10, tileScale=4)
    kandidat = (vek.map(lambda f: ee.Feature(f).set("luas_km2", ee.Feature(f).geometry().area(100).divide(1e6)))
                .filterBounds(pusat.buffer(6000))
                .filter(ee.Filter.gte("luas_km2", 1.0)))
    gabungan = kandidat.geometry(100).dissolve(100).simplify(20)
    batas = ee.Geometry(gabungan.getInfo(), None, False)
    kotak = ee.Geometry(gabungan.bounds().getInfo(), None, False)
    _CACHE.update({"batas": batas, "kotak": kotak})
    return batas, kotak

def _deteksi_colab_asli(kotak, air_img, d0, d1):
    # 100% identik dengan sel [2] Colab yang menghasilkan 251,26 Ha
    s1_col = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(kotak).filterDate(d0, d1)
              .filter(ee.Filter.eq("instrumentMode", "IW"))
              .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
              .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
              .select(["VV", "VH"]))
    s2_col = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(kotak).filterDate(d0, d1)
    cs_col = ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED").filterBounds(kotak).filterDate(d0, d1)
    s2_linked = s2_col.linkCollection(cs_col, ["cs_cdf"])

    n_s1 = s1_col.size().getInfo()
    n_s2 = s2_linked.size().getInfo()
    tgl_s1 = ee.Date(s1_col.aggregate_max("system:time_start")).format("YYYY-MM-dd").getInfo() if n_s1 > 0 else "-"
    tgl_s2 = ee.Date(s2_linked.aggregate_max("system:time_start")).format("YYYY-MM-dd").getInfo() if n_s2 > 0 else "-"

    s1 = s1_col.median().reproject(crs=CRS, scale=SKALA).focalMedian(15, "circle", "meters")
    if n_s2 > 0:
        s2 = (s2_linked.map(lambda im: im.updateMask(im.select("cs_cdf").gte(0.60)))
              .select(["B2", "B3", "B4", "B8"]).median().reproject(crs=CRS, scale=SKALA))
    else:
        s2 = ee.Image.constant([0, 0, 0, 0]).rename(["B2", "B3", "B4", "B8"]).updateMask(ee.Image.constant(0))

    kuat = s1.select("VV").gt(-12.0).And(s1.select("VH").gt(-22.0)).rename("kja")
    mentah = kuat.updateMask(air_img).selfMask()

    ndvi = s2.normalizedDifference(["B8", "B4"])
    ndwi = s2.normalizedDifference(["B3", "B8"])
    eceng = ndvi.gt(0.30).Or(ndwi.lt(-0.30)).unmask(0).rename("eceng")

    bersih0 = mentah.updateMask(eceng.Not()).selfMask()
    ukuran = bersih0.connectedPixelCount(128, True)
    bersih = bersih0.updateMask(ukuran.gte(5)).selfMask()
    return s1, s2, bersih, tgl_s1, tgl_s2

def jalankan_analisis_mingguan(*args, **kwargs):
    inisialisasi_ee(kwargs.get("kredensial"))
    cfg = baca_konfigurasi(kwargs.get("konfigurasi") or kwargs.get("config"))

    # Tangkap tanggal T1 & T2 baik jika dikirim 4 argumen maupun 1 argumen
    if len(args) >= 4:
        t1_a, t1_b, t2_a, t2_b = str(args[0])[:10], str(args[1])[:10], str(args[2])[:10], str(args[3])[:10]
    else:
        akhir2 = str(args[0])[:10] if len(args) >= 1 and args[0] else str(kwargs.get("tgl_akhir", "2026-11-15"))[:10]
        dt2 = datetime.strptime(akhir2, "%Y-%m-%d").date()
        t2_a, t2_b = (dt2 - timedelta(days=6)).isoformat(), dt2.isoformat()
        dt1 = dt2 - timedelta(days=7)
        t1_a, t1_b = (dt1 - timedelta(days=6)).isoformat(), dt1.isoformat()

    batas, kotak = dapatkan_batas_waduk()
    air_img = ee.Image.constant(1).clip(batas).rename("air")

    s1_t1, s2_t1, kja_t1, _, _ = _deteksi_colab_asli(kotak, air_img, ee.Date(t1_a), ee.Date(t1_b).advance(1, "day"))
    s1_t2, s2_t2, kja_t2, tgl_s1, tgl_s2 = _deteksi_colab_asli(kotak, air_img, ee.Date(t2_a), ee.Date(t2_b).advance(1, "day"))

    # Hitung luas bersih & klaster persis seperti sel [4] Colab
    aoi_hitung = ee.Geometry.Point([107.385, -6.545]).buffer(8500).bounds()
    luas_m2 = ee.Number(
        ee.Image.pixelArea().updateMask(kja_t2).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi_hitung, scale=10, maxPixels=1e9
        ).get("area")
    ).getInfo() or 0

    klaster_img = kja_t2.connectedComponents(connectedness=ee.Kernel.plus(1), maxSize=1024)
    jml_klaster = ee.Number(
        klaster_img.select("labels").reduceRegion(
            reducer=ee.Reducer.countDistinct(), geometry=aoi_hitung, scale=10, maxPixels=1e9
        ).get("labels")
    ).getInfo() or 0

    luas_ha = round(luas_m2 / 10000.0, 2)
    petak_min = int(round(luas_m2 / cfg["luas_petak_maks_m2"]))
    petak_tengah = int(round(luas_m2 / cfg["luas_petak_tengah_m2"]))
    petak_maks = int(round(luas_m2 / cfg["luas_petak_min_m2"]))
    persen_kuota = round((petak_tengah / cfg["kuota_petak_pemprov"]) * 100, 1)

    # Layer perubahan T1 vs T2
    k1, k2 = kja_t1.unmask(0), kja_t2.unmask(0)
    k1d = k1.focalMax(20, "circle", "meters")
    k2d = k2.focalMax(20, "circle", "meters")
    tetap = k2.And(k1d).updateMask(air_img).selfMask()
    bergeser = k1.And(k2d.Not()).updateMask(air_img).selfMask()
    baru = k2.And(k1d.Not()).updateMask(air_img).selfMask()

    # Thumbnail aman & cepat untuk Gemini AI
    region_coords = kotak.getInfo()["coordinates"]
    vis_thumb = s1_t2.select(["VV"]).visualize(min=-25, max=0, palette=["0f172a", "334155", "94a3b8"]).blend(
        kja_t2.selfMask().visualize(palette=[WARNA["t2"]])
    )
    url_thumbnail = vis_thumb.clip(kotak).getThumbURL({
        "region": region_coords, "dimensions": 512, "format": "png"
    })

    iso = datetime.strptime(t2_b, "%Y-%m-%d").date().isocalendar()
    def biner(w):
        return {"min": 0, "max": 1, "palette": ["000000", w]}

    return {
        "metrik": {
            "minggu_id": f"{iso[0]}-W{iso[1]:02d}",
            "tgl_citra_s1": tgl_s1,
            "tgl_citra_s2": tgl_s2,
            "jumlah_klaster": int(jml_klaster),
            "luas_bersih_ha": luas_ha,
            "estimasi_petak_min": petak_min,
            "estimasi_petak_tengah": petak_tengah,
            "estimasi_petak_maks": petak_maks,
            "persen_kuota": persen_kuota,
        },
        "url_thumbnail": url_thumbnail,
        "layers_peta": {
            "tetap": {"image": tetap, "vis_params": biner(WARNA["tetap"])},
            "bergeser": {"image": bergeser, "vis_params": biner(WARNA["bergeser"])},
            "baru": {"image": baru, "vis_params": biner(WARNA["baru"])},
            "kja_t1": {"image": kja_t1, "vis_params": biner(WARNA["t1"])},
            "kja_t2": {"image": kja_t2, "vis_params": biner(WARNA["t2"])},
            "s2_t1": {"image": s2_t1, "vis_params": dict(VIS_RGB)},
            "s2_t2": {"image": s2_t2, "vis_params": dict(VIS_RGB)},
        }
    }
