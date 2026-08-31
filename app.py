import os
import secrets
import tempfile
import threading
from urllib.parse import quote

from flask import Flask, Response, flash, redirect, render_template_string, request, url_for

from markitdown import MarkItDown

import hindi_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB limit

md = MarkItDown(enable_plugins=False)
hindi_pdf.register(md)

# One convert at a time: pdfium is not thread-safe, and a Free Render instance
# has 512 MB. Starting two books at once OOMs the dyno and Render returns
# "Not Found" (x-render-routing: no-server) until it comes back.
_convert_lock = threading.Lock()
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File to Markdown</title>
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
    font-size: 0.62rem;
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
    <h1>File to Markdown</h1>
    <p class="lede">Upload any document. Hindi and English PDFs are decoded as text you can keep.</p>
    <form class="card" method="post" action="/convert" enctype="multipart/form-data">
      <label class="drop">
        <input id="file" type="file" name="file" required>
        <div class="glyph">FILE</div>
        <strong>Choose a file</strong>
        <span id="fname">or drop it here · up to 32 MB</span>
      </label>
      <button type="submit">Convert and download</button>
    </form>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for m in messages %}<p class="msg" role="alert">{{ m }}</p>{% endfor %}
      {% endif %}
    {% endwith %}
    <p class="hint">Keep this tab open. A long book can take a minute, then the file downloads.</p>
  </main>
  <script>
    document.getElementById("file").addEventListener("change", function () {
      document.getElementById("fname").textContent =
        this.files[0] ? this.files[0].name : "or drop it here · up to 32 MB";
    });
    document.querySelector("form").addEventListener("submit", function () {
      var button = this.querySelector("button");
      button.disabled = true;
      button.textContent = "Starting…";
    });
  </script>
</body>
</html>
"""

WAIT = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Converting…</title>
<style>
  body {
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    background: #efe8dc;
    color: #1c1915;
    font-family: "Iowan Old Style", Palatino, Georgia, serif;
    text-align: center;
  }
  p { color: #6b6458; }
  .mark {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.72rem;
    font-weight: 650;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #b5441f;
  }
</style>
</head>
<body>
  <main>
    <p class="mark">Markitdown</p>
    <h1>Converting</h1>
    <p>{{ filename }}</p>
    <p>Keep this tab open. The Markdown file will download automatically.</p>
  </main>
</body>
</html>
"""


@app.route("/health")
def health():
    return "ok", 200


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        return convert()
    return render_template_string(PAGE)


@app.errorhandler(413)
def too_large(_error):
    flash("That file is over the 32 MB upload limit.")
    return redirect("/")


def _md_name(filename: str) -> str:
    base = os.path.splitext(os.path.basename(filename or "document"))[0].strip() or "document"
    return base.replace('"', "").replace("\r", "").replace("\n", "") + ".md"


def _safe_suffix(filename: str) -> str:
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    cleaned = "." + "".join(c for c in ext if c.isalnum())[:12]
    return cleaned if len(cleaned) > 1 else ".bin"


def _download(text: str, filename: str) -> Response:
    name = _md_name(filename)
    ascii_name = name.encode("ascii", "ignore").decode() or "download.md"
    return Response(
        text.encode("utf-8"),
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
            ),
            "Cache-Control": "no-store",
        },
    )


def _convert_job(job_id: str, data: bytes, filename: str) -> None:
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=_safe_suffix(filename), delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        with _convert_lock:
            result = md.convert(path)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["text"] = result.text_content or ""
    except Exception as error:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(error)
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _start_job(data: bytes, filename: str) -> str:
    job_id = secrets.token_urlsafe(8)
    with _jobs_lock:
        if len(_jobs) > 16:
            for key, job in list(_jobs.items()):
                if job["status"] != "pending":
                    del _jobs[key]
                    break
        _jobs[job_id] = {
            "status": "pending",
            "filename": filename,
            "text": "",
            "error": "",
        }
    threading.Thread(
        target=_convert_job, args=(job_id, data, filename), daemon=True
    ).start()
    return job_id


@app.route("/convert", methods=["POST"])
def convert():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect("/")

    data = file.read()
    if not data:
        flash("That file is empty.")
        return redirect("/")

    return redirect(url_for("job", job_id=_start_job(data, file.filename)))


@app.route("/jobs/<job_id>")
def job(job_id: str):
    with _jobs_lock:
        found = _jobs.get(job_id)
        if found is None:
            flash("That conversion expired. Upload the file again.")
            return redirect("/")
        snapshot = dict(found)
    if snapshot["status"] == "error":
        flash(f"Conversion failed: {snapshot['error']}")
        return redirect("/")
    if snapshot["status"] == "done":
        return _download(snapshot["text"], snapshot["filename"])
    return render_template_string(WAIT, filename=snapshot["filename"])


if __name__ == "__main__":
    assert _md_name("volumeH1.1.pdf") == "volumeH1.1.md"
    assert _md_name("notes.docx") == "notes.md"
    assert _safe_suffix("notes.docx") == ".docx"
    assert _safe_suffix("noext") == ".bin"
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
