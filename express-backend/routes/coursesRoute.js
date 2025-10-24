// routes/courseRoute.js
import express from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const router = express.Router();

// === Course management ===
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const COURSES_FILE = path.join(__dirname, "../src/config/courses.json");

// Load courses (auto-create empty if not exist or invalid)
function loadCourses() {
  try {
    // console.log("Loading courses from COURSES_FILE:", COURSES_FILE);
    if (!fs.existsSync(COURSES_FILE)) return [];
    const data = fs.readFileSync(COURSES_FILE, "utf-8");
    return JSON.parse(data);
  } catch (err) {
    console.error("Error loading courses:", err);
    return [];
  }
}

// Save courses
function saveCourses(courses) {
  try {
    fs.writeFileSync(COURSES_FILE, JSON.stringify(courses, null, 2), "utf-8");
  } catch (err) {
    console.error("Error saving courses:", err);
  }
}

// === Routes ===

// GET /courses
router.get("/courses", (req, res) => {
  res.json(loadCourses());
});

// POST /courses
router.post("/courses", (req, res) => {
  const data = req.body;
  const required = ["department", "program", "description"];

  if (!required.every((k) => Object.hasOwn(data, k))) {
    return res.status(400).json({ error: "Missing fields" });
  }

  const courses = loadCourses();
  courses.push(data);
  saveCourses(courses);

  res.status(201).json({ message: "Course added" });
});

export default router;
