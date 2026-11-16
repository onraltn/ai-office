import uvicorn
uvicorn.run('office.app:create_app', factory=True, host='127.0.0.1', port=8796)
