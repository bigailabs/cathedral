#!/usr/bin/env python3
"""
FastAPI deployment - Production-ready web API.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 03_fastapi.py
"""
from basilica import BasilicaClient

client = BasilicaClient()

deployment = client.deploy(
    name="api",
    source="""
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Hello from FastAPI!"}

@app.get("/items/{item_id}")
def get_item(item_id: int):
    return {"item_id": item_id, "name": f"Item {item_id}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
""",
    port=8000,
    pip_packages=["fastapi", "uvicorn"],
    ttl_seconds=600,
)

print(f"API docs: {deployment.url}/docs")
