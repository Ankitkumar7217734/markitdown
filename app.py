import os
import io
import tempfile
from flask import Flask, request, send_file, render_template_string, flash, redirect

from markitdown import MarkItDown

import hindi_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB limit

md = MarkItDown(enable_plugins=False)
hindi_pdf.register(md)

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF to Markdown</title>
<style>
  :root {
    --bg: #efe8dc;
    --paper: #faf6ef;
    --ink: #1c1915;
    --muted: #6b6458;
    --line: #d4cbb8;
    --accent: #b5441f;
    --accent-soft: #f3e4d8;
    --error: #9b1c1c;
    --shadow: 0 18px 50px rgba(28, 25, 21, 0.08);
  }
  * { box-sizing: border-box; }
  html, body { min-height: 100%; }
  body {
    margin: 0;
    color: var(--ink);
    background:
      radial-gradient(1200px 500px at 50% -120px, #f7f1e6, transparent 70%),
      var(--bg);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, "Noto Serif Devanagari", Georgia, serif;
    line-height: 1.45;
  }
  .wrap {
    width: min(560px, calc(100% - 32px));
    margin: 0 auto;
    padding: 72px 0 64px;
    text-align: center;
  }
  .mark {
    display: inline-block;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.72rem;
    font-weight: 650;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    margin: 0 0 14px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: clamp(1.7rem, 4vw, 2.15rem);
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }
  .lede {
    margin: 0 0 32px;
    color: var(--muted);
    font-size: 1.02rem;
  }
  .card {
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 22px;
    box-shadow: var(--shadow);
  }
  .drop {
    position: relative;
    display: grid;
    justify-items: center;
    gap: 8px;
    padding: 36px 20px 32px;
    border: 1.5px dashed #c4b49a;
    border-radius: 14px;
    background: var(--accent-soft);
    text-align: center;
    cursor: pointer;
  }
  .drop:hover, .drop:focus-within {
    border-color: var(--accent);
    background: #f8ebe1;
  }
  .drop input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
  }
  .glyph {
    width: 44px;
    height: 44px;
    border-radius: 10px;
    background: var(--paper);
    border: 1px solid var(--line);
    display: grid;
    place-items: center;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--accent);
  }
  .drop strong {
    font-size: 1.05rem;
    font-weight: 600;
  }
  .drop span, .hint {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.82rem;
    color: var(--muted);
  }
  .drop #fname { color: var(--ink); }
  button {
    width: 100%;
    margin-top: 14px;
    padding: 13px 22px;
    border: 0;
    border-radius: 11px;
    background: var(--ink);
    color: var(--paper);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
  }
  button:hover { background: #2c2823; }
  button:active { transform: translateY(1px); }
  button:focus-visible, .drop:focus-within {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  .hint { margin: 14px 2px 0; }
  .msg {
    margin: 14px 0 0;
    padding: 10px 12px;
    text-align: left;
    border-radius: 10px;
    background: #f8e4e0;
    color: var(--error);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.9rem;
  }
  @media (max-width: 480px) {
    .wrap { padding: 40px 0 48px; }
    .card { padding: 16px; }
    .drop { padding: 28px 14px; }
  }
  @media (prefers-reduced-motion: reduce) {
    button:active { transform: none; }
  }
</style>
</head>
<body>
  <main class="wrap">
    <p class="mark">Markitdown</p>
    <h1>PDF to Markdown</h1>
    <p class="lede">Hindi and English documents, decoded as text you can keep.</p>
    <form class="card" method="post" action="/convert" enctype="multipart/form-data">
      <label class="drop">
        <input id="file" type="file" name="file" accept="application/pdf" required>
        <div class="glyph">PDF</div>
        <strong>Choose a PDF</strong>
        <span id="fname">or drop it here · up to 32 MB</span>
      </label>
      <button type="submit">Convert and download</button>
    </form>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for m in messages %}<p class="msg" role="alert">{{ m }}</p>{% endfor %}
      {% endif %}
    {% endwith %}
    <p class="hint">The Markdown file downloads when conversion finishes.</p>
  </main>
  <script>
    document.getElementById("file").addEventListener("change", function () {
      document.getElementById("fname").textContent =
        this.files[0] ? this.files[0].name : "or drop it here · up to 32 MB";
    });
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(PAGE)


@app.route("/convert", methods=["POST"])
def convert():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect("/")

    if not file.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF file.")
        return redirect("/")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        result = md.convert(tmp_path)
    except Exception as e:
        flash(f"Conversion failed: {e}")
        return redirect("/")
    finally:
        os.remove(tmp_path)

    md_bytes = io.BytesIO(result.text_content.encode("utf-8"))
    out_name = os.path.splitext(file.filename)[0] + ".md"

    return send_file(
        md_bytes,
        mimetype="text/markdown",
        as_attachment=True,
        download_name=out_name,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
