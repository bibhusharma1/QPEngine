def main() -> None:
    print("Hello from masterdeorca!")
"""MasterDeOrca application package."""


def main() -> None:
    import uvicorn

    from config.settings import API_PORT

    uvicorn.run("src.masterdeorca.app:app", host="0.0.0.0", port=API_PORT)
