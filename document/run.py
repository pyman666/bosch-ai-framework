if __name__ == "__main__":
    import uvicorn
    from document.main import app

    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="debug")
