from flask import Flask

from app.api.routes import api_bp
from app.web.routes import web_bp


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)
    return app
