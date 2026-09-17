import uvicorn


def main():
    uvicorn.run("files_to_feishu.app:create_app", factory=True, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
