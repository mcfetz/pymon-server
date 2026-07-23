import logging
import os

from flask import Flask
from flask_cors import CORS
from flasgger import Swagger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db_models import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Restrict CORS to configured origins; override via PYMON_CORS_ORIGINS env var
# (comma-separated list, e.g. "https://pymon.example.com,http://localhost:5174")
_cors_origins = os.environ.get("PYMON_CORS_ORIGINS", "").strip()
CORS(app, origins=_cors_origins.split(",") if _cors_origins else [])

swagger = Swagger(app)

# SQLAlchemy ORM Setup
DATABASE_URL = "sqlite:///metrics.db"
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Create all tables if they do not exist yet
Base.metadata.create_all(bind=engine)
