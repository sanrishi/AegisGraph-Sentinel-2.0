import os
from dotenv import load_dotenv


def validate_environment():
    """Validate required environment variables on startup."""
    load_dotenv()

    required_vars = [
        "API_URL",
        "AEGIS_ALLOWED_ORIGINS",
    ]

    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing_vars)}. "
            f"Please create a .env file based on .env.example"
        )
