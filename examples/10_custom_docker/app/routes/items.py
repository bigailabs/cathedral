"""Item routes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Optional

router = APIRouter()
items_db: Dict[int, dict] = {}
counter = 0


class ItemCreate(BaseModel):
    name: str
    price: float
    description: Optional[str] = None


@router.get("/")
def list_items():
    return {"items": list(items_db.values())}


@router.post("/")
def create_item(item: ItemCreate):
    global counter
    counter += 1
    items_db[counter] = {"id": counter, "name": item.name, "price": item.price, "description": item.description}
    return items_db[counter]


@router.get("/{item_id}")
def get_item(item_id: int):
    if item_id not in items_db:
        raise HTTPException(status_code=404, detail="Item not found")
    return items_db[item_id]
