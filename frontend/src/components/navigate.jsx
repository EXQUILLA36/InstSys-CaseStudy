import React, { useState,useEffect } from "react";
import Login from "../modules/login.jsx";
import Dashboard from "../modules/dashboard.jsx";
import Register from "../modules/register.jsx";
import ChatPrompt from "../modules/chatPrompt.jsx";
import Account from "./account.jsx";

function Navigate() {
  const [page, setPage] = useState("login");
  const [chatInitialView, setChatInitialView] = useState("chat");
  const [studentId, setStudentId] = useState(null);

  // useEffect(() => {
  //   const savedStudentId = localStorage.getItem("studentId");
  //   if (savedStudentId) {
  //     setStudentId(savedStudentId);
  //     setPage("dashboard");
  //   }
  // }, []);

   const handleLogin = (id) => {
    localStorage.setItem("studentId", id);
    setStudentId(id);
    setPage("dashboard");
  };

  const handleLogout = () => {
    localStorage.removeItem("studentId");
    setStudentId(null);
    setPage("login");
  };

  if (page === "register") return <Register goLogin={() => setPage("login")} />;

  if (page === "dashboard")
    return (
      <Dashboard
        goChat={() => {
          setChatInitialView("chat");
          setPage("chat");
        }}
        goLogin={() => setPage("login")}
        onLogout={handleLogout}
        studentId={studentId}
        goAccounts={() => {
          setChatInitialView("account");
          setPage("chat");
        }}
      />
    );

  if (page === "chat")
    return (
      <ChatPrompt
        goDashboard={() => setPage("dashboard")}
        initialView={chatInitialView}
        studentId={studentId}
      />
    );

  if (page === "account")
    return <Account goDashboard={() => setPage("dashboard")} />;

  return (
    <Login
      goRegister={() => setPage("register")}
      goDashboard={() => setPage("dashboard")}
      onLogin={handleLogin}
    />
  );
}

export default Navigate;
