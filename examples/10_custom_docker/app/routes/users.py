"""User routes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict

router = APIRouter()
users_db: Dict[int, dict] = {}
counter = 0


class UserCreate(BaseModel):
    name: str
    email: str


@router.get("/")
def list_users():
    return {"users": list(users_db.values())}


@router.post("/")
def create_user(user: UserCreate):
    global counter
    counter += 1
    users_db[counter] = {"id": counter, "name": user.name, "email": user.email}
    return users_db[counter]


@router.get("/{user_id}")
def get_user(user_id: int):
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    return users_db[user_id]
