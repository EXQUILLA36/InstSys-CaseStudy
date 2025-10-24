import { useRef, useEffect, useState } from "react";
import * as faceapi from "face-api.js";
import React from "react";

function FaceScanner({ faceOn, onClose }) {
  const videoRef = useRef();
  const canvasRef = useRef();
  const [faceMatch, setFaceMatch] = useState(true);

  const [registeredDescriptor, setRegisteredDescriptor] = useState(null);
  const registeredDescriptorRef = useRef(null);
  const [modelsLoaded, setModelsLoaded] = useState(false);

  useEffect(() => {
    let intervalId = null;

    // ------------------------------
    // Start Camera
    // ------------------------------
    const startVideo = () => {
      navigator.mediaDevices
        .getUserMedia({
          video: {
            width: { ideal: 1280 },  // try 640, 720, 1080, 1920, etc.
            height: { ideal: 1280 },
            facingMode: "user", // ensures front camera on laptops/phones
          },
        })
        .then((stream) => {
          if (videoRef.current) {
            videoRef.current.srcObject = stream;
            videoRef.current.onplaying = () => {
              console.log("✅ Video feed is live");
              if (!intervalId) {
                intervalId = setInterval(detectMyFace, 500);
              }
            };
          }
        })
        .catch((err) => console.error("❌ Camera permission denied:", err));
    };

    // ------------------------------
    // Stop Camera
    // ------------------------------
    const stopVideo = () => {
      console.log("🛑 Stopping camera...");

      if (videoRef.current && videoRef.current.srcObject) {
        const tracks = videoRef.current.srcObject.getTracks();
        tracks.forEach((track) => track.stop());
        videoRef.current.srcObject = null;
        console.log("✅ Camera tracks stopped.");
      }

      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
        console.log("✅ Detection interval cleared.");
      }

      console.log("🧹 FaceScanner cleanup complete.");
    };

    // ------------------------------
    // Load FaceAPI Models
    // ------------------------------
    const loadModels = async () => {
      await Promise.all([
        faceapi.nets.tinyFaceDetector.loadFromUri("/models/face-api"),
        faceapi.nets.faceLandmark68Net.loadFromUri("/models/face-api"),
        faceapi.nets.faceRecognitionNet.loadFromUri("/models/face-api"),
        faceapi.nets.ssdMobilenetv1.loadFromUri("/models/face-api"),
      ]);
      console.log("✅ Models loaded");
      setModelsLoaded(true);
    };

    // ------------------------------
    // Register Face
    // ------------------------------
    const faceRegister = async () => {
      const img = await faceapi.fetchImage("./models/face/face.jpg");
      const detection = await faceapi
        .detectSingleFace(img)
        .withFaceLandmarks()
        .withFaceDescriptor();

      if (!detection) {
        console.log("⚠️ No face found in registration image");
        return;
      }

      setRegisteredDescriptor(detection.descriptor);
      registeredDescriptorRef.current = detection.descriptor;

      console.log("✅ Face descriptor loaded");
    };

    // ------------------------------
    // Face Detection Loop
    // ------------------------------
    const detectMyFace = async () => {
      if (!videoRef.current || !canvasRef.current) return;

      const video = videoRef.current;

      // Wait until metadata (videoWidth/Height) is available
      if (!video.videoWidth || !video.videoHeight) return;

      const detections = await faceapi
        .detectAllFaces(video, new faceapi.TinyFaceDetectorOptions())
        .withFaceLandmarks()
        .withFaceDescriptors();

      const canvas = canvasRef.current;
      const displaySize = { width: video.videoWidth, height: video.videoHeight };
      faceapi.matchDimensions(canvas, displaySize);

      const resizedDetections = faceapi.resizeResults(detections, displaySize);
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      faceapi.draw.drawDetections(canvas, resizedDetections);

      const descriptor = registeredDescriptorRef.current;
      if (!descriptor) {
        console.log("⏳ Waiting for registered face descriptor...");
        return;
      }

      const labeledDescriptor = new faceapi.LabeledFaceDescriptors(
        "Registered User",
        [new Float32Array(descriptor)]
      );

      const faceMatcher = new faceapi.FaceMatcher(labeledDescriptor, 0.6);

      resizedDetections.forEach((det) => {
        const bestMatch = faceMatcher.findBestMatch(det.descriptor);

        if(bestMatch.label === "unknown") {
          setFaceMatch(false);
          console.log("Face Do Not Match");
        } else {
          setFaceMatch(true);
          console.log("Face Match");
        }

        
        const box = det.detection.box;
        const drawBox = new faceapi.draw.DrawBox(box, {
          label:
            bestMatch.label === "unknown" ? "❌ Not Match" : "✅ Face Match",
            boxColor: bestMatch.label === "unknown" ? "red" : "green",
        });
        drawBox.draw(canvas);
      });
    };

    // ------------------------------
    // Initialized
    // ------------------------------
    const init = async () => {
      console.log("Initializing Face Scanner...");
      if (!modelsLoaded) await loadModels();
      await faceRegister();
      startVideo();
    };

    if (faceOn) init();

    // Cleanup when component unmounts or faceOn becomes false
    return () => {
      stopVideo();
    };
  }, [faceOn]);

  // ------------------------------
  // UI
  // ------------------------------
  return (
      
    <div className="flex flex-col w-full h-full">
      <div className="flex flex-col items-center justify-center w-full p-2 h-fit">
        <h1 className="text-[clamp(1rem,2vw,3rem)] font-medium">Face Detection</h1>
        <p className="text-[clamp(0.5rem,1vw,1rem)] text-center">Scan your face to verify your identity</p>
      </div>
      <div className="flex w-full h-full justify-center items-center">
        <video
          ref={videoRef}
          autoPlay
          muted
          playsInline
        className={`w-[100%] aspect-square rounded-full object-cover border-5
          ${faceMatch ? "border-green-600" : "border-red-600"}`}
        />
        <canvas
          ref={canvasRef}
          className="absolute top-0 left-0 w-full h-full"
        />
      </div>
    </div>
  );
}

export default FaceScanner;
