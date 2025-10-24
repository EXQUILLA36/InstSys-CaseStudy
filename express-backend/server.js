import express from "express";
import axios from "axios";
import cors from "cors";
import loginRoute from "./routes/loginRoute.js";
import fetchStudentRoute from "./routes/fetchStudentRoute.js";
import registerRoute from "./routes/registerRoute.js";
import refreshCollections from "./routes/refreshCollections.js";
import coursesRoute from "./routes/coursesRoute.js";
import { callPythonAPI, configPythonAPI } from "./API/PythonAPI.js";

const app = express();

console.log("server is starting...");

const PORT = process.env.PORT || 5000;

app.use(
  cors({
    origin: "http://localhost:5173", // allow your Vite frontend
    credentials: true,
  })
);

app.use(express.json());

// ✅ Health check route for frontend
app.get("/health", (req, res) => {
  res.status(200).json({ status: "ok", message: "Express server is running" });
  console.log("Health check endpoint was called.");
});

app.use("/", loginRoute);
app.use("/student", fetchStudentRoute);
app.use("/", refreshCollections);
app.use("/", registerRoute);
app.use("/", coursesRoute);

// ✅ Example endpoint that talks to Python
app.post("/v1/chat/prompt", async (req, res) => {
  try {
    const { query: userQuery } = req.body;
    console.log("Received request to /v1/chat/prompt with query:", userQuery);

    if (!userQuery) {
      return res.status(400).json({ error: "Missing query parameter" });
    }

    const response = await callPythonAPI(userQuery);
    res.json(response);
  } catch (error) {
    console.error("Error in /v1/chat/prompt:", error.message);
    res.status(500).json({ error: "Failed to fetch data from Python API" });
  }
});

(async () => {
  try {
    console.log("🧠 Initializing AI Analyst via Python API...");
    const result = await configPythonAPI(["default_collection"]);
    console.log("✅ AI Analyst configured successfully:", result);
  } catch (err) {
    console.error("❌ Failed to configure AI Analyst:", err.message);
  }
})();

app.listen(PORT, () => {
  console.log(`✅ Server is running on port ${PORT}`);
});
