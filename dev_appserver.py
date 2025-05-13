import uvicorn
from app import create_app

app = create_app()


def main():

    uvicorn.run(
        "dev_appserver:app",
        reload=True,
        log_config="app/config/logging_config.json",
    )


if __name__ == "__main__":
    main()
