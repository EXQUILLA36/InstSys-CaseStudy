import express from "express";
import axios from "axios";

const router = express.Router();
const FASTAPI_URL = "http://localhost:5000"; // your AI service

// Configure AI Analyst
router.post("/config", async (req, res) => {
  try {
    const { collections } = req.body;
    const response = await axios.post(`${FASTAPI_URL}/ai_config`, { collections });
    res.json(response.data);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Chat with AI Analyst
router.post("/chat", async (req, res) => {
  try {
    const { query } = req.body;
    const response = await axios.post(`${FASTAPI_URL}/chatprompt`, { query });
    res.json(response.data);
  } catch (err) {
    if (err.response)
      res.status(err.response.status).json({ error: err.response.data.detail });
    else res.status(500).json({ error: err.message });
  }
});

export default router;
