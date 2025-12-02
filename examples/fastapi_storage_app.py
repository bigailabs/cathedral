"""
FastAPI Storage Demo Application

This is a sample FastAPI application that demonstrates storage integration.
Deploy it using the Basilica SDK:

    deployment = client.deploy(
        name="my-api",
        source="fastapi_storage_app.py",
        port=8000,
        storage=True,
    )
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import socket
from pathlib import Path
from datetime import datetime

app = FastAPI(title="Basilica Storage Demo")

STORAGE_PATH = Path("/data")


class WriteRequest(BaseModel):
    filename: str
    content: str


@app.get("/")
def root():
    return {
        "service": "Basilica FastAPI Demo",
        "hostname": socket.gethostname(),
        "storage_mounted": STORAGE_PATH.exists(),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/storage/list")
def list_files():
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    files = []
    for f in STORAGE_PATH.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            files.append(str(f.relative_to(STORAGE_PATH)))
    return {"files": files, "count": len(files)}


@app.post("/storage/write")
def write_file(req: WriteRequest):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / req.filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(req.content)
    return {"success": True, "path": req.filename, "size": len(req.content)}


@app.get("/storage/read/{filename:path}")
def read_file(filename: str):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return {"path": filename, "content": file_path.read_text()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
