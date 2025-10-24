const mongoose = require("mongoose")

const userSchema = new mongoose.Schema({
  _id: { type: String, required: true },
  student_name: { type: String, required: true },
  year: { type: String, required: true },
  course: { type: String, required: true },
  email: { type: String, required: true, unique: true },
  password: { type: String, required: true },
  role: { type: String, required: true },
})
const fileSchema = new mongoose.Schema({
  file_name: { type: String, require: true },
  fileType: { type: String, required: true },
  file: { type: Buffer, required: true },
  fileHash: { type: String, required: true, unique: true },
})