
import os

def test_sanitization(url):
    DATABASE_URL = url
    if DATABASE_URL.startswith("postgresql+asyncpg://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif DATABASE_URL.startswith("postgres+asyncpg://"):
        DATABASE_URL = DATABASE_URL.replace("postgres+asyncpg://", "postgresql://", 1)
    return DATABASE_URL

urls = [
    "postgresql+asyncpg://user:pass@host:5432/db",
    "postgres+asyncpg://user:pass@host:5432/db",
    "postgresql://user:pass@host:5432/db",
    "postgres://user:pass@host:5432/db",
]

for url in urls:
    sanitized = test_sanitization(url)
    print(f"Original: {url}")
    print(f"Sanitized: {sanitized}")
    assert not sanitized.startswith("postgresql+asyncpg://")
    assert not sanitized.startswith("postgres+asyncpg://")
    assert sanitized.startswith("postgresql://") or sanitized.startswith("postgres://")

print("\nVerification successful!")
