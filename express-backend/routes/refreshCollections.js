import express from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const router = express.Router();
console.log("🔹 refresh collections route initialized");

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const __outdirname = path.dirname(__dirname);

const ROLE_ASSIGN_FILE = path.join(
  __outdirname,
  "/src/config/last_role_assign.json"
);

// console.log("ROLE_ASSIGN_FILE path:", ROLE_ASSIGN_FILE);

// Global-like variables (you can store these elsewhere if needed)
let collections = {};
let ai = null;
let role = "Admin";
let assign = ["BSCS"];

router.post("/refresh_collections", (req, res) => {
  collections = {}; // clear previous collections

  try {
    const data = fs.readFileSync(ROLE_ASSIGN_FILE, "utf-8");
    const lastRoleAssign = JSON.parse(data);

    role = lastRoleAssign.role || "Admin";
    assign = lastRoleAssign.assign || ["BSCS"];

    console.log(`\n\n\nRefreshed collections for role: ${role}, assign: ${assign}\n\n\n`);
  } catch (err) {
    role = "Admin";
    assign = ["BSCS"];
  }

  // If last login was guest, set role and assign to Guest
  if (role === "Guest") {
    assign = ["Guest"];
  }

  res.status(200).json({
    message: "Collections refreshed successfully",
    role,
    assign,
  });
});

export default router;
