"""Flask application factory for the Salesforce Data Browser."""

from flask import Flask

from sf_browser import routes


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    app.register_blueprint(routes.bp)
    app.teardown_appcontext(routes.close_db)

    return app
