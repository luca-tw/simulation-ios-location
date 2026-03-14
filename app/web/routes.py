from flask import Blueprint, render_template, send_from_directory
import os

web_bp = Blueprint("web", __name__)


@web_bp.route("/assets/<path:filename>")
def serve_assets(filename):
    # Locate the assets directory within the project root
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assets_dir = os.path.join(root_dir, 'assets')
    return send_from_directory(assets_dir, filename)


@web_bp.get("/")
def index():
    return render_template("index.html")
