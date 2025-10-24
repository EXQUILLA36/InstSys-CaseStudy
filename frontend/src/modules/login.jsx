import { useState, useEffect } from "react";
import "../css/login.css"
import Popup from "../utils/popups";
import FaceScanner from "../utils/faceScanner";
import { motion } from "framer-motion";

function Login({ goRegister, goDashboard }) {
  const [loading, setLoading] = useState(true); // loading until backend is ready
  const [form, setForm] = useState({
    studentId: "",
    email: "",
    password: "",
  });
  const [error, setError] = useState("");
 

  const [faceOn, setFaceOn] = useState(false);

  const [showPassword, setShowPassword] = useState(false);

  const [popup, setPopup] = useState({
    show: false,
    type: "success",
    message: "",
  });

  const showPopup = (type, message) => {
    setPopup({ show: true, type, message });

    // Auto close after 2 seconds
    setTimeout(() => {
      setPopup({ show: false, type: "", message: "" });
    }, 2000);
  };

  useEffect(() => {
    const checkServer = async () => {
      try {
        const res = await fetch("http://127.0.0.1:5000/health");
        if (res.ok) {
          setLoading(false); // backend is ready → hide loading
        } else {
          setTimeout(checkServer, 200); // retry after 1s
        }
      } catch {
        setTimeout(checkServer, 200); // retry after 1s
      }
    };

    checkServer();
  }, []);

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const processLoginSuccess = async (data) => {
    localStorage.setItem("studentId", data.studentId);
    localStorage.setItem("role", data.role);
    showPopup("success", "Login successful!");
    // Trigger backend to refresh collections for new role/assign
    await fetch("http://127.0.0.1:5000/refresh_collections", { method: "POST" });
    goDashboard();
  };

  const handleLogin = async (e) => {
    e.preventDefault();

    setError("");

    try {
      const res = await fetch("http://127.0.0.1:5000/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const data = await res.json();
      if (res.ok) {
        processLoginSuccess(data);
      } else {
        showPopup("error", data.error || "Login failed");
      }
    } catch {
      setError("Server error");
    }
  };

  const handleGuestLogin = async () => {
    const guestId = "PDM-0000-000000";
    try {
      // goDashboard();
      // Fetch guest.json to get the role
      const guestRes = await fetch("http://127.0.0.1:5000/student/" + guestId);
      // if (!guestRes.ok) {
      //   showPopup("error", "Guest account not found");
      //   return;
      // }
      const guestData = await guestRes.json();
      const guestRole = guestData.role || "Guest";
      console.log("guest result: ",guestData);

      const res = await fetch("http://127.0.0.1:5000/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          studentId: guestId,
          email: "", // guest.json email is empty
          password: "", // guest.json password is empty
        }),
      });
      const data = await res.json();
      if (res.ok) {
        processLoginSuccess({ studentId: guestId, role: guestRole });
      } else {
        showPopup("error", data.error || "Guest login failed");
      }
    } catch {
      setError("Server error");
    }
  };

  const toggleFace = () => {
    setFaceOn((prev) => !prev)
    console.log(faceOn)
  };

  return (
    <>
      {loading && (
        <div className="absolute flex-col gap-5 w-full h-full z-50 flex items-center justify-center bg-amber-900/10 backdrop-blur-2xl">
          <span className="loader"></span>
          <span class="loaderBar"></span>
        </div>
      )}

      {!loading && (
        <motion.div 
          initial={{ opacity: 0.6 }}   // Start below, invisible
          animate={{ opacity: 1 }}    // Slide up & fade in
          transition={{ duration: 0.5, ease: "easeOut" }} 
          className="screen p-10 w-screen h-screen bg-[linear-gradient(to_right,rgba(121,44,26,0.7),rgba(240,177,0,0.6)),url('/images/PDM-Facade.png')] bg-cover bg-center flex flex-row justify-between items-center">
          {/* Left Card */}
          <div className="w-full h-screen flex flex-col gap-2 justify-center items-center">
            <div className="logo bg-[url('/images/PDM-Logo.svg')] bg-contain w-[30vw] h-[30vw]"></div>

            <h1 className="text-[clamp(1rem,3vw,4rem)] text-center font-sans font-medium text-white text-yellow- max-sm:hidden">
              Pambayang Dalubhasaan ng Marilao
            </h1>

          </div>
          {/* Right Card */}
          <motion.div
            initial={{ opacity: 0.5, x: 5 }}   // Start below, invisible
            animate={{ opacity: 1, x: 0 }}    // Slide up & fade in
            transition={{ duration: 0.5, ease: "easeOut" }} 
            className="login_panel flex flex-col backdrop-blur-sm gap-5 justify-center items-center w-[50%] h-fit py-[2.5vw] !rounded-3xl bg-white/60 shadow-[3px_3px_2px_#6a7282,_-2px_-2px_2px_#d1d5dc] border-white border-2">
            <h1 className="font-medium font-sans text-[clamp(2rem,5vw,4rem)]">Log In</h1>
            {/* Login Card */}
            <motiondiv className="login_card w-full h-fit rounded-xl">
              <div
                className="flex flex-col gap-4 justify-center items-center px-10"
              >
                <input
                  type="text"
                  name="studentId"
                  required
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-5 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="Enter Student ID"
                  value={form.studentId}
                  onChange={handleChange}
                />

                <input
                  type="text"
                  name="email"
                  required
                  className="focus:outline-none focus:border-b-2 border-amber-500/50 w-full py-5 px-2 bg-white border-0 rounded-lg drop-shadow-lg shadow-gray-500"
                  placeholder="Enter Student Email"
                  value={form.email}
                  onChange={handleChange}
                />
                <div className="flex justify-between w-full bg-white pr-2 border-0 rounded-lg drop-shadow-lg shadow-gray-500 flex-row">
                  <input
                    type={showPassword ? "text" : "password"}
                    name="password"
                    required
                    className="w-[70%] py-5 px-2 focus:outline-none bg-white border-0 rounded-lg"
                    placeholder="Enter Password"
                    autoComplete="new-password"
                    value={form.password}
                    onChange={handleChange}
                  />
                  <button
                    tabIndex={-1}
                    onClick={() => setShowPassword((prev) => !prev)}
                    className={`w-[1.5vw] aspect-square bg-center bg-contain bg-no-repeat hover:scale-102 transform-all duration-200 cursor-pointer  max-sm:w-[5vw]
                      ${showPassword ? "bg-[url(/password/passHide.svg)]" : "bg-[url(/password/passShow.svg)]" }`}
                  ></button>

                </div>
                
                {error && (
                  <div className="text-red-600 font-medium">{error}</div>
                )}
                <button
                  type="submit"
                  className="login_button w-full py-[0.5vw] shadow-gray-400 shadow-md rounded-lg bg-yellow-500 text-[clamp(0.5rem,1.5vw,2rem)] font-sans font-medium cursor-pointer hover:scale-102 transition-all duration-300 "
                  onClick={handleLogin}
                >Log In</button>

                 
                {/* SIGN-IN BUTTON */}
                <button
                  type="button"
                  className="font-sans font-medium text-black shadow-[1px_2px_5px_#6a7282,_-1px_-1px_10px_#eeffff] py-3 px-10 rounded-lg w-full bg-gray-100/50 text-[clamp(0.6rem,1.3vw,1.2rem)] cursor-pointer hover:scale-102 transition-all duration-300"
                  onClick={handleGuestLogin}
                >
                  Sign-In as Guest
                </button>
              </div>
            </motiondiv>


            {/* REGISTER BUTTON */}
            <button
                type="button"
                className="font-sans font-medium underline text-[clamp(0.6rem,1.3vw,1.2rem)] cursor-pointer"
                onClick={goRegister}
              >
                Create Account →
              </button>
          </motion.div>
          {/* Popups */}
           <Popup
              show={popup.show}
              type={popup.type}
              message={popup.message}
              onClose={() =>
                setPopup({ show: false, type: "", message: "" })
              }
            />
            {/* Face Toggle Button */}
            <button 
              className="absolute bottom-7 right-10 w-[4vw] shadow-gray-400 shadow-md cursor-pointer bg-gray-200 rounded-2xl hover:scale-105 transition-all duration-300"
              title="Face Scanner"
              onClick={toggleFace}
            ><img src="./ico/face-scanner.svg" alt="" /></button>
            {/* Face modal */}
            {faceOn && (
              <div className="absolute p-5  inset-0 bg-black/40 flex flex-col gap-2 items-center justify-center z-50 ">
                <div className="bg-gray-200 p-4 rounded-xl shadow-md border-2 border-black/20 shadow-gray-900 relative w-[40%] h-fit flex flex-col  justify-center  max-sm:w-[80%]">
                  <FaceScanner faceOn={faceOn} onClose={toggleFace} />
                </div>
                <button
                  onClick={toggleFace} 
                  className="absolute w-[5vw] rounded-full p-5 top-5 right-10 cursor-pointer">
                    <img src="./ico/white-cross.svg" alt="" />
                  </button>
              </div>
            )}
        </motion.div>
        
      )}
    </>
  );
}

export default Login;
