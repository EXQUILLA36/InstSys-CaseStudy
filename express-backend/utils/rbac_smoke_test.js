import { createStudentAccount, verifyPassword, loadStudents, saveStudents, getConstructorUserDoc } from "./RBAC.js";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const args = process.argv.slice(2);
const KEEP = process.env.PERSIST === "1" || args.includes("--keep") || args.includes("-k");

const TEST_ID = process.env.ID || "TEST-USER-001";
const TEST_ID_2 = process.env.ID2 || "TEST-USER-002";

function log(label, value) {
  console.log(`${label}:`, value);
}

(function main() {
  const students = loadStudents();
  // Cleanup any existing test user
  if (students[TEST_ID]) {
    delete students[TEST_ID];
    saveStudents(students);
  }
  if (students[TEST_ID_2]) {
    delete students[TEST_ID_2];
    saveStudents(students);
  }

  const res = createStudentAccount({
    studentId: TEST_ID,
    firstName: "Test",
    middleName: "User",
    lastName: "One",
    year: "1",
    course: "Bachelor of Science in Information Technology (BSIT)",
    password: "Secret123!",
    email: "test.user@example.com",
  });
  log("createStudentAccount", res);

  const ok = verifyPassword(TEST_ID, "Secret123!");
  const bad = verifyPassword(TEST_ID, "WrongPass");

  log("verifyPassword(correct)", ok);
  log("verifyPassword(wrong)", bad);

  // Create a second user with the SAME password to demonstrate salting
  const res2 = createStudentAccount({
    studentId: TEST_ID_2,
    firstName: "Test",
    middleName: "User",
    lastName: "Two",
    year: "1",
    course: "Bachelor of Science in Information Technology (BSIT)",
    password: "Secret123!",
    email: "test.user2@example.com",
  });
  log("createStudentAccount #2", res2);

  const post = loadStudents();
  const hash1 = post[TEST_ID]?.password;
  const hash2 = post[TEST_ID_2]?.password;

  // Show that the hashes differ even for the same password (due to random salt)
  log("hashesDifferent", hash1 && hash2 ? hash1 !== hash2 : false);

  // Show bcrypt header which contains version and cost factor (e.g., $2b$10$)
  // DO NOT log full hashes in real apps; only for local debugging
  const prefix1 = typeof hash1 === "string" ? hash1.slice(0, 7) : undefined;
  const prefix2 = typeof hash2 === "string" ? hash2.slice(0, 7) : undefined;
  log("bcryptPrefixUser1", prefix1);
  log("bcryptPrefixUser2", prefix2);

  // Resolve and print the absolute path of students.json
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  const __outdirname = path.dirname(__dirname); // express-backend
  const DB_FILE = path.resolve(__outdirname, "src/accounts/students.json");
  log("studentsJsonPath", DB_FILE);

  // Also output constructor.js-shaped documents to a preview JSON for visual check
  const previewPath = path.resolve(__outdirname, "src/accounts/constructor_preview.json");
  const doc1 = getConstructorUserDoc(TEST_ID);
  const doc2 = getConstructorUserDoc(TEST_ID_2);
  fs.writeFileSync(previewPath, JSON.stringify({ [TEST_ID]: doc1, [TEST_ID_2]: doc2 }, null, 2), "utf-8");
  log("constructorPreviewPath", previewPath);

  if (KEEP) {
    console.log("\nKeeping test users in students.json (use --keep or PERSIST=1).\n");
    return;
  }

  // Cleanup test users by default
  const after = loadStudents();
  delete after[TEST_ID];
  delete after[TEST_ID_2];
  saveStudents(after);
  console.log("\nCleaned up test users. Use --keep to persist them.\n");
})();
