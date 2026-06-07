from app.database import engine
from app.models import User
from app.database import Base

Base.metadata.create_all(bind=engine)

print("Database tables created successfully.")
