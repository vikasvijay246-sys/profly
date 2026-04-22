import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-CHANGE-IN-PRODUCTION")

    _db_url = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(BASE_DIR, 'propflow.db')}"
    )
    if _db_url.startswith("postgres://"):          # Heroku compat
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Pool settings for PostgreSQL
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,      # reconnect on stale connections
        "pool_recycle": 300,        # recycle connections every 5 min
    }

    UPLOAD_FOLDER      = os.path.join(BASE_DIR, "static", "uploads")
    # Hard limit: 10 MB per file upload
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 10)) * 1024 * 1024

    ALLOWED_EXTENSIONS = {
        # Images
        "png", "jpg", "jpeg", "gif", "webp",
        # Videos
        "mp4", "mov", "avi", "webm",
        # Audio / voice notes
        "mp3", "ogg", "wav", "m4a", "aac",
        # Documents
        "pdf", "doc", "docx", "xls", "xlsx", "txt", "zip",
    }

    ADMIN_PHONE    = os.environ.get("ADMIN_PHONE",    "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

    # Rent auto-generate: day of month to create next month's record
    RENT_GENERATE_DAY = int(os.environ.get("RENT_GENERATE_DAY", 25))
