from flask import Flask, render_template, request, Response, send_file
from PIL import Image, ImageFilter, ImageEnhance
import vtracer
import tempfile
import os
import re
import base64
from io import BytesIO

app = Flask(__name__)

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


def preprocess_image(img: Image.Image, preset_name: str) -> Image.Image:
    width, height = img.size

    if preset_name == "logo":
        if width < MIN_SIZE or height < MIN_SIZE:
            scale = max(MIN_SIZE / width, MIN_SIZE / height)
            img = img.resize(
                (int(width * scale), int(height * scale)),
                Image.LANCZOS
            )
        gray = img.convert("L")
        gray = gray.filter(ImageFilter.SHARPEN)
        gray = gray.filter(ImageFilter.SHARPEN)
        bw = gray.point(lambda p: 255 if p >= 128 else 0, mode="1")
        img = bw.convert("RGBA")

    elif preset_name == "illustration":
        if width < 512 or height < 512:
            scale = max(512 / width, 512 / height)
            img = img.resize(
                (int(width * scale), int(height * scale)),
                Image.LANCZOS
            )
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
    """Embed original image as base64 inside SVG — zero loss, pixel-perfect."""
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
    """Convert image to SVG string — fully in-memory using tempfiles for vtracer."""
    if preset_name == "exact":
        return exact_to_svg(img)

    img = preprocess_image(img, preset_name)
    preset = PRESETS[preset_name]

    # vtracer requires actual file paths, so we use a temp dir
    # but nothing persists — temp dir is deleted after this block
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


@app.route("/", methods=["GET", "POST"])
def index():
    svg_data   = None   # base64-encoded SVG for inline embedding
    error      = None
    filename   = None

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
                    img = Image.open(file.stream)
                    svg_str = convert_to_svg(img, preset_name=preset)

                    # Base64-encode SVG so we can pass it through the template
                    # and serve it back via /download without any disk writes
                    svg_data = base64.b64encode(svg_str.encode("utf-8")).decode("utf-8")
                    filename = os.path.splitext(file.filename)[0] + ".svg"

                except Exception as e:
                    error = f"Conversion failed: {e}"

    return render_template(
        "index.html",
        svg_data=svg_data,
        filename=filename,
        error=error,
    )


@app.route("/download", methods=["POST"])
def download():
    """Receive base64 SVG from form and return it as a file download."""
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


if __name__ == "__main__":
    app.run(debug=True)
