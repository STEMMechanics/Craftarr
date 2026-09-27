from .env import getenv

import uvicorn

from dotenv import load_dotenv


load_dotenv(
    getenv(
        "CRAFTARR_CONSOLE_ENV",
        ".env",
    )
)


def main():

    host = getenv(
        "CRAFTARR_CONSOLE_HOST",
        "127.0.0.1",
    )

    port = int(
        getenv(
            "CRAFTARR_CONSOLE_PORT",
            "8000",
        )
    )

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()