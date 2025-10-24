import express from "express";
import { createStudentAccount } from "../utils/RBAC.js"; // adjust path if needed

const router = express.Router();

router.post("/register", (req, res) => {
  const data = req.body;
    console.log("Register route hit with data:", data);
  const requiredFields = [
    "studentId",
    "firstName",
    "middleName",
    "lastName",
    "email",
    "year",
    "course",
    "password",
  ];

  // Check for missing fields
  const missing = requiredFields.filter((field) => !(field in data));
  if (missing.length > 0) {
    return res.status(400).json({ error: `Missing fields: ${missing.join(", ")}` });
  }

  // Map short course code to full course name
  const courseMap = {
    BSCS: "Bachelor of Science in Computer Science (BSCS)",
    BSIT: "Bachelor of Science in Information Technology (BSIT)",
    BSHM: "Bachelor of Science in Hospitality Management (BSHM)",
    BSTM: "Bachelor of Science in Tourism Management (BSTM)",
    BSOAd: "Bachelor of Science in Office Administration (BSOAd)",
    BECEd: "Bachelor of Early Childhood Education (BECEd)",
    BTLEd: "Bachelor of Technology in Livelihood Education (BTLEd)",
  };

  const courseFull = courseMap[data.course] || data.course;

  try {
    const result = createStudentAccount({
      studentId: data.studentId,
      firstName: data.firstName,
      middleName: data.middleName,
      lastName: data.lastName,
      year: data.year,
      course: courseFull,
      password: data.password,
      email: data.email,
    });

    if (result.error) {
      return res.status(409).json(result);
    }

    return res.json(result);
  } catch (err) {
    console.error("Register error:", err);
    return res.status(500).json({ error: "Server error during registration" });
  }
});

export default router;
