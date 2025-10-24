import fs from "fs";
import path from "path";
import bcrypt from "bcrypt";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const __outdirname = path.dirname(__dirname);

// ======== Database Path ========
const DB_FILE = path.resolve(__outdirname, "src/accounts/students.json");

// ======== Security Config ========
const SALT_ROUNDS = 10;

// Utility: detect if a string looks like a bcrypt hash
const isBcryptHash = (value) => typeof value === "string" && /^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$/.test(value);

// ======== File I/O Helpers ========
export function loadStudents() {
  if (!fs.existsSync(DB_FILE)) {
    return {};
  }
  const raw = fs.readFileSync(DB_FILE, "utf-8");
  try {
    const parsed = JSON.parse(raw);
    // Ensure we always return an object for keyed access
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function saveStudents(students) {
  const content = JSON.stringify(students ?? {}, null, 2);
  fs.writeFileSync(DB_FILE, content, "utf-8");
}

// ======== RBAC Core ========
export function getRoleFromCourse(course) {
  const courseRoles = {
    "Bachelor of Science in Computer Science (BSCS)": "student CS",
    "Bachelor of Science in Information Technology (BSIT)": "student IT",
    "Bachelor of Science in Hospitality Management (BSHM)": "student HM",
    "Bachelor of Science in Tourism Management (BSTM)": "student TM",
    "Bachelor of Science in Office Administration (BSOAd)": "student OAd",
    "Bachelor of Early Childhood Education (BECEd)": "student ECEd",
    "Bachelor of Technology in Livelihood Education (BTLEd)": "student TLEd",
  };
  return courseRoles[course] || "student";
}

// ======== Account Creation ========
export function createStudentAccount({
  studentId,
  firstName,
  middleName,
  lastName,
  year,
  course,
  password,
  email,
  role = null
}) {
  const students = loadStudents();

  if (students[studentId]) {
    return { error: "Student ID already exists" };
  }

  // Hash password with bcrypt
  const hashedPassword = bcrypt.hashSync(password, SALT_ROUNDS);
  const fullName = [firstName, middleName, lastName].filter(Boolean).join(" ");

  const assignedRole = role || getRoleFromCourse(course);

  students[studentId] = {
    studentName: fullName,
    year,
    course,
    email,
    password: hashedPassword,
    role: assignedRole,
  };
  saveStudents(students);

  return {
    message: "Student account created successfully",
    studentId,
    role: assignedRole,
  };
}

// Build a document matching src/components/constructor.js userSchema
export function buildConstructorUserDoc({
  studentId,
  fullName,
  year,
  course,
  email,
  hashedPassword,
  role,
}) {
  return {
    _id: studentId,
    student_name: fullName,
    year,
    course,
    email,
    password: hashedPassword,
    role,
  };
}

// Convenience helper: get a constructor-shaped doc for an existing studentId
export function getConstructorUserDoc(studentId) {
  const students = loadStudents();
  const rec = students[studentId];
  if (!rec) return null;
  const fullName = rec.studentName;
  return buildConstructorUserDoc({
    studentId,
    fullName,
    year: rec.year,
    course: rec.course,
    email: rec.email,
    hashedPassword: rec.password,
    role: rec.role,
  });
}

// ======== Password Verification ========
export function verifyPassword(studentId, password) {
  const students = loadStudents();
  const record = students[studentId];
  if (!record || !record.password) return false;

  const stored = record.password;
  // If it's a bcrypt hash, compare securely
  if (isBcryptHash(stored)) {
    try {
      return bcrypt.compareSync(password, stored);
    } catch {
      return false;
    }
  }

  // Fallback for legacy plaintext passwords. If it matches, upgrade to bcrypt.
  if (stored === password) {
    try {
      const newHash = bcrypt.hashSync(password, SALT_ROUNDS);
      students[studentId].password = newHash;
      saveStudents(students);
    } catch {
      // ignore upgrade failure; still allow login this time
    }
    return true;
  }

  return false;
}

// ======== Student Info ========
export function getStudentInfo(studentId) {
  const students = loadStudents();
  if (!students[studentId]) return { error: "Student not found" };

  const student = students[studentId];
  return {
    studentId,
    studentName: student.studentName,
    year: student.year,
    course: student.course,
    role: student.role,
  };
}

// ======== Admin: Get All Students ========
export function getAllStudents(requestingUserId) {
  const students = loadStudents();

  if (!students[requestingUserId]) {
    return { error: "Requesting user not found" };
  }

  if (students[requestingUserId].role !== "admin") {
    return { error: "Unauthorized access" };
  }

  return Object.entries(students).map(([sid, data]) => ({
    studentId: sid,
    studentName: data.studentName,
    year: data.year,
    course: data.course,
    role: data.role,
  }));
}

export function mapStudentRole(studentRole) {
  const roleMap = {
    "student CS": { role: "Teaching_Faculty", assign: ["BSCS"] },
    "student IT": { role: "Teaching_Faculty", assign: ["BSIT"] },
    "student HM": { role: "Teaching_Faculty", assign: ["BSHM"] },
    "student TM": { role: "Teaching_Faculty", assign: ["BSTM"] },
    "student OAd": { role: "Teaching_Faculty", assign: ["BSOAd"] },
    "student ECEd": { role: "Teaching_Faculty", assign: ["BECEd"] },
    "student TLEd": { role: "Teaching_Faculty", assign: ["BTLEd"] },
    "faculty": { role: "Faculty", assign: ["Faculty"] },
    "Guest": { role: "Guest", assign: ["Guest"] },
    "student": { role: "Student", assign: [] }, // fallback
    "admin": { role: "Admin", assign: [""] },
  };
  return roleMap[studentRole] || { role: "Student", assign: [] };
};