import uvicorn #type: ignore
from fastapi.middleware.cors import CORSMiddleware #type: ignore
from fastapi import FastAPI, Request, HTTPException#type: ignore
from fastapi.responses import JSONResponse #type: ignore
from utils.ai_core import AIAnalyst
from src.config import Configuration
from utils.ai_core.analyst import AIAnalyst

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------configuration----------------------

config = Configuration()

@app.on_event("shutdown")
async def shutdown_event():
    config.shutdown()
    
# ----------------------configuration----------------------
    
# ----------------------Route---------------------- 

ai_analyst = None

@app.post("/v1/chat/prompt/ai_config")
async def AI_config(request: Request):
    global ai_analyst
    data = await request.json()
    collections = data['collections'] 
    
    ai_analyst = AIAnalyst(collections=collections, llm_config=config, execution_mode=config.execution_mode)
    return JSONResponse({"status": "AI Analyst configured successfully"}, status_code=200)

@app.post("/v1/chat/prompt/response")
async def ChatPrompt(request: Request):
    global ai_analyst
    if ai_analyst is None:
        raise HTTPException(status_code=400, detail="AI Analyst not configured. Call /ai_config first")
    
    data = await request.json()
    if not data or 'query' not in data:
        raise HTTPException(status_code=400, detail="Missing query")
    
    user_query = data['query']
    final_answer = ai_analyst.web_start_ai_analyst(user_query=user_query)
    return JSONResponse({"response": final_answer}, status_code=201)

# ----------------------Route----------------------

if __name__ == "__main__":
    
    uvicorn.run("entrypoint:app", port=5001, reload=True)
