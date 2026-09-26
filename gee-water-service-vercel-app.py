"""
Habitat Scanner 3D — Google Earth Engine water-detection service
==================================================================
A small standalone service, meant for Google Cloud Run, that does ONE
job: given a latitude/longitude and a radius, return permanent-water-
body polygons from ESA WorldCover satellite data via Google Earth
Engine, as GeoJSON. This is the same technique already proven working
in the earlier hazard-map-backend project (see fetch_gee_data there) —
this file is a minimal, focused re-packaging of that logic as its own
service, not a rewrite of the underlying approach.

Why this exists as a separate service rather than living inside the
Cloudflare Worker: Earth Engine's protocol is built around Google's
Python client library (the `ee` package used below). There's no
equivalent of that library for the JavaScript runtime Cloudflare
Workers use, so this piece has to run somewhere Python can run.
Google Cloud Run is a natural fit since it's Google's own
infrastructure, sits alongside the Google service account this needs,
and has a free tier (2,000,000 requests/month) far beyond what this
app will realistically use.

AUTHENTICATION — two supported paths, tried in this order:
1. Application Default Credentials: only works when this code runs
   ON Google Cloud's own infrastructure (e.g. Cloud Run) configured to
   run AS the Earth-Engine-enabled service account. On Vercel, Hugging
   Face Spaces, or any non-Google host, this path always fails
   harmlessly and the code falls through to path 2 below.
2. GOOGLE_SERVICE_ACCOUNT_JSON environment variable: set this to the
   full contents of the service account's JSON key file, added as a
   Vercel Environment Variable (Project Settings -> Environment
   Variables), never committed to the repo itself. This is the path
   that applies when hosting on Vercel.
"""

import os
import json
import ee
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # tighten to your real domain once deployed, see note near the bottom

GEE_ENABLED = False
GEE_INIT_ERROR = None

def _init_earth_engine():
    global GEE_ENABLED, GEE_INIT_ERROR
    # Path 1: Application Default Credentials (preferred — see module docstring)
    try:
        ee.Initialize()
        GEE_ENABLED = True
        print("Earth Engine initialized via Application Default Credentials.")
        return
    except Exception as e_adc:
        adc_error = str(e_adc)

    # Path 2: explicit service account JSON from an environment variable
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if service_account_json:
        try:
            key_data = json.loads(service_account_json)
            creds = ee.ServiceAccountCredentials(
                key_data["client_email"], key_data=json.dumps(key_data)
            )
            ee.Initialize(credentials=creds)
            GEE_ENABLED = True
            print("Earth Engine initialized via GOOGLE_SERVICE_ACCOUNT_JSON.")
            return
        except Exception as e_key:
            GEE_INIT_ERROR = "Service-account key auth failed: " + str(e_key)
            print(GEE_INIT_ERROR)
            return

    GEE_INIT_ERROR = "No working credentials found. ADC error: " + adc_error + \
        ". No GOOGLE_SERVICE_ACCOUNT_JSON environment variable is set either."
    print(GEE_INIT_ERROR)

_init_earth_engine()

# ESA WorldCover v100 class 80 = permanent water bodies. Class 90 (herbaceous
# wetland) is intentionally NOT included here — OpenStreetMap's natural=wetland
# tagging covers that, and this service is specifically about open water.
WATER_CLASS = 80


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "gee_enabled": GEE_ENABLED, "init_error": GEE_INIT_ERROR})


@app.route("/water", methods=["GET"])
def water():
    if not GEE_ENABLED:
        return jsonify({
            "error": "Earth Engine is not initialized on this service.",
            "detail": GEE_INIT_ERROR
        }), 503

    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        radius_m = float(request.args.get("radius_m", 21000))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon query parameters are required and must be numeric."}), 400

    if radius_m > 50000:
        radius_m = 50000  # sanity cap — this app only ever needs ~21 km

    try:
        aoi = ee.Geometry.Point(lon, lat).buffer(radius_m)
        img = ee.ImageCollection("ESA/WorldCover/v100").first()
        mask = img.eq(WATER_CLASS)
        vectors = img.updateMask(mask).reduceToVectors(
            geometry=aoi,
            scale=35,            # coarser than the 10 m native resolution — keeps
                                  # polygon count and compute cost manageable
            maxPixels=1e10,
            eightConnected=False,
            geometryType="polygon",
        )
        info = vectors.getInfo()
        features = []
        for f in info.get("features", []):
            props = f.get("properties", {}) or {}
            props["source"] = "Satellite (Google Earth Engine, ESA WorldCover v100)"
            props["custom_type"] = "water"
            f["properties"] = props
            features.append(f)

        return jsonify({"type": "FeatureCollection", "features": features})

    except Exception as e:
        return jsonify({"error": "Earth Engine request failed.", "detail": str(e)}), 502


if __name__ == "__main__":
    # Only used for local testing (python app.py). On Vercel, the "app"
    # object above is imported and run directly — this block never executes
    # there.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
