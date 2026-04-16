#!/usr/bin/env python3
"""
Decorator-based FastAPI deployment.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 05_decorator_fastapi.py
"""
import cathedral


@cathedral.deployment(
    name="decorator-api",
    port=8000,
    pip_packages=["fastapi", "uvicorn"],
    ttl_seconds=600,
)
def serve():
    from fastapi import FastAPI
    import uvicorn

    app = FastAPI()

    @app.get("/")
    def root():
        return {"message": "Hello from decorator FastAPI!"}

    @app.get("/items/{item_id}")
    def get_item(item_id: int):
        return {"item_id": item_id, "name": f"Item {item_id}"}

    uvicorn.run(app, host="0.0.0.0", port=8000)


deployment = serve()
print(f"API docs: {deployment.url}/docs")
