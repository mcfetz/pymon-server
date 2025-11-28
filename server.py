import logging

from flasgger import Swagger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db_models import Base
from routes.alarms import *
from routes.metrics import *
from routes.plugins import *
from routes.agents import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)
app = Flask(__name__)
swagger = Swagger(app)

# SQLAlchemy ORM Setup
DATABASE_URL = "sqlite:///metrics.db"
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Create all tables if they do not exist yet
Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
