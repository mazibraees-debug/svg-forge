from flask import Flask, render_template, request, Response, jsonify
from PIL import Image, ImageFilter, ImageEnhance
import vtracer
import tempfile
import os
import re
import base64
from io import BytesIO
from functools import wraps

app = Flask(__name__)

# ── API Key ──────────────────────────────────────────────────────────────────
# Set this as an environment variable in Vercel dashboard:
#   Key:   API_KEY
#   Value: any secret string you choose, e.g. "svgforge-secret-xyz123"
#
# Anyone calling /api/* must pass this in the header:
#   X-API-Key: svgforge-secret-xyz123

API_KEY = os.environ.get("API_KEY", "svgforge-secret-xyz123")


def require_api_key(f):
    """Decorator that blocks requests without a valid API key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key or key != API_KEY:
            return jsonify({"error": "Unauthorized. Provide a valid X-API-Key header."}), 401
        return f(*args, **kwargs)
    return decorated


# ── Presets ──────────────────────────────────────────────────────────────────

MIN_SIZE = 1024

PRESETS = {
    "illustration": {
        "colormode":        "color",
        "hierarchical":     "stacked",
        "mode":             "spline",
        "filter_speckle":   6,
        "color_precision":  6,
        "layer_difference": 20,
        "corner_threshold": 60,
        "length_threshold": 4.0,
        "max_iterations":   10,
        "splice_threshold": 45,
        "path_precision":   3,
    },
    "logo": {
        "colormode":        "color",
        "hierarchical":     "stacked",
        "mode":             "spline",
        "filter_speckle":   4,
        "color_precision":  8,
        "layer_difference": 10,
        "corner_threshold": 60,
        "length_threshold": 3.0,
        "max_iterations":   10,
        "splice_threshold": 30,
        "path_precision":   4,
    },
}


# ── Image processing helpers ─────────────────────────────────────────────────

def preprocess_image(img: Image.Image, preset_name: str) -> Image.Image:
    width, height = img.size

    if preset_name == "logo":
        if width < MIN_SIZE or height < MIN_SIZE:
            scale = max(MIN_SIZE / width, MIN_SIZE / height)
            img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        gray = img.convert("L")
        gray = gray.filter(ImageFilter.SHARPEN)
        gray = gray.filter(ImageFilter.SHARPEN)
        bw = gray.point(lambda p: 255 if p >= 128 else 0, mode="1")
        img = bw.convert("RGBA")

    elif preset_name == "illustration":
        if width < 512 or height < 512:
            scale = max(512 / width, 512 / height)
            img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        img = img.convert("RGBA")
        rgb = img.convert("RGB")
        rgb = ImageEnhance.Contrast(rgb).enhance(1.3)
        r, g, b = rgb.split()
        img = Image.merge("RGBA", (r, g, b, img.split()[3]))

    else:
        img = img.convert("RGBA")

    return img


def fix_svg_viewbox(svg_content: str) -> str:
    width_match  = re.search(r'<svg[^>]*\swidth="([^"]+)"',  svg_content)
    height_match = re.search(r'<svg[^>]*\sheight="([^"]+)"', svg_content)

    w = re.sub(r'[^\d.]', '', width_match.group(1)  if width_match  else "1024") or "1024"
    h = re.sub(r'[^\d.]', '', height_match.group(1) if height_match else "1024") or "1024"

    svg_content = re.sub(
        r'<svg[^>]*>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {w} {h}" '
        f'width="100%" height="100%" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="max-width:100%;max-height:100%;display:block;">',
        svg_content,
        count=1
    )
    return svg_content


def exact_to_svg(img: Image.Image) -> str:
    width, height = img.size
    buffer = BytesIO()
    img.convert("RGBA").save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" '
        f'width="100%" height="100%" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="max-width:100%;max-height:100%;display:block;">\n'
        f'  <image href="data:image/png;base64,{b64}" '
        f'x="0" y="0" width="{width}" height="{height}" '
        f'preserveAspectRatio="xMidYMid meet"/>\n'
        f'</svg>'
    )


def convert_to_svg(img: Image.Image, preset_name: str) -> str:
    if preset_name == "exact":
        return exact_to_svg(img)

    img = preprocess_image(img, preset_name)
    preset = PRESETS[preset_name]

    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = os.path.join(tmpdir, "input.png")
        svg_path = os.path.join(tmpdir, "output.svg")
        img.save(png_path, format="PNG")

        vtracer.convert_image_to_svg_py(
            png_path,
            svg_path,
            colormode=        preset["colormode"],
            hierarchical=     preset["hierarchical"],
            mode=             preset["mode"],
            filter_speckle=   preset["filter_speckle"],
            color_precision=  preset["color_precision"],
            layer_difference= preset["layer_difference"],
            corner_threshold= preset["corner_threshold"],
            length_threshold= preset["length_threshold"],
            max_iterations=   preset["max_iterations"],
            splice_threshold= preset["splice_threshold"],
            path_precision=   preset["path_precision"],
        )

        with open(svg_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

    return fix_svg_viewbox(svg_content)


# ── Web UI routes (no API key needed) ────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    svg_data = None
    error    = None
    filename = None

    if request.method == "POST":
        if "image" not in request.files:
            error = "No file selected!"
        else:
            file   = request.files["image"]
            preset = request.form.get("preset", "illustration")

            if file.filename == "":
                error = "No file selected!"
            else:
                try:
                    img     = Image.open(file.stream)
                    svg_str = convert_to_svg(img, preset_name=preset)
                    svg_data = base64.b64encode(svg_str.encode("utf-8")).decode("utf-8")
                    filename = os.path.splitext(file.filename)[0] + ".svg"
                except Exception as e:
                    error = f"Conversion failed: {e}"

    return render_template("index.html", svg_data=svg_data, filename=filename, error=error)


@app.route("/download", methods=["POST"])
def download():
    svg_data = request.form.get("svg_data", "")
    filename = request.form.get("filename", "output.svg")
    try:
        svg_bytes = base64.b64decode(svg_data)
    except Exception:
        return "Invalid data", 400

    return Response(
        svg_bytes,
        mimetype="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── Public API routes (API key required) ─────────────────────────────────────

@app.route("/api/convert", methods=["POST"])
@require_api_key
def api_convert():
    """
    Convert an uploaded image to SVG.

    Request:
        Header:  X-API-Key: your-secret-key
        Body:    multipart/form-data
                   image  — image file (PNG or JPG)
                   preset — illustration | logo | exact  (default: illustration)

    Response JSON:
        {
          "success": true,
          "filename": "logo.svg",
          "preset_used": "logo",
          "svg_base64": "<base64 encoded SVG string>"
        }
    """
    if "image" not in request.files:
        return jsonify({"error": "No image file provided. Send as multipart form field 'image'."}), 400

    file   = request.files["image"]
    preset = request.form.get("preset", "illustration")

    if preset not in ("illustration", "logo", "exact"):
        return jsonify({"error": f"Invalid preset '{preset}'. Use: illustration, logo, exact"}), 400

    if file.filename == "":
        return jsonify({"error": "Empty filename."}), 400

    try:
        img     = Image.open(file.stream)
        svg_str = convert_to_svg(img, preset_name=preset)
        svg_b64 = base64.b64encode(svg_str.encode("utf-8")).decode("utf-8")

        return jsonify({
            "success":    True,
            "filename":   os.path.splitext(file.filename)[0] + ".svg",
            "preset_used": preset,
            "svg_base64": svg_b64,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/presets", methods=["GET"])
@require_api_key
def api_presets():
    """
    Return available presets.

    Request:
        Header: X-API-Key: your-secret-key

    Response JSON:
        { "presets": [ { "id": "...", "label": "...", "desc": "..." } ] }
    """
    return jsonify({
        "presets": [
            {"id": "illustration", "label": "Illustration", "desc": "Artwork, cartoons, flat designs"},
            {"id": "logo",         "label": "Logo / Icon",  "desc": "Crisp shapes, sharp edges — hard threshold preprocessing"},
            {"id": "exact",        "label": "Exact Image",  "desc": "Pixel-perfect replica embedded in SVG, no vector tracing"},
        ]
    })


@app.route("/api/health", methods=["GET"])
def api_health():
    """Public health check — no API key needed."""
    return jsonify({"status": "ok", "service": "SVG Forge"})


if __name__ == "__main__":
    app.run(debug=True)