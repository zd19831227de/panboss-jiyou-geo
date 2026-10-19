"""Authentication router — register & login"""
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, EmailStr
from app.database import get_db
from app.core.security import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest):
    """Register a new user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ? OR email = ?",
                       (req.username, req.email))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Username or email already exists")

        hashed = hash_password(req.password)
        cursor.execute(
            "INSERT INTO users (username, email, hashed_password) VALUES (?, ?, ?)",
            (req.username, req.email, hashed),
        )
        return {"message": "User created", "user_id": cursor.lastrowid}


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    """Login and receive JWT token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (req.username,))
        user = cursor.fetchone()
        if not user or not verify_password(req.password, user["hashed_password"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="Account disabled")

        token = create_access_token({"sub": str(user["id"]), "username": user["username"]})
        return TokenResponse(
            access_token=token,
            user={"id": user["id"], "username": user["username"], "role": user["role"]},
        )


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    """Return current user info (if token valid)"""
    return user or {"detail": "Not authenticated"}
