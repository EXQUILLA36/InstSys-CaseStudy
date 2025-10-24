import React, { useEffect, useState, useRef, use } from "react";

export default function VoiceInput({ setInput, micON, sendMessage, toggleMic  }) {
  const [transcript, setTranscript] = useState("");
  const audioRef = useRef(null);

  const micStreamRef = useRef(null);
  const audioContextRef = useRef(null);
  const sourceRef = useRef(null);
  const gainNodeRef = useRef(null);
  const analyserRef = useRef(null);
  const dataArrayRef = useRef(null);

  const recognitionRef = useRef(null); 


  /*
  MICROPHONE SETUP
  So as the file name itself, this is the voice input Module. Now to further explain this code:
  -So we have a micON state that is passsed from the parent component which is yung aiChat.jsx
  -We then ask for browsers media permission sepecifically the microphone lang, then we store that as "stream"
  -That stream is like metadata lang ng audio, not the whole media itself
  -Then we store that to adioContextRef which is the main controller of all things audio
  Think of it as like sound studio you have this huge board with all the audio related stuff, So thats basically audioContext
  We took the stream then manipulate it using audioContext
  Moving on to the source, gainNode, analyser
  -Source is like the input, so we create a source from the stream
  -GainNode is like volume control, we set it to 1.0 which is normal volume
  -Analyser is like a visualizer, it gives us data about the audio frequencies
  We connect them all together, source to gainNode to analyser to destination (which is the speakers)
  Then we create a data array to hold the frequency data
  We then have a function detectVoice that continuously checks the audio data
  If the average frequency is above a certain threshold (30 in this case), we log that voice is detected
  Finally we have cleanup code to stop the mic and disconnect everything when micON is false or component unmounts
  */

   /*
  SPEECH RECOGNITION SETUP
  - We check if the browser supports SpeechRecognition API
  - If supported, we create a new instance of SpeechRecognition
  - We set it to continuous and interimResults to true so we get real-time results
  - We set the language to English (en-US)
  - We define onresult event handler to process the results
  - We define onerror event handler to handle any errors
  - We start the recognition
  - In the onresult handler, we build the transcript from the results
  - We update the transcript state and also call setInput to update the input field in real-time
  - If the result is final, we clear the transcript and toggle the mic off (which stops recognition)
  - We also have cleanup code to stop recognition and clear handlers when mic is turned off or component unmounts
   */ 

  useEffect(() => {
    console.log("micON changed:", micON);

    const startListening = async () => {
      try {
        // 1. Access microphone
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        micStreamRef.current = stream;

        // 2. Media or Audio environment whatever you wanna call it
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextRef.current = audioContext;

        // 3. This is like the audio board where you create different nodes
        const source = audioContext.createMediaStreamSource(stream);
        const gainNode = audioContext.createGain();
        const analyser = audioContext.createAnalyser();

        // Random stuff for the analyser edits frequence and volume stuff
        analyser.fftSize = 2048;
        gainNode.gain.value = 1.0;

        // 4. Connect them nodes together
        source.connect(gainNode);
        gainNode.connect(analyser);
        // analyser.connect(audioContext.destination); // This is the one that connect the audio to speaker (Prolly will remove this, but will stay for now for testing purposes)

        // Same as audioContextRef, we store these nodes in refs so we can access them later for cleanup
        sourceRef.current = source;
        gainNodeRef.current = gainNode;
        analyserRef.current = analyser;

        // 5. Speech Recognition Setup
        if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
          const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
          const recognition = new SpeechRecognition();
          recognition.continuous = true;
          recognition.interimResults = true;
          recognition.lang = "en-US";

          // We store the transcript in a ref so we can access it in the onresult handler
          recognition.onresult = (event) => {
            let interimTranscript  = '';
            for (let i = event.resultIndex; i < event.results.length; ++i) {
              interimTranscript += event.results[i][0].transcript;
            }
            // Then we set the transcript state and also update the input field in real-time
            // This way the user can see what they are saying as they speak
            console.log("Transcript:", interimTranscript );
            setTranscript(interimTranscript);
            setInput(interimTranscript);

            // After setting the interim transcript, we check if the result is final
            if (event.results[event.results.length - 1].isFinal && interimTranscript.trim() !== "") {
              console.log("Final transcript, submitting:", interimTranscript);
              sendMessage(interimTranscript);
              setTranscript("");
              setInput("");
              interimTranscript = "";
              toggleMic();
            }
          };

          recognition.onerror = (event) => {
            console.error("Speech recognition error:", event.error);
          };

          recognitionRef.current = recognition;
          recognition.start();
          console.log("Speech recognition started.");
        } else {
          console.log("Speech Recognition API is not supported in this browser.");
          alert("Speech Recognition API is not supported in this browser.");
          toggleMic();
        }

        // 6. Create a data array to hold the frequency data
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        dataArrayRef.current = dataArray;

        // 7. Function to detect voice
        const detectVoice = () => {
          analyser.getByteFrequencyData(dataArray);
          const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
          if (avg > 30) {
            console.log("Voice detected:", avg);
          }
          requestAnimationFrame(detectVoice); //This would be on loop para constanly ni chcheck niya yung media data 
        };

        // Start the whole function on loop
        detectVoice();
        console.log("Mic started and listening...");
      } catch (err) {
        console.error("Error accessing microphone:", err);
      }
    };

    const stopListening = () => {
      try {
        // 1. Stop all tracks in the MediaStream
        if (micStreamRef.current) {
          micStreamRef.current.getTracks().forEach((t) => {
            t.stop();
          });
          micStreamRef.current = null;
        }

        // 2. Disconnect all nodes
        sourceRef.current?.disconnect();
        gainNodeRef.current?.disconnect();
        analyserRef.current?.disconnect();

        // 3. Clear node refs
        sourceRef.current = null;
        gainNodeRef.current = null;
        analyserRef.current = null;

        // 4. Close the AudioContext safely
        if (audioContextRef.current && audioContextRef.current.state !== "closed") {
          audioContextRef.current.close();
          console.log("micON changed:", micON);
        } else {
          console.log("AudioContext already closed — skipping");
        }
          if (recognitionRef.current) {
            recognitionRef.current.stop();        
            recognitionRef.current.onresult = null;
            recognitionRef.current.onerror = null;
            recognitionRef.current = null;
            console.log("Speech recognition stopped.");
        }

      // Optional: Reset transcript state if desired
      setTranscript("");

        audioContextRef.current = null;
      } catch (err) {
        console.warn("Error stopping mic:", err);
      }
    };

    // Reactively start/stop when micON changes
    if (micON) {
      startListening();
    } else stopListening();

    // Cleanup when component unmounts
    return stopListening;
  }, [!micON]);


  return (
    micON ? <div className="flex flex-col">
      <div className="w-full px-4 text-sm text-gray-600">Listening...</div>
      <div className="w-full px-4">{transcript}</div>
    </div> : null
  )

}
