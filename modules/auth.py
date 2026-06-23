"""
用户鉴权模块 — JWT + JSON 文件存储
"""
import json, logging, os, time
from datetime import datetime, timedelta
from pathlib import Path

import hashlib
from jose import jwt, JWTError
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

SECRET_KEY = "funsun-auction-jwt-secret-2026"
USERS_FILE = Path(__file__).parent.parent / "data" / "users.json"
TOKEN_EXPIRE_HOURS = 24


def _load_users() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    return json.loads(USERS_FILE.read_text(encoding="utf-8"))


def _save_users(users: list[dict]):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash(plain: str) -> str:
    return hashlib.sha256(f"funsun_salt_{plain}".encode()).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return _hash(plain) == hashed

def hash_password(plain: str) -> str:
    return _hash(plain)


def create_token(username: str, role: str) -> str:
    """生成 JWT token"""
    payload = {
        "username": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    """解析 JWT token"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None


def authenticate(username: str, password: str) -> dict | None:
    """验证用户登录，返回用户信息"""
    users = _load_users()
    for u in users:
        if u["username"] == username:
            stored = u.get("password", "")
            # 兼容明文密码和哈希密码
            if len(stored) == 64 and all(c in '0123456789abcdef' for c in stored):
                ok = verify_password(password, stored)
            else:
                ok = (stored == password)
            if ok:
                return {"username": u["username"], "role": u["role"]}
    return None


def get_current_user(request: Request) -> dict:
    """从请求 cookie 获取当前用户"""
    token = request.cookies.get("funsun_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期")
    return payload


# ====== 用户管理（管理后台用） ======

def list_users() -> list[dict]:
    users = _load_users()
    return [{"id": u["id"], "username": u["username"], "role": u["role"], "created_at": u.get("created_at", "")} for u in users]


def create_user(username: str, password: str, role: str = "user") -> dict:
    users = _load_users()
    new_id = max([u["id"] for u in users], default=0) + 1
    users.append({
        "id": new_id, "username": username,
        "password": hash_password(password),
        "role": role, "created_at": datetime.now().strftime("%Y-%m-%d"),
    })
    _save_users(users)
    return {"id": new_id, "username": username, "role": role}


def update_user(user_id: int, username: str = None, password: str = None, role: str = None) -> bool:
    users = _load_users()
    for u in users:
        if u["id"] == user_id:
            if username: u["username"] = username
            if password: u["password"] = hash_password(password)
            if role: u["role"] = role
            _save_users(users)
            return True
    return False


def delete_user(user_id: int) -> bool:
    users = _load_users()
    new_users = [u for u in users if u["id"] != user_id]
    if len(new_users) < len(users):
        _save_users(new_users)
        return True
    return False


def change_password(username: str, old_password: str, new_password: str) -> bool:
    """修改密码（管理后台用）"""
    users = _load_users()
    for u in users:
        if u["username"] == username:
            stored = u.get("password", "")
            if not (stored == old_password or verify_password(old_password, stored)):
                return False
            u["password"] = hash_password(new_password)
            _save_users(users)
            return True
    return False


# 启动时自动哈希旧密码
def migrate_passwords():
    users = _load_users()
    changed = False
    for u in users:
        pw = u.get("password", "")
        if len(pw) != 64:
            u["password"] = hash_password(pw)
            changed = True
    if changed:
        _save_users(users)
        logger.info("旧密码已迁移为 bcrypt 哈希")
