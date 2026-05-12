from fastapi import FastAPI
from app.routers import users

app = FastAPI(
    title="RadiusFiber OSS/BSS Backend",
    version="1.0.0"
)

@app.get("/")
def root():
    return {"message": "RadiusFiber Backend is Running"}

app.include_router(users.router)
