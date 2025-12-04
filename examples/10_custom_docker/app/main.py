"""Main FastAPI application."""
from fastapi import FastAPI
from app.routes import users, items

app = FastAPI(title="My API", version="1.0.0")

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(items.router, prefix="/items", tags=["items"])


@app.get("/")
def root():
    return {"app": "My API", "version": "1.0.0"}


@app.get("/health")
def health():
    return {"status": "healthy"}
