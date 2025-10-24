import React, { useState, useRef, useEffect } from "react";
import "../css/register.css";
import { motion } from "framer-motion";
import Popup from "../utils/popups";
import * as faceapi from "face-api.js";

function Register({ goLogin }) {
  const fileInputRef = useRef(null);
  const [preview, setPreview] = useState(null);
  const [descriptor, setDescriptor] = useState(null);
  const [file, setFile] = useState(null); // Store the actual image file

  const [popup, setPopup] = useState({
    show: false,
    type: "success",
    message: "",
  });

  const [showPassword, setShowPassword] = useState(false);

  const showPopup = (type, message) => {
    setPopup({ show: true, type, message });

    // Auto close after 2 seconds
    setTimeout(() => {
      setPopup({ show: false, type: "", message: "" });
    }, 2000);
  };

  const [form, setForm] = useState({
    firstName: "",
    middleName: "",
    lastName: "",
    password: "",
    confirmPassword: "",
    email: "",
    course: "",
    year: "",
    studentId: "",
  });

  const [passwordStrength, setPasswordStrength] = useState(0);
  const [showSuccess, setShowSuccess] = useState(false);
  const [error, setError] = useState("");

  // ------------------------------
  // Password Utilities
  // ------------------------------
  const handleChange = (e) => {
    const { name, value } = e.target;
    setForm({ ...form, [name]: value });

    if (name === "password") {
      setPasswordStrength(checkPasswordStrength(value));
    }
  };

  function validatePassword(password) {
    const lengthValid = password.length >= 8 && password.length <= 16;
    const upper = /[A-Z]/.test(password);
    const lower = /[a-z]/.test(password);
    const number = /[0-9]/.test(password);
    const special = /[^A-Za-z0-9]/.test(password);
    return lengthValid && upper && lower && number && special;
  }

  function checkPasswordStrength(password) {
    let strength = 0;
    if (password.length >= 8) strength++;
    if (/[A-Z]/.test(password)) strength++;
    if (/[a-z]/.test(password)) strength++;
    if (/[0-9]/.test(password)) strength++;
    if (/[^A-Za-z0-9]/.test(password)) strength++;
    if (password.length >= 12) strength++;
    return Math.min(strength, 5);
  }

  // ------------------------------
  // Handle File Upload
  // ------------------------------
  const handleOpenFile = () => {
    fileInputRef.current.click();
  };

  const handleFileChange = async (event) => {
    const selectedFile = event.target.files[0];
    if (!selectedFile) return;

    if (selectedFile.type !== "image/jpeg") {
      showPopup("error", "❌ Only JPG files are allowed!");
      return;
    }

    setFile(selectedFile);

    const reader = new FileReader();
    reader.onload = async (e) => {
      const imageSrc = e.target.result;
      setPreview(imageSrc);

      // Detect face descriptor
      const img = await faceapi.fetchImage(imageSrc);
      const detection = await faceapi
        .detectSingleFace(img)
        .withFaceLandmarks()
        .withFaceDescriptor();

      if (!detection) {
        showPopup("error", "⚠️ No face detected in image. Please try again.");
        setDescriptor(null);
        return;
      }

      setDescriptor(Array.from(detection.descriptor));
      console.log("✅ Face detected, descriptor generated:", detection.descriptor);
    };
    reader.readAsDataURL(selectedFile);
  };

  // ------------------------------
  // Submit Handler
  // ------------------------------
  const handleSubmit = async (e) => {
    e.preventDefault();

    const requiredFields = [
      "firstName",
      "lastName",
      "password",
      "confirmPassword",
      "email",
      "course",
      "year",
      "studentId",
    ];
    for (const field of requiredFields) {
      if (!form[field] || form[field].trim() === "") {
        showPopup("error", "❌ Please fill out all required fields.");
        return;
      }
    }

    if (!validatePassword(form.password)) {
      showPopup(
        "error",
        "❌ Password must be 8–16 chars long, with uppercase, lowercase, numbers & symbols."
      );
      return;
    }

    if (form.password !== form.confirmPassword) {
      showPopup("error", "❌ Passwords do not match.");
      return;
    }

    const pattern = /^PDM-\d{4}-\d{6}$/;
    if (!pattern.test(form.studentId)) {
      showPopup("info", "⚠ Student ID must follow: PDM-0000-000000");
      return;
    }

    if (!file || !descriptor) {
      console.log("file: ", file, "\ndescriptor: ", descriptor);
      showPopup("error", "⚠ Please upload a face photo before registering.");
      return;
    }

    setError("");

    // Construct payload with FormData (Flask-compatible)
    const formData = new FormData();
    console.log(file);
    formData.append("image", file);
    formData.append(
      "data",
      JSON.stringify({
        firstName: form.firstName,
        middleName: form.middleName,
        lastName: form.lastName,
        password: form.password,
        studentId: form.studentId,
        course: form.course,
        year: form.year,
        email: form.email,
        faceDescriptor: descriptor,
      })
    );

    try {
      const res = await fetch("http://127.0.0.1:5000/register", {
        method: "POST",
        body: formData,
      });

      const data = await res.json();

      if (res.ok) {
        setShowSuccess(true);
        goLogin();
      } else {
        showPopup("error", data.error || "❌ Registration failed.");
      }
    } catch (err) {
      showPopup("error", "⚠ Server error. Please try again later.");
    }
  };

  const handlePopupClose = () => {
    setShowSuccess(false);
    window.location.href = "/login";
  };

  // ------------------------------
  // Load FaceAPI Models
  // ------------------------------
  useEffect(() => {
    const loadModels = async () => {
      await Promise.all([
        faceapi.nets.tinyFaceDetector.loadFromUri("/models/face-api"),
        faceapi.nets.faceLandmark68Net.loadFromUri("/models/face-api"),
        faceapi.nets.faceRecognitionNet.loadFromUri("/models/face-api"),
        faceapi.nets.ssdMobilenetv1.loadFromUri("/models/face-api"),
      ]);
      console.log("✅ Face models loaded for Register");
    };
    loadModels();
  }, []);

  // ------------------------------
  // JSX Render
  // ------------------------------
  return (
    <>
      <div className="w-screen h-screen bg-[linear-gradient(to_right,rgba(121,44,26,1),rgba(240,177,0,0.6)),url('/images/PDM-Facade.png')] bg-cover bg-center flex justify-center items-center">
        <motion.div
          initial={{ opacity: 0, y: 50 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: "easeOut" }}
          className="flex flex-col md:flex-row w-[90vw] max-w-[1200px] h-auto justify-center gap-6 md:gap-10 items-center px-4 py-6 rounded-xl transition-all duration-300"
        >
         <div className="flex flex-col items-center gap-5 w-full md:w-fit h-fit overflow-y-auto md:overflow-visible max-h-[80vh] md:max-h-none bg-white/70 border-white border-2 rounded-lg p-6 md:p-10 shadow-gray-100/40 shadow-[3px_3px_2px_#6a7282,_-2px_-2px_2px_#d1d5dc] scrollbar-thin scrollbar-thumb-gray-400/50 scrollbar-track-transparent">
            <h1 className="font-medium font-sans text-[clamp(1.5rem,2vw,3rem)] text-center">
              Confirm your Account
            </h1>

            <div className="flex flex-col md:flex-row w-full gap-4">
              <button
                className="w-full md:w-[15vw] aspect-square bg-gray-300 rounded-lg border-gray-500 cursor-pointer border-dashed border-2 overflow-hidden relative group transition-all duration-300 hover:scale-[1.02]"
                onClick={handleOpenFile}
              >
                {preview ? (
                  <>
                    <img
                      src={preview}
                      alt="Preview"
                      className="w-full h-full object-cover rounded-lg"
                    />
                    <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center text-white font-medium transition-opacity">
                      Change Photo
                    </div>
                  </>
                ) : (
                  <span className="text-gray-600 font-medium text-[clamp(0.8rem,1vw,1.2rem)]">
                    Upload 1x1
                  </span>
                )}
              </button>

              {/* Hidden File Input */}
              <input
                type="file"
                accept="image/jpeg"
                ref={fileInputRef}
                onChange={handleFileChange}
                className="hidden"
              />

              <div className="flex flex-col gap-2 w-full">
                <input
                  name="firstName"
                  value={form.firstName}
                  onChange={handleChange}
                  type="text"
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="First Name"
                />
                <input
                  name="middleName"
                  value={form.middleName}
                  onChange={handleChange}
                  type="text"
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="Middle Name"
                />
                <input
                  name="lastName"
                  value={form.lastName}
                  onChange={handleChange}
                  type="text"
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="SurName"
                />
              </div>
            </div>

            <div className="flex flex-col md:flex-row w-full justify-around gap-2">
              <input
                name="studentId"
                value={form.studentId}
                onChange={handleChange}
                type="text"
                className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                placeholder="PDM-0000-000000"
              />
              <select
                name="course"
                value={form.course}
                onChange={handleChange}
                className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
              >
                <option value="" disabled>
                  -- Select Course --
                </option>
                <option value="BSCS">BS Computer Science</option>
                <option value="BSIT">BS Information Technology</option>
                <option value="BSHM">BS Hospitality Management</option>
                <option value="BSTM">BS Tourism Management</option>
                <option value="BSOAd">BS Office Administration</option>
                <option value="BECEd">BECEd</option>
                <option value="BTLEd">BTLEd</option>
              </select>

              <select
                name="year"
                value={form.year}
                onChange={handleChange}
                className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
              >
                <option value="" disabled>
                  -- Select Year --
                </option>
                <option value="First Year">First Year</option>
                <option value="Second Year">Second Year</option>
                <option value="Third Year">Third Year</option>
                <option value="Fourth Year">Fourth Year</option>
              </select>
            </div>

            <div className="flex flex-col w-full gap-2">
              <input
                name="email"
                value={form.email}
                onChange={handleChange}
                type="email"
                className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                placeholder="user.pdm@gmail.com"
              />
            </div>

            <div className="flex flex-col w-full gap-2">
              <div className="h-[2px] w-[100%] bg-gray-500"></div>

              <div className="flex flex-col md:flex-row gap-2">
                <input
                  name="password"
                  value={form.password}
                  onChange={handleChange}
                  type={showPassword ? "text" : "password"}
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="Create Password"
                />
                <input
                  name="confirmPassword"
                  value={form.confirmPassword}
                  onChange={handleChange}
                  type={showPassword ? "text" : "password"}
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-4 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="Confirm Password"
                />
              </div>

              <div className="password-strength-bar w-[100%] h-2 bg-gray-300 rounded">
                <div
                  className="password-strength-fill h-2 rounded"
                  style={{
                    width: `${(passwordStrength / 5) * 100}%`,
                    backgroundColor: [
                      "#e74c3c",
                      "#e67e22",
                      "#f1c40f",
                      "#2ecc71",
                      "#3498db",
                    ][passwordStrength > 0 ? passwordStrength - 1 : 0],
                  }}
                ></div>
              </div>

              <div className="flex flex-row justify-between items-center">
                <span className="password-strength-label text-sm">
                  {passwordStrength === 0
                    ? "Too weak"
                    : passwordStrength === 1
                    ? "Very Weak"
                    : passwordStrength === 2
                    ? "Weak"
                    : passwordStrength === 3
                    ? "Medium"
                    : passwordStrength === 4
                    ? "Strong"
                    : "Very Strong"}
                </span>
                <button
                  tabIndex={-1}
                  onClick={() => setShowPassword((prev) => !prev)}
                  className={`w-[1.2vw] aspect-square bg-center bg-contain bg-no-repeat hover:scale-102 transform-all duration-200 cursor-pointer  max-sm:w-[5vw]
                      ${
                        showPassword
                          ? "bg-[url(/password/passHide.svg)]"
                          : "bg-[url(/password/passShow.svg)]"
                      }`}
                ></button>
              </div>

              <button
                type="submit"
                className="login_button w-full py-[0.5vw] shadow-gray-400 shadow-md rounded-lg bg-yellow-500 text-[clamp(0.8rem,1.5vw,2rem)] font-sans font-medium cursor-pointer hover:scale-102 transition-all duration-300 "
                onClick={handleSubmit}
              >
                Register Account
              </button>

              <button
                type="button"
                onClick={() => (window.location.href = "/login")}
                className="font-sans font-medium underline text-[clamp(0.6rem,1.3vw,1.2rem)] cursor-pointer"
              >
                ← Already Have an Account?
              </button>
            </div>
          </div>

          {error && <p className="text-red-600 font-medium">{error}</p>}

          {showSuccess && (
            <div className="bg-green-200 text-green-800 p-3 rounded-lg">
              ✅ Registered successfully!
              <button onClick={handlePopupClose} className="ml-2 underline">
                Go to Login
              </button>
            </div>
          )}

          <Popup
            show={popup.show}
            type={popup.type}
            message={popup.message}
            onClose={() => setPopup({ show: false, type: "", message: "" })}
          />
        </motion.div>
      </div>
    </>
  );
}

export default Register;
